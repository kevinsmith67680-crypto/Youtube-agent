"""Detection service: Phase 3 time series in, persistent surge episodes out.

``DetectionService.run`` performs one detection run:

1. **select** series: monitoring targets in scope (category / query / all) or the series
   queued by the Phase 3 executor (transactional outbox, ``detection_queue``);
2. **read** (per series): every revision available to the agent at the run time
   (knowledge at T, agent availability), the saved detector state, the observations
   newer than the state's watermark; preprocess (comparability, grid, quality) and run
   the engine (incremental, or deterministic recomputation for late / revised data);
3. **multiple testing**: all calibrated hypotheses of this run form one family
   (Benjamini-Hochberg / -Yekutieli); the rolling 24 h alert budget is loaded;
4. **write** (per series, one transaction): advisory lock on the series, optimistic
   state-version check (a concurrent worker that got there first wins and this one skips),
   detector state, anomaly results (idempotent: unique per observation revision, detector,
   algorithm and configuration version), alert decisions and episode updates (advisory
   lock per episode key), queue entry cleared unless re-marked meanwhile;
5. **stale episodes** are resolved; the run record stores counts, the multiple-testing
   family, the budget and errors.

Processing time (``evaluated_at``, ``first_detected_at``) is always kept separate from
event time (observation windows). Synthetic series are skipped unless explicitly
included, and synthetic episodes are never listed as production alerts.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from trend_agent.db.detection_models import SurgeEventRow
from trend_agent.db.discovery_models import DiscoveredKeywordRow
from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.repositories.detection_runs import (
    DetectionQueueRepository,
    DetectionRunRepository,
    result_row,
)
from trend_agent.db.repositories.detector_state import DetectorStateRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.surge_events import SqlEpisodeStore, SurgeEventRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.detection.alert_policy import AlertDecision, DecisionContext, decide
from trend_agent.detection.cold_start import CorroborationContext
from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.detection.contract import make_renderer
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import EngineOutput, EngineState, PointEvaluation, SeriesEngine
from trend_agent.detection.events import EpisodeChange, EpisodeManager
from trend_agent.detection.preprocessing import AnalysisSeries, knowledge_at, prepare
from trend_agent.detection.significance import (
    AlertBudget,
    FamilyResult,
    Hypothesis,
    NullDistribution,
    NullLookup,
    control,
)
from trend_agent.discovery.models import Category, EvidenceType, NoveltyKind
from trend_agent.logging import get_logger, log_event
from trend_agent.monitoring.config import LoadedMonitoringConfig
from trend_agent.monitoring.freshness import assess_freshness, estimate_delay
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.monitoring.scheduler import period_step
from trend_agent.schemas.detection import DetectorType, RunStatus, RunType
from trend_agent.schemas.evidence import SurgeClassification, classify
from trend_agent.schemas.monitoring import HealthStatus, MonitoringStatus
from trend_agent.schemas.surge import SurgeAlert, TargetSurgeView
from trend_agent.utils.time import Clock, utc_now

_log = get_logger(__name__)
QUEUE_LEASE = timedelta(minutes=5)


class ConcurrentUpdateError(Exception):
    """Another worker updated this series' detector state first."""


@dataclass(frozen=True)
class TargetInfo:
    id: int
    query_id: int
    query: str
    keyword_id: int | None
    topic_cluster_id: int | None
    category: Category
    region: str
    language: str | None
    is_proxy: bool
    is_synthetic: bool
    status: MonitoringStatus
    provider: str | None
    tier: str
    latest_period_end: datetime | None
    last_attempt_at: datetime | None
    last_no_data_at: datetime | None
    entity_type: str | None
    entity_first_seen_at: datetime | None
    novelty_kind: str | None


@dataclass
class SeriesWork:
    series_id: int
    target: TargetInfo
    series: AnalysisSeries
    output: EngineOutput
    stored_version: int | None
    freshness: str | None
    queued_until: datetime | None


@dataclass
class DetectionReport:
    run_id: uuid.UUID
    run_type: RunType
    as_of: datetime
    series_selected: int = 0
    series_evaluated: int = 0
    observations_evaluated: int = 0
    targets_evaluated: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    recomputations: Counter[str] = field(default_factory=Counter)
    hypotheses: int = 0
    candidates: int = 0
    accepted: int = 0
    notifications: int = 0
    episodes_updated: int = 0
    changes: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    multiple_testing: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    stale_resolved: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "run_type": self.run_type.value,
            "as_of": self.as_of.isoformat(),
            "series_selected": self.series_selected,
            "series_evaluated": self.series_evaluated,
            "observations_evaluated": self.observations_evaluated,
            "targets_evaluated": self.targets_evaluated,
            "skipped": dict(self.skipped),
            "recomputations": dict(self.recomputations),
            "hypotheses_tested": self.hypotheses,
            "candidates_found": self.candidates,
            "accepted_after_control": self.accepted,
            "notifications": self.notifications,
            "episodes_updated": self.episodes_updated,
            "stale_resolved": self.stale_resolved,
            "changes": self.changes,
            "errors": self.errors,
            "multiple_testing": self.multiple_testing,
            "budget": self.budget,
            "seconds": round(self.seconds, 3),
        }


def target_info(row: MonitoringTargetRow, query: str, novelty: str | None) -> TargetInfo:
    return TargetInfo(
        id=row.id,
        query_id=row.query_id,
        query=query,
        keyword_id=row.keyword_id,
        topic_cluster_id=row.topic_cluster_id,
        category=row.category,
        region=row.region,
        language=row.language,
        is_proxy=row.is_proxy,
        is_synthetic=row.is_synthetic,
        status=row.status,
        provider=row.provider_in_use or row.preferred_provider,
        tier=row.tier.value,
        latest_period_end=row.latest_period_end,
        last_attempt_at=row.last_attempt_at,
        last_no_data_at=row.last_no_data_at,
        entity_type=row.entity_type,
        entity_first_seen_at=row.entity_first_seen_at,
        novelty_kind=novelty or row.novelty_kind,
    )


def meta_for(target: TargetInfo, series: AnalysisSeries) -> EpisodeMeta:
    return EpisodeMeta(
        target_id=target.id,
        query=target.query,
        query_id=target.query_id,
        keyword_id=target.keyword_id,
        topic_cluster_id=target.topic_cluster_id,
        category=target.category,
        region=series.region,
        language=series.language,
        provider=series.provider,
        measurement=series.measurement,
        is_proxy=series.is_proxy,
        classification=classify(series.measurement, is_proxy=series.is_proxy),
        is_synthetic=series.is_synthetic,
        step=period_step(series.granularity) or timedelta(days=1),
    )


def load_nulls(session: Session) -> NullLookup:
    lookup = NullLookup()
    for row in DetectionRunRepository(session).active_calibrations():
        if "tail" not in row.parameters:
            continue
        lookup.nulls[row.stratum_key] = NullDistribution.from_parameters(
            row.stratum_key, row.calibration_version, row.source, row.parameters
        )
    return lookup


class DetectionService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        loaded: LoadedDetectionConfig,
        monitoring: LoadedMonitoringConfig | None = None,
        clock: Clock = utc_now,
        include_synthetic: bool | None = None,
    ) -> None:
        self._factory = session_factory
        self.loaded = loaded
        self.cfg = loaded.config
        self.monitoring = monitoring
        self._clock = clock
        self.include_synthetic = (
            self.cfg.integration.include_synthetic
            if include_synthetic is None
            else include_synthetic
        )

    @property
    def session_factory(self) -> SessionFactory:
        return self._factory

    def now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------ selection
    def _targets(
        self,
        repo: MonitoringRepository,
        *,
        category: Category | None,
        query: str | None,
        target_ids: Sequence[int] | None,
        include_synthetic: bool,
    ) -> dict[int, TargetInfo]:
        statuses = [s for s in MonitoringStatus if s is not MonitoringStatus.DISABLED]
        rows: list[MonitoringTargetRow] = []
        after: int | None = None
        while True:
            page = repo.targets(
                statuses=statuses,
                category=category,
                query_text=query,
                include_synthetic=include_synthetic,
                after_id=after,
                limit=1000,
            )
            rows.extend(page)
            if len(page) < 1000:
                break
            after = page[-1].id
        if target_ids is not None:
            wanted = set(target_ids)
            rows = [r for r in rows if r.id in wanted]
        texts = repo.query_text(list({r.query_id for r in rows}))
        keywords = repo.keywords_by_ids([r.keyword_id for r in rows if r.keyword_id])
        out: dict[int, TargetInfo] = {}
        for r in rows:
            kw: DiscoveredKeywordRow | None = keywords.get(r.keyword_id) if r.keyword_id else None
            novelty = kw.novelty_kind.value if kw is not None else None
            out[r.id] = target_info(r, texts.get(r.query_id, ("?", "?"))[0], novelty)
        return out

    def _freshness(
        self,
        target: TargetInfo,
        delays: dict[str, Any],
        health: dict[str, str],
        now: datetime,
    ) -> str | None:
        if self.monitoring is None or target.provider is None:
            return None
        cfg = self.monitoring.config
        profile = cfg.providers.get(target.provider)
        tier_rule = cfg.tiers.rule(_tier(target.tier))
        assessment = assess_freshness(
            now=now,
            profile=profile,
            provider_available=profile is not None and profile.enabled,
            provider_unhealthy=health.get(target.provider) == HealthStatus.UNHEALTHY.value,
            latest_period_end=target.latest_period_end,
            last_attempt_at=target.last_attempt_at,
            last_no_data_at=target.last_no_data_at,
            delay=delays.get(target.provider),
            planned_periods=tier_rule.collect_every_periods,
        )
        return assessment.status.value

    def _cold_context(
        self, repo: MonitoringRepository, target: TargetInfo, as_of: datetime
    ) -> CorroborationContext:
        types: Counter[str] = Counter()
        providers: set[str] = set()
        if target.keyword_id is not None:
            start = as_of - self.cfg.cold_start.corroboration_window
            for item in repo.evidence_items(target.keyword_id, start, as_of):
                kind = item.simulated_evidence_type if item.is_synthetic else item.evidence_type
                if kind is None or kind is EvidenceType.SYNTHETIC_TEST_DATA:
                    continue
                types[kind.value] += 1
                providers.add(item.provider_name)
        novelty = None
        if target.novelty_kind:
            try:
                novelty = NoveltyKind(target.novelty_kind)
            except ValueError:
                novelty = None
        return CorroborationContext(
            novelty_kind=novelty,
            entity_type=target.entity_type,
            entity_first_seen_at=target.entity_first_seen_at,
            evidence_types=dict(types),
            providers=tuple(sorted(providers)),
        )

    # ------------------------------------------------------------------ run
    def run(
        self,
        *,
        run_type: RunType = RunType.LIVE,
        category: Category | None = None,
        query: str | None = None,
        target_ids: Sequence[int] | None = None,
        include_synthetic: bool | None = None,
        from_queue: bool = False,
        worker: str = "detect",
        queue_limit: int = 500,
        nulls: NullLookup | None = None,
    ) -> DetectionReport:
        started = time.perf_counter()
        now = self._clock()
        include = self.include_synthetic if include_synthetic is None else include_synthetic
        cfg = self.cfg
        with transaction(self._factory) as session:
            run_id = DetectionRunRepository(session).start(
                run_type=run_type,
                scope={
                    "category": category.value if category else None,
                    "query": query,
                    "target_ids": list(target_ids) if target_ids else None,
                    "from_queue": from_queue,
                    "include_synthetic": include,
                    "worker": worker,
                },
                now=now,
                as_of=now,
                algorithm_version=cfg.algorithm_version,
                configuration_version=self.loaded.version_tag,
                threshold_version=cfg.threshold_version,
                is_synthetic=include,
            )
        report = DetectionReport(run_id, run_type, now)
        status = RunStatus.FAILED
        try:
            works = self._read(
                report,
                now=now,
                category=category,
                query=query,
                target_ids=target_ids,
                include=include,
                from_queue=from_queue,
                worker=worker,
                queue_limit=queue_limit,
                nulls=nulls,
            )
            family = self._family(works, report)
            budgets = self._budgets(now)
            for work in works:
                self._write(work, run_id, now, family, budgets, report)
            report.stale_resolved = self._resolve_stale(now, include, run_id)
            report.budget = {
                "real": budgets[False].as_dict(),
                **({"synthetic": budgets[True].as_dict()} if include else {}),
            }
            status = RunStatus.PARTIAL if report.errors else RunStatus.SUCCEEDED
        except Exception as exc:
            report.errors.append({"error": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            report.seconds = time.perf_counter() - started
            with transaction(self._factory) as session:
                DetectionRunRepository(session).finish(
                    run_id,
                    {
                        "status": status,
                        "completed_at": self._clock(),
                        "targets_evaluated": report.targets_evaluated,
                        "targets_skipped": sum(report.skipped.values()),
                        "series_evaluated": report.series_evaluated,
                        "observations_evaluated": report.observations_evaluated,
                        "hypotheses_tested": report.hypotheses,
                        "candidates_found": report.candidates,
                        "accepted_after_control": report.accepted,
                        "alerts_created": report.notifications,
                        "episodes_updated": report.episodes_updated,
                        "errors": len(report.errors),
                        "error_details": report.errors[:50],
                        "multiple_testing": {**report.multiple_testing, "budget": report.budget},
                        "stats": {
                            "skipped": dict(report.skipped),
                            "recomputations": dict(report.recomputations),
                            "seconds": round(report.seconds, 3),
                            "stale_resolved": report.stale_resolved,
                        },
                    },
                )
        log_event(
            _log,
            "detect.run_completed",
            run_id=str(run_id),
            run_type=run_type.value,
            series=report.series_evaluated,
            observations=report.observations_evaluated,
            notifications=report.notifications,
            errors=len(report.errors),
        )
        return report

    def _read(
        self,
        report: DetectionReport,
        *,
        now: datetime,
        category: Category | None,
        query: str | None,
        target_ids: Sequence[int] | None,
        include: bool,
        from_queue: bool,
        worker: str,
        queue_limit: int,
        nulls: NullLookup | None,
    ) -> list[SeriesWork]:
        cfg = self.cfg
        works: list[SeriesWork] = []
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            lookup = nulls if nulls is not None else load_nulls(session)
            engine = SeriesEngine(self.loaded, lookup)
            targets = self._targets(
                repo,
                category=category,
                query=query,
                target_ids=target_ids,
                include_synthetic=include,
            )
            queued: dict[int, datetime] = {}
            if from_queue:
                claims = DetectionQueueRepository(session).claim(
                    worker, now, QUEUE_LEASE, queue_limit
                )
                queued = {c.series_id: c.last_marked_at for c in claims}
                summaries = ts.series_summaries(series_ids=list(queued)) if queued else []
            else:
                summaries = ts.series_summaries(target_ids=list(targets)) if targets else []
            delays: dict[str, Any] = {}
            health: dict[str, str] = {}
            if self.monitoring is not None:
                delays = {
                    name: estimate_delay(ts.delay_samples(name), profile)
                    for name, profile in self.monitoring.config.providers.items()
                }
                health = {h.provider_name: h.status.value for h in repo.all_health()}
            states = DetectorStateRepository(session)
            seen_targets: set[int] = set()
            for summary in summaries:
                report.series_selected += 1
                target = targets.get(summary.target_id or -1)
                if target is None:
                    report.skipped["target_out_of_scope_or_disabled"] += 1
                    continue
                if summary.is_synthetic and not include:
                    report.skipped["synthetic_excluded"] += 1
                    continue
                step = period_step(_granularity(summary.granularity)) or timedelta(days=1)
                limit = cfg.preprocessing.max_history_periods.get(
                    _granularity(summary.granularity), 730
                )
                points = ts.points(
                    series_ids=[summary.series_id],
                    start=now - step * (limit + 1),
                    current_only=False,
                    limit=1_000_000,
                )
                known = knowledge_at(points, now, AvailabilityMode.AGENT)
                stored = states.load(summary.series_id)
                state = EngineState.from_payloads(stored.payloads) if stored else None
                new_ids: set[int] | None = None
                if state is not None:
                    new_ids = {p.id for p in known if p.id > state.watermark_obs_id}
                    config_changed = (
                        state.algorithm_version != cfg.algorithm_version
                        or state.config_digest != self.loaded.digest
                    )
                    if not new_ids and not config_changed:
                        report.skipped["no_new_observations"] += 1
                        continue
                series = prepare(
                    known, cfg, as_of=now, mode=AvailabilityMode.AGENT, already_known=True
                )
                cold = None
                if series.n_usable < cfg.baseline.robust.min_samples:
                    cold = self._cold_context(repo, target, now)
                output = engine.run(
                    series,
                    state,
                    new_obs_ids=new_ids,
                    category=target.category.value,
                    cold_context=cold,
                )
                if output.skipped:
                    report.skipped[f"not_analysable:{series.status.value}"] += 1
                if output.recompute_reason and output.recompute_reason != "initial":
                    report.recomputations[output.recompute_reason] += 1
                report.series_evaluated += 1
                report.observations_evaluated += len(output.evaluations)
                seen_targets.add(target.id)
                works.append(
                    SeriesWork(
                        summary.series_id,
                        target,
                        series,
                        output,
                        stored.version if stored else None,
                        self._freshness(target, delays, health, now),
                        queued.get(summary.series_id, now if not from_queue else None),
                    )
                )
            if from_queue:
                queue = DetectionQueueRepository(session)
                for sid, marked in queued.items():
                    if all(w.series_id != sid for w in works):
                        queue.done(sid, marked)
            report.targets_evaluated = len(seen_targets)
        return works

    def _family(self, works: list[SeriesWork], report: DetectionReport) -> FamilyResult | None:
        hyps = [
            Hypothesis(h.key, h.p_value, h.exact)
            for w in works
            for ev in w.output.evaluations
            if not ev.is_late
            for h in ev.hypotheses
        ]
        report.candidates = sum(
            1
            for w in works
            for ev in w.output.evaluations
            if not ev.is_late and (ev.any_trigger or ev.any_watch)
        )
        mt = self.cfg.multiple_testing
        if not hyps:
            report.multiple_testing = {
                "method": "alert_budget",
                "family_size": 0,
                "note": "no calibrated p-values in this run",
            }
            return None
        family = control(hyps, mt.method, mt.fdr_q)
        report.hypotheses = family.family_size
        report.accepted = len(family.accepted)
        report.multiple_testing = {
            **family.as_dict(),
            "threshold_version": self.cfg.threshold_version,
            "note": "per-run FDR control; not an online guarantee across runs",
        }
        return family

    def _budgets(self, now: datetime) -> dict[bool, AlertBudget]:
        mt = self.cfg.multiple_testing
        with transaction(self._factory) as session:
            repo = SurgeEventRepository(session)
            return {
                nature: AlertBudget(
                    limit_per_day=mt.max_new_alerts_per_day,
                    used_last_24h=repo.notifications_since(
                        now - timedelta(days=1), synthetic=nature
                    ),
                    target_false_alerts_per_day=mt.target_false_alerts_per_day,
                )
                for nature in (False, True)
            }

    def _write(
        self,
        work: SeriesWork,
        run_id: uuid.UUID,
        now: datetime,
        family: FamilyResult | None,
        budgets: dict[bool, AlertBudget],
        report: DetectionReport,
    ) -> None:
        cfg = self.cfg
        series = work.series
        try:
            with transaction(self._factory) as session:
                states = DetectorStateRepository(session)
                if not states.try_lock_series(work.series_id):
                    raise ConcurrentUpdateError("series locked by another worker")
                state = work.output.state
                saved = states.save(
                    series_id=work.series_id,
                    target_id=work.target.id,
                    provider=series.provider,
                    measurement=series.measurement,
                    normalization_key=series.normalization_key,
                    payloads=state.payloads(),
                    expected_version=work.stored_version,
                    last_observation_id=state.last_obs_id,
                    last_period_end=state.processed_until_period_end,
                    watermark=state.watermark_obs_id,
                    algorithm_version=cfg.algorithm_version,
                    configuration_version=self.loaded.version_tag,
                    is_synthetic=series.is_synthetic,
                    now=now,
                )
                if not saved:
                    raise ConcurrentUpdateError("detector state changed since it was read")
                if work.output.evaluations and series.family is not None:
                    self._decide_and_store(session, work, run_id, now, family, budgets, report)
                if work.queued_until is not None:
                    DetectionQueueRepository(session).done(work.series_id, work.queued_until)
        except ConcurrentUpdateError as exc:
            report.skipped["concurrent_update"] += 1
            log_event(_log, "detect.concurrent_skip", series_id=work.series_id, reason=str(exc))
        except Exception as exc:
            report.errors.append(
                {"series_id": work.series_id, "error": f"{type(exc).__name__}: {exc}"[:500]}
            )
            log_event(
                _log,
                "detect.series_failed",
                logging.ERROR,
                series_id=work.series_id,
                error=type(exc).__name__,
            )

    def _decide_and_store(
        self,
        session: Session,
        work: SeriesWork,
        run_id: uuid.UUID,
        now: datetime,
        family: FamilyResult | None,
        budgets: dict[bool, AlertBudget],
        report: DetectionReport,
    ) -> None:
        cfg = self.cfg
        series = work.series
        assert series.family is not None
        meta = meta_for(work.target, series)
        ctx = DecisionContext(
            classification=meta.classification,
            measurement=series.measurement,
            family=series.family,
            multiple_testing=family,
            comparability=series.comparability.status if series.comparability else None,
            freshness=work.freshness,
        )
        decisions = [(ev, decide(ev, ctx, cfg)) for ev in work.output.evaluations]
        rows: list[dict[str, Any]] = []
        for ev, decision in decisions:
            rows.extend(self._result_rows(ev, decision, run_id, now, series.is_synthetic))
        ids = DetectionRunRepository(session).insert_results(rows)
        manager = EpisodeManager(
            SqlEpisodeStore(session, run_id),
            cfg,
            config_version=self.loaded.version_tag,
            budget=budgets[series.is_synthetic],
            renderer=make_renderer(
                loaded=self.loaded,
                family=series.family,
                comparability=series.comparability,
                multiple_testing=family,
                freshness=work.freshness,
                now=now,
            ),
        )
        for ev, decision in decisions:
            obs = ev.observation_id
            result_ids = {
                d: ids[(obs, d.value)]
                for d in DetectorType
                if obs is not None and (obs, d.value) in ids
            }
            change = manager.apply(
                ev, decision, meta, now, family=series.family, result_ids=result_ids
            )
            if change is not None:
                self._record(change, decision, meta, report)

    def _record(
        self,
        change: EpisodeChange,
        decision: AlertDecision,
        meta: EpisodeMeta,
        report: DetectionReport,
    ) -> None:
        report.episodes_updated += 1
        if change.notification:
            report.notifications += 1
        if change.kind in (
            "created",
            "escalated",
            "reopened",
            "resolved",
            "rejected",
            "downgraded",
        ):
            report.changes.append(
                {
                    "kind": change.kind,
                    "event_id": change.episode.id,
                    "query": meta.query,
                    "category": meta.category.value,
                    "classification": meta.classification.value,
                    "stage": change.status.value if change.status else None,
                    "confidence": change.episode.confidence.value,
                    "severity": round(change.severity, 1),
                    "notification": change.notification,
                    "triggered": [d.value for d in decision.triggered],
                    "reason": change.reason[:200],
                    "synthetic": meta.is_synthetic,
                }
            )

    def _result_rows(
        self,
        ev: PointEvaluation,
        decision: AlertDecision,
        run_id: uuid.UUID,
        now: datetime,
        synthetic: bool,
    ) -> list[dict[str, Any]]:
        if ev.observation is None:
            return []
        cfg = self.cfg
        base: dict[str, Any] = {
            "run_id": run_id,
            "target_id": ev.target_id,
            "series_id": ev.series_id,
            "observation_id": ev.observation.id,
            "revision": ev.observation.revision,
            "period_start": ev.period_start,
            "period_end": ev.period_end,
            "baseline_model": ev.baseline.model.value,
            "baseline_value": ev.baseline.expected,
            "baseline_dispersion": ev.baseline.dispersion,
            "observed": ev.value,
            "quality": ev.quality,
            "support": decision.confidence.value,
            "available_at": ev.available_at or now,
            "evaluated_at": now,
            "is_late": ev.is_late,
            "algorithm_version": cfg.algorithm_version,
            "configuration_version": self.loaded.version_tag,
            "is_synthetic": synthetic,
            "expires_at": ev.observation.expires_at,
        }
        a, c, p = ev.anomaly, ev.cusum, ev.change_point
        if ev.is_late:
            # Historical/late values never open alerts: keep the numbers (score, baseline,
            # threshold, status) but not the full decision trace, to avoid storing a large
            # derived copy of the whole history on the first run.
            return [
                result_row(
                    base,
                    detector=det,
                    score=score,
                    threshold=threshold,
                    p_value=None,
                    status=status,
                    reasons=reasons,
                    details={"late": True},
                )
                for det, score, threshold, status, reasons in (
                    (
                        DetectorType.ROBUST_ANOMALY,
                        a.z,
                        a.alert_threshold,
                        a.status.value,
                        list(a.reasons),
                    ),
                    (DetectorType.CUSUM, c.statistic, c.threshold, c.status.value, list(c.reasons)),
                    (
                        DetectorType.CHANGE_POINT,
                        p.probability,
                        cfg.change_point.probability_threshold,
                        p.status.value,
                        list(p.reasons),
                    ),
                )
            ]
        rows = [
            result_row(
                base,
                detector=DetectorType.ROBUST_ANOMALY,
                score=a.z,
                threshold=a.alert_threshold,
                p_value=a.p_value if a.p_value_exact else None,
                status=a.status.value,
                reasons=list(a.reasons),
                details={
                    **a.as_dict(),
                    "baseline": ev.baseline.as_dict(),
                    "decision": decision.as_dict(),
                },
            ),
            result_row(
                base,
                detector=DetectorType.CUSUM,
                score=c.statistic,
                threshold=c.threshold,
                p_value=ev.cusum_p_value
                if ev.cusum_p_value is not None and ev.cusum_p_value < 1
                else None,
                status=c.status.value,
                reasons=list(c.reasons),
                details=c.as_dict(),
            ),
            result_row(
                base,
                detector=DetectorType.CHANGE_POINT,
                score=p.probability,
                threshold=cfg.change_point.probability_threshold,
                p_value=None,
                status=p.status.value,
                reasons=list(p.reasons),
                details=p.as_dict(),
            ),
        ]
        if ev.cold_start is not None:
            rows.append(
                result_row(
                    base,
                    detector=DetectorType.COLD_START,
                    score=ev.cold_start.growth_ratio,
                    threshold=cfg.cold_start.growth_ratio,
                    p_value=None,
                    status="watch" if ev.cold_start.stage else "not_triggered",
                    reasons=list(ev.cold_start.reasons),
                    details=ev.cold_start.as_dict(),
                )
            )
        return rows

    def _resolve_stale(self, now: datetime, include: bool, run_id: uuid.UUID) -> int:
        with transaction(self._factory) as session:
            store = SqlEpisodeStore(session, run_id)
            manager = EpisodeManager(
                store,
                self.cfg,
                config_version=self.loaded.version_tag,
                budget=AlertBudget(limit_per_day=0),
            )

            def step_of(ep: Any) -> timedelta:
                return timedelta(seconds=int(ep.counters.get("step_seconds", 86_400)))

            closed = [
                ep for ep in manager.resolve_stale(now, step_of) if include or not ep.is_synthetic
            ]
            return len(closed)

    # ------------------------------------------------------------------ read side
    def feed(
        self,
        *,
        active: bool = True,
        category: Category | None = None,
        include_synthetic: bool = False,
        limit: int = 500,
    ) -> list[SurgeAlert]:
        """Typed alerts for Phase 5 (validated against the published contract)."""
        with transaction(self._factory) as session:
            rows = SurgeEventRepository(session).events(
                active=active, category=category, include_synthetic=include_synthetic, limit=limit
            )
            out = []
            for r in rows:
                if (
                    r.payload
                    and "schema_version" in r.payload
                    and "contract_error" not in r.payload
                ):
                    out.append(SurgeAlert.model_validate(r.payload))
            return out

    def target_views(
        self, *, category: Category | None = None, include_synthetic: bool = False
    ) -> list[TargetSurgeView]:
        """Episodes grouped per target with the combined (target-level) classification."""
        from trend_agent.schemas.evidence import STAGE_RANK, combine

        alerts = self.feed(active=True, category=category, include_synthetic=include_synthetic)
        grouped: dict[int, list[SurgeAlert]] = {}
        for a in alerts:
            grouped.setdefault(a.target_id, []).append(a)
        views = []
        for tid, items in sorted(grouped.items()):
            cats = tuple(sorted({a.measurement_type for a in items}, key=lambda m: m.value))
            combined = combine(cats)
            ranked = [a.alert_status for a in items if a.alert_status in STAGE_RANK]
            top = max(ranked, key=lambda s: STAGE_RANK[s]) if ranked else None
            if combined is SurgeClassification.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY:
                statement = (
                    "Several indirect signals rose together. This is a potential YouTube "
                    "demand opportunity, not measured YouTube search growth."
                )
            elif combined is SurgeClassification.DIRECT_SEARCH_SURGE:
                statement = "Direct YouTube search-interest evidence is present."
            else:
                statement = "A single evidence category shows an increase."
            views.append(
                TargetSurgeView(
                    target_id=tid,
                    query=items[0].query,
                    category=items[0].category,
                    topic_cluster_id=items[0].topic_cluster_id,
                    combined_classification=combined,
                    highest_stage=top,
                    evidence_categories=cats,
                    statement=statement,
                    alerts=tuple(items),
                    synthetic=any(a.synthetic for a in items),
                )
            )
        return views

    def episode(self, event_id: int) -> SurgeEventRow | None:
        with transaction(self._factory) as session:
            return SurgeEventRepository(session).get(event_id)


def _granularity(value: str) -> Any:
    from trend_agent.schemas.enums import TimeGranularity

    return TimeGranularity(value)


def _tier(value: str) -> Any:
    from trend_agent.schemas.monitoring import MonitoringTier

    return MonitoringTier(value)
