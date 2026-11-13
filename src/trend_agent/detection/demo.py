"""End-to-end SYNTHETIC demonstration of Phase 4 (scenarios A-N), with self-checks.

Everything here is synthetic: fictional subjects, generated values, a simulated clock.

Flow (uses the real Phase 3 storage path and the live detection service):

1. reset earlier Phase 4 demo data (only this demo's providers / targets);
2. **calibrate** on a separate training split of synthetic null series (disjoint seeds) and
   store the calibration (source = synthetic, applied to synthetic series only);
3. create monitoring targets and write observations through
   ``TimeSeriesRepository`` + the detection outbox exactly as the Phase 3 executor does,
   each value becoming available 36 h after its day ends (revisions and a late delivery
   included);
4. advance the simulated clock day by day and run detection from the queue;
5. check every scenario's expected outcome, the output contract, idempotent reprocessing,
   deterministic replay without look-ahead, and the large-scale null false-alert rate.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from trend_agent.container import AppContainer
from trend_agent.db.detection_models import (
    AnomalyResultRow,
    DetectionCalibrationRow,
    DetectionQueueRow,
    DetectionRunRow,
    SurgeEventRow,
)
from trend_agent.db.monitoring_models import MonitoringTargetRow, ObservationRow, SeriesRow
from trend_agent.db.repositories.core import QueryRepository
from trend_agent.db.repositories.detection_runs import (
    DetectionQueueRepository,
    DetectionRunRepository,
)
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import transaction
from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import SeriesEngine
from trend_agent.detection.evaluation import build_world, evaluate_world
from trend_agent.detection.preprocessing import knowledge_at, prepare
from trend_agent.detection.replay_adapter import DetectionReplayer
from trend_agent.detection.service import DetectionService, load_nulls
from trend_agent.detection.synthetic import (
    InjectedEvent,
    SyntheticSeriesSpec,
    benchmark_specs,
    generate,
    null_specs,
)
from trend_agent.discovery.models import Category
from trend_agent.monitoring.replay import replay
from trend_agent.schemas.detection import DetectorType, PreprocessStatus, RunType
from trend_agent.schemas.enums import (
    DataNature,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.evidence import AlertStage, SurgeClassification, classify
from trend_agent.schemas.monitoring import (
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
    search_property_for,
)
from trend_agent.schemas.observation import NormalizationContext
from trend_agent.schemas.scheduling import RevisionBehaviour
from trend_agent.schemas.surge import SurgeAlert
from trend_agent.schemas.time_series import ObservationDraft
from trend_agent.utils.text import canonicalize_query

M = MeasurementCategory
DIRECT = "synthetic_detect_trends"
WEB = "synthetic_detect_web"
VIDEO = "synthetic_detect_video"
PROVIDERS = (DIRECT, WEB, VIDEO)
START = datetime(2026, 7, 6, tzinfo=UTC)  # a Monday
DAY = timedelta(days=1)
LAG = timedelta(hours=36)
DAYS = 112
BACKFILL_UNTIL = 80  # first detection run at day 80 12:00 (history up to day 78)
POLICY = "synthetic-test-data@1"
SEED = 4404


class StageClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class DemoSeries:
    scenario: str
    query: str
    category: Category
    measurement: MeasurementCategory
    provider: str
    spec: SyntheticSeriesSpec
    first_day: int = 0
    novelty: str | None = None
    scale_change_day: int | None = None
    scale_factor: float = 1.0
    revision: tuple[int, float, int] | None = None  # (day, revised value, revised on day)
    late: tuple[int, int] | None = None  # (day, delivered on day)

    @property
    def is_proxy(self) -> bool:
        return self.measurement not in (
            M.DIRECT_YOUTUBE_SEARCH_INTEREST,
            M.EXTERNAL_SEARCH_INTEREST,
        )


def _spec(key: str, **kw: Any) -> SyntheticSeriesSpec:
    kw.setdefault("provider", DIRECT)
    return SyntheticSeriesSpec(key=key, seed=SEED, days=DAYS, start=START, **kw)


def scenario_series() -> list[DemoSeries]:
    d, e = M.DIRECT_YOUTUBE_SEARCH_INTEREST, M.EXTERNAL_SEARCH_INTEREST
    out = [
        DemoSeries("A", "nimbus coder 3 tutorial", Category.AI, d, DIRECT,
                   _spec("A", base=18, noise=0.08, events=(InjectedEvent("step", 88, 42.0),))),
        DemoSeries("B", "ai agent framework comparison", Category.AI, d, DIRECT,
                   _spec("B", base=25, noise=0.07, events=(InjectedEvent("ramp", 82, 40.0, 25),))),
        DemoSeries("C", "fortnite starfall rift event", Category.GAMING, d, DIRECT,
                   _spec("C", base=22, noise=0.08, events=(InjectedEvent("event", 92, 55.0, 3),))),
        DemoSeries("D", "python async tutorial", Category.TECHNOLOGY, d, DIRECT,
                   _spec("D", base=30, noise=0.05, weekly=0.6)),
        DemoSeries("F", "lumen 7 local install", Category.AI, d, DIRECT,
                   _spec("F", base=5, noise=0.05, events=(InjectedEvent("ramp", 104, 20.0, 6),)),
                   first_day=105, novelty="new_entity"),
        DemoSeries("G", "lattice 6 upgrade guide", Category.TECHNOLOGY, d, DIRECT,
                   _spec("G", base=30, noise=0.06), scale_change_day=95, scale_factor=1.8),
        DemoSeries("H", "aurora 2 benchmark", Category.AI, e, WEB,
                   _spec("H", base=20, noise=0.08, measurement=e, provider=WEB,
                         events=(InjectedEvent("step", 95, 30.0),))),
        DemoSeries("I", "dragonfall season 3 trailer", Category.GAMING, M.VIDEO_ACTIVITY, VIDEO,
                   _spec("I", base=12, noise=0.15, measurement=M.VIDEO_ACTIVITY, provider=VIDEO,
                         scale=ScaleType.ABSOLUTE_COUNT,
                         events=(InjectedEvent("event", 96, 40.0, 3),))),
        DemoSeries("J", "quantum sdk release notes", Category.TECHNOLOGY, d, DIRECT,
                   _spec("J", base=25, noise=0.06, events=(InjectedEvent("spike", 100, 45.0, 1),)),
                   revision=(100, 26.0, 103)),
        DemoSeries("K", "gpu driver rollback", Category.TECHNOLOGY, d, DIRECT,
                   _spec("K", base=28, noise=0.07, missing_rate=0.3)),
        DemoSeries("N", "vector db pricing", Category.TECHNOLOGY, d, DIRECT,
                   _spec("N", base=26, noise=0.07), late=(98, 102)),
    ]  # fmt: skip
    cats = [Category.AI, Category.TECHNOLOGY, Category.GAMING]
    for k in range(12):
        out.append(
            DemoSeries(
                "E",
                f"steady interest topic {k + 1}",
                cats[k % 3],
                d,
                DIRECT,
                _spec(
                    f"E{k}", base=10 + 6 * k, noise=0.06 + 0.005 * k, ar=0.3, weekly=0.3 * (k % 2)
                ),
            )
        )
    return out


@dataclass
class Check:
    scenario: str
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "check": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class DemoReport:
    days: int
    runs: int
    checks: list[Check] = field(default_factory=list)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    calibration: dict[str, Any] = field(default_factory=dict)
    null_scale: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    run_totals: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "synthetic": True,
            "label": "SYNTHETIC DEMO - fictional subjects, simulated clock, not real data",
            "days": self.days,
            "runs": self.runs,
            "all_checks_passed": self.passed,
            "checks": [c.as_dict() for c in self.checks],
            "episodes": self.episodes,
            "calibration": self.calibration,
            "null_scale": self.null_scale,
            "replay": self.replay,
            "run_totals": self.run_totals,
            "seconds": round(self.seconds, 1),
        }


# ----------------------------------------------------------------------------- setup
def reset(container: AppContainer) -> None:
    """Remove earlier Phase 4 demo data only (its providers' series and targets)."""
    with transaction(container.session_factory) as session:
        targets = sa.select(MonitoringTargetRow.id).where(
            MonitoringTargetRow.is_synthetic.is_(True),
            MonitoringTargetRow.preferred_provider.in_(PROVIDERS),
        )
        session.execute(sa.delete(SurgeEventRow).where(SurgeEventRow.target_id.in_(targets)))
        series = sa.select(SeriesRow.id).where(SeriesRow.provider_name.in_(PROVIDERS))
        session.execute(sa.delete(DetectionQueueRow).where(DetectionQueueRow.series_id.in_(series)))
        session.execute(
            sa.delete(ObservationRow).where(ObservationRow.provider_name.in_(PROVIDERS))
        )
        session.execute(sa.delete(SeriesRow).where(SeriesRow.provider_name.in_(PROVIDERS)))
        session.execute(sa.delete(MonitoringTargetRow).where(MonitoringTargetRow.id.in_(targets)))
        session.execute(
            sa.delete(DetectionRunRow).where(
                DetectionRunRow.run_type == RunType.DEMO, DetectionRunRow.is_synthetic.is_(True)
            )
        )
        session.execute(
            sa.delete(DetectionCalibrationRow).where(DetectionCalibrationRow.dataset.like("demo-%"))
        )


def ensure_targets(
    container: AppContainer, items: list[DemoSeries], now: datetime
) -> dict[str, int]:
    out: dict[str, int] = {}
    with transaction(container.session_factory) as session:
        queries = QueryRepository(session)
        for s in items:
            canonical = canonicalize_query(s.query)
            qid = queries.get_or_create(canonical, s.query, "en", None)
            row = MonitoringTargetRow(
                query_id=qid,
                category=s.category,
                region="US",
                language="en",
                measurement_category=s.measurement,
                is_proxy=s.is_proxy,
                preferred_provider=s.provider,
                provider_in_use=s.provider,
                fallback_providers=[],
                tier=MonitoringTier.NORMAL,
                status=MonitoringStatus.ACTIVE,
                status_reason="synthetic detection demo",
                status_changed_at=now,
                priority_score=70.0,
                novelty_kind=s.novelty,
                config_version="detection-demo",
                is_synthetic=True,
                manual_override={"fixture": "detection_demo", "scenario": s.scenario},
            )
            session.add(row)
            session.flush()
            out[s.spec.key] = row.id
    return out


def _draft(
    s: DemoSeries,
    target_id: int,
    query_id: int,
    day: int,
    value: float | None,
    collected: datetime,
    published: datetime | None = None,
) -> ObservationDraft:
    start = START + DAY * day
    scale = s.spec.scale
    version = "2" if s.scale_change_day is not None and day >= s.scale_change_day else "1"
    published = published or start + DAY + LAG
    return ObservationDraft(
        query_id=query_id,
        target_id=target_id,
        provider_name=s.provider,
        provider_version="synthetic-1",
        measurement_category=M.SYNTHETIC_TEST_OBSERVATION,
        simulated_category=s.measurement,
        is_proxy=s.is_proxy,
        unit="items" if scale is ScaleType.ABSOLUTE_COUNT else "index",
        period_start=start,
        period_end=start + DAY,
        granularity=TimeGranularity.DAY,
        provider_published_at=min(published, collected),
        collected_at=collected,
        value=value,
        scale_type=scale,
        normalization_context=NormalizationContext(
            scale_type=scale,
            method="synthetic_detection_demo",
            scale_version=version,
            granularity=TimeGranularity.DAY,
            search_property=search_property_for(s.measurement),
        ),
        region="US",
        language="en",
        search_property=search_property_for(s.measurement)
        if not s.is_proxy
        else SearchProperty.NOT_APPLICABLE,
        data_nature=DataNature.SYNTHETIC,
        sample_quality=SampleQuality.COMPLETE if value is not None else SampleQuality.MISSING,
        source_reference=f"synthetic://detection-demo/{s.spec.key}/{start.date()}",
        data_policy_id=POLICY,
    )


def _value(s: DemoSeries, values: list[float | None], day: int) -> float | None:
    v = values[day]
    if v is None:
        return None
    if s.scale_change_day is not None and day >= s.scale_change_day:
        return float(min(100.0, round(v * s.scale_factor)))
    return v


def write_available(
    container: AppContainer,
    items: list[DemoSeries],
    generated: dict[str, list[float | None]],
    targets: dict[str, int],
    query_ids: dict[int, int],
    previous: datetime | None,
    now: datetime,
) -> int:
    """Write every observation (or revision) that became available in (previous, now]."""
    written = 0
    with transaction(container.session_factory) as session:
        ts = TimeSeriesRepository(session)
        marks: list[tuple[int, int | None, datetime | None]] = []
        for s in items:
            tid = targets[s.spec.key]
            values = generated[s.spec.key]
            for day in range(s.first_day, DAYS):
                available = START + DAY * (day + 1) + LAG
                if s.late is not None and day == s.late[0]:
                    available = START + DAY * s.late[1] + timedelta(hours=12)
                if available > now or (previous is not None and available <= previous):
                    continue
                draft = _draft(s, tid, query_ids[tid], day, _value(s, values, day), now)
                result = ts.write(
                    draft, revision_behaviour=RevisionBehaviour.REVISABLE, ingested_at=now
                )
                marks.append((result.series_id, tid, draft.period_start))
                written += 1
            if s.revision is not None:
                day, revised, on = s.revision
                when = START + DAY * on + timedelta(hours=12)
                if when <= now and (previous is None or when > previous):
                    # A revision is published when it is revised, not with the original.
                    draft = _draft(s, tid, query_ids[tid], day, revised, now, published=when)
                    result = ts.write(
                        draft, revision_behaviour=RevisionBehaviour.REVISABLE, ingested_at=now
                    )
                    marks.append((result.series_id, tid, draft.period_start))
                    written += 1
        DetectionQueueRepository(session).mark(marks, now, channel=None)
    return written


# ----------------------------------------------------------------------------- demo
def run_demo(
    container: AppContainer, *, reset_first: bool = True, null_series: int = 1000
) -> DemoReport:
    started = time.perf_counter()
    loaded: LoadedDetectionConfig = container.detection_config()
    items = scenario_series()
    report = DemoReport(days=DAYS, runs=0)
    if reset_first:
        reset(container)
    clock = StageClock(START + DAY * BACKFILL_UNTIL + timedelta(hours=12))
    # The monitoring service registers the data policies the observations reference.
    container.monitoring(clock=clock, include_synthetic=True).ensure_policies()

    # 1) calibration on a disjoint synthetic training split (same provider stratum)
    from trend_agent.detection.calibration import calibrate

    train_specs = [
        SyntheticSeriesSpec(**{**s.__dict__, "provider": DIRECT})
        for s in benchmark_specs("train", 240, days=150)
        if not s.events
    ]
    cal = calibrate(
        build_world(train_specs).points,
        loaded,
        version=f"demo-{loaded.config.threshold_version}",
        source="synthetic",
        dataset=f"demo-train-null-{len(train_specs)}x150d",
        now=clock(),
    )
    with transaction(container.session_factory) as session:
        runs = DetectionRunRepository(session)
        for key, params in cal.strata.items():
            runs.save_calibration(
                version=cal.version,
                stratum=key,
                source=cal.source,
                dataset=cal.dataset,
                method="empirical_tail+gpd",
                parameters=params,
                train_start=cal.train_start,
                train_end=cal.train_end,
            )
    report.calibration = {
        "version": cal.version,
        "dataset": cal.dataset,
        "strata": list(cal.strata),
        "recommended_h": cal.recommended_h,
        "configured": {"k": loaded.config.cusum.k, "h": loaded.config.cusum.h},
    }

    # 2) targets and synthetic values
    targets = ensure_targets(container, items, clock())
    generated = {s.spec.key: generate(s.spec).values for s in items}
    with transaction(container.session_factory) as session:
        query_ids = {
            r.id: r.query_id
            for r in session.execute(
                sa.select(MonitoringTargetRow).where(
                    MonitoringTargetRow.id.in_(list(targets.values()))
                )
            ).scalars()
        }
    service = DetectionService(
        session_factory=container.session_factory,
        loaded=loaded,
        monitoring=None,
        clock=clock,
        include_synthetic=True,
    )
    totals: Counter[str] = Counter()
    first_triggers: dict[int, list[str]] = {}
    previous: datetime | None = None
    for day in range(BACKFILL_UNTIL, DAYS + 3):
        clock.now = START + DAY * day + timedelta(hours=12)
        write_available(container, items, generated, targets, query_ids, previous, clock.now)
        previous = clock.now
        result = service.run(run_type=RunType.DEMO, from_queue=True, worker="demo")
        report.runs += 1
        totals["observations_evaluated"] += result.observations_evaluated
        totals["notifications"] += result.notifications
        totals["hypotheses"] += result.hypotheses
        totals["accepted"] += result.accepted
        for reason, n in result.recomputations.items():
            totals[f"recompute:{reason}"] += n
        for ch in result.changes:
            if ch["notification"] and ch["event_id"] not in first_triggers:
                first_triggers[ch["event_id"]] = ch.get("triggered", [])
    report.run_totals = dict(totals)
    _checks(container, service, report, items, targets, generated, first_triggers, loaded, clock)
    # Scenario L: thousands of null queries (memory; same calibration and pipeline rules)
    l_specs = [
        SyntheticSeriesSpec(**{**s.__dict__, "provider": DIRECT})
        for s in null_specs(null_series, days=120)
    ]
    with transaction(container.session_factory) as session:
        nulls = load_nulls(session)
    m = evaluate_world(
        build_world(l_specs),
        loaded,
        nulls,
        dataset=f"synthetic-null-{null_series}x120d",
        split="null",
        eval_start=60,
    )
    report.null_scale = {
        "series": null_series,
        "days": 60,
        "correlated_share": 0.5,
        "false_alerts": m.fp,
        "false_alerts_per_day": m.false_alerts_per_day,
        "false_alerts_per_1000_series_day": m.false_alerts_per_1000_series_day,
        "target_per_day": loaded.config.multiple_testing.target_false_alerts_per_day,
    }
    target = loaded.config.multiple_testing.target_false_alerts_per_day
    report.checks.append(
        Check(
            "L",
            "false alerts within budget across 1,000 null queries",
            (m.false_alerts_per_1000_series_day or 0.0) <= target,
            f"{m.fp} false alerts in {null_series} series x 60 days = "
            f"{m.false_alerts_per_1000_series_day} per 1,000 series per day (target <= {target})",
        )
    )
    report.seconds = time.perf_counter() - started
    return report


def _episodes(container: AppContainer, target_ids: list[int]) -> list[SurgeEventRow]:
    with transaction(container.session_factory) as session:
        return SurgeEventRepository(session).events(
            include_synthetic=True, target_ids=target_ids, limit=1000
        )


def _checks(
    container: AppContainer,
    service: DetectionService,
    report: DemoReport,
    items: list[DemoSeries],
    targets: dict[str, int],
    generated: dict[str, list[float | None]],
    first_triggers: dict[int, list[str]],
    loaded: LoadedDetectionConfig,
    clock: StageClock,
) -> None:
    def check(scenario: str, name: str, passed: bool, detail: str) -> None:
        report.checks.append(Check(scenario, name, bool(passed), detail))

    by_key = {s.spec.key: s for s in items}
    events = _episodes(container, list(targets.values()))
    eps: dict[str, list[SurgeEventRow]] = {k: [] for k in targets}
    tid_key = {v: k for k, v in targets.items()}
    for e in events:
        eps[tid_key[e.target_id]].append(e)

    def notified(rows: list[SurgeEventRow]) -> list[SurgeEventRow]:
        return [e for e in rows if e.notifications > 0]

    for e in events:
        s = by_key[tid_key[e.target_id]]
        report.episodes.append(
            {
                "scenario": s.scenario,
                "query": s.query,
                "category": s.category.value,
                "event_id": e.id,
                "classification": e.evidence_classification.value,
                "status": e.status.value,
                "confidence": e.confidence_status.value,
                "severity": round(e.severity, 1),
                "max_severity": round(e.maximum_severity, 1),
                "episode_start": e.episode_start.date().isoformat(),
                "first_detected_at": e.first_detected_at.isoformat(),
                "notifications": e.notifications,
                "first_triggers": first_triggers.get(e.id, []),
                "explanation": e.explanation,
            }
        )

    # A - sudden AI model breakout
    a = notified(eps["A"])
    check("A", "breakout detected", len(a) >= 1, f"{len(a)} alerting episode(s)")
    if a:
        first = a[0]
        check(
            "A",
            "rapid anomaly detector triggered",
            "robust_anomaly" in first_triggers.get(first.id, []),
            f"first triggers {first_triggers.get(first.id)}",
        )
        check(
            "A",
            "severity recorded",
            first.maximum_severity > 0,
            f"max severity {first.maximum_severity:.1f}",
        )
        check(
            "A",
            "statistically supported confirmation",
            first.status is AlertStage.CONFIRMED
            or any(t == "confirmed" for t in first.counters.get("notified", [])),
            f"status {first.status.value}, confidence {first.confidence_status.value}",
        )
    check(
        "A", "no duplicate episodes", len(a) == 1, f"{len(a)} alerting episode(s) for one breakout"
    )
    # B - gradual growth: sustained detectors
    b = notified(eps["B"])
    check("B", "sustained growth detected", len(b) >= 1, f"{len(b)} alerting episode(s)")
    triggers_b = first_triggers.get(b[0].id, []) if b else []
    check(
        "B",
        "detected by a sustained-change detector (not one extreme value)",
        bool({"cusum", "change_point"} & set(triggers_b)),
        f"detectors at first alert: {triggers_b}",
    )
    # C - limited-time gaming event
    c = notified(eps["C"])
    check("C", "event detected", len(c) >= 1, f"{len(c)} alerting episode(s)")
    check(
        "C",
        "episode resolved after the event",
        bool(c) and all(e.status in (AlertStage.RESOLVED,) for e in c),
        f"statuses {[e.status.value for e in c]}",
    )
    # D - weekly seasonality
    d_tid = targets["D"]
    with transaction(container.session_factory) as session:
        latest = session.execute(
            sa.select(AnomalyResultRow.baseline_model)
            .where(
                AnomalyResultRow.target_id == d_tid,
                AnomalyResultRow.detector_type == DetectorType.ROBUST_ANOMALY,
            )
            .order_by(AnomalyResultRow.period_start.desc())
            .limit(1)
        ).scalar_one_or_none()
        d_points = TimeSeriesRepository(session).points(target_id=d_tid, limit=100_000)
    check(
        "D",
        "seasonal baseline selected",
        latest is not None and latest.value == "seasonal_profile",
        f"latest baseline model: {latest.value if latest else None}",
    )
    window_start = START + DAY * BACKFILL_UNTIL
    seasonal = _count_triggers(d_points, loaded, window_start, seasonal=True)
    robust = _count_triggers(d_points, loaded, window_start, seasonal=False)
    check(
        "D",
        "seasonal baseline reduces predictable false alarms",
        seasonal < robust and not notified(eps["D"]),
        f"anomaly triggers in the final {DAYS - BACKFILL_UNTIL} days: {seasonal} with the "
        f"seasonal profile vs {robust} with a robust-only baseline; alerting episodes: "
        f"{len(notified(eps['D']))}",
    )
    # E - noise only
    e_keys = [k for k in targets if k.startswith("E")]
    e_alerts = sum(len(notified(eps[k])) for k in e_keys)
    check(
        "E",
        "noise produces no (or very few) alerts",
        e_alerts <= 1,
        f"{e_alerts} alerting episode(s) across {len(e_keys)} noise series over "
        f"{DAYS - BACKFILL_UNTIL} days",
    )
    # F - sparse new keyword (cold start)
    with transaction(container.session_factory) as session:
        f_results = (
            session.execute(
                sa.select(AnomalyResultRow)
                .where(
                    AnomalyResultRow.target_id == targets["F"],
                    AnomalyResultRow.detector_type == DetectorType.COLD_START,
                )
                .order_by(AnomalyResultRow.period_start)
            )
            .scalars()
            .all()
        )
    states = [r.details.get("state") for r in f_results]
    f_eps = eps["F"]
    check(
        "F",
        "cold-start status reported",
        bool(states) and all(s != "baseline_established" for s in states),
        f"cold-start states: {states}",
    )
    check(
        "F",
        "no fabricated statistical confidence",
        all(e.status is not AlertStage.CONFIRMED for e in f_eps)
        and all(e.confidence_status.value == "low_confidence_cold_start" for e in f_eps),
        f"episodes: {[(e.status.value, e.confidence_status.value) for e in f_eps]}",
    )
    # G - normalisation change
    with transaction(container.session_factory) as session:
        g_series = TimeSeriesRepository(session).series_summaries(target_ids=[targets["G"]])
        g_points = TimeSeriesRepository(session).points(target_id=targets["G"], limit=100_000)
    mixed = prepare(g_points, loaded.config, already_known=True)
    check(
        "G",
        "new normalisation context stored as a separate series",
        len(g_series) == 2,
        f"{len(g_series)} series",
    )
    check(
        "G",
        "cross-context calculation rejected",
        mixed.status is PreprocessStatus.INCOMPATIBLE
        and "normalization_context_changed" in mixed.reasons,
        f"status {mixed.status.value}; reasons {mixed.reasons[:3]}",
    )
    check(
        "G",
        "rescaling did not create an alert",
        not notified(eps["G"]),
        f"{len(notified(eps['G']))} alerting episode(s)",
    )
    # H - external search surge
    h = notified(eps["H"])
    check(
        "H",
        "external search surge classified as such",
        bool(h)
        and all(e.evidence_classification is SurgeClassification.EXTERNAL_SEARCH_SURGE for e in h),
        f"{[(e.evidence_classification.value, e.status.value) for e in h]}",
    )
    check(
        "H",
        "no confirmed YouTube search surge",
        all(e.status is not AlertStage.CONFIRMED for e in eps["H"]),
        "external evidence is capped at CANDIDATE",
    )
    # I - video popularity spike
    i = notified(eps["I"])
    check(
        "I",
        "video activity is a proxy attention candidate only",
        bool(i)
        and all(
            e.evidence_classification is SurgeClassification.PROXY_ATTENTION_SIGNAL
            and e.status is not AlertStage.CONFIRMED
            for e in eps["I"]
        )
        and all("not a search-demand surge" in e.explanation for e in i),
        f"{[(e.evidence_classification.value, e.status.value) for e in eps['I']]}",
    )
    # J - data revision
    j = eps["J"]
    with transaction(container.session_factory) as session:
        revs = session.execute(
            sa.select(ObservationRow.revision, ObservationRow.value, ObservationRow.is_current)
            .where(
                ObservationRow.target_id == targets["J"],
                ObservationRow.period_start == START + DAY * 100,
            )
            .order_by(ObservationRow.revision)
        ).all()
        j_points = TimeSeriesRepository(session).points(
            target_id=targets["J"], current_only=False, limit=100_000
        )
    check(
        "J",
        "revision lineage preserved",
        len(revs) == 2 and not revs[0][2] and revs[1][2],
        f"revisions for day 100: {[(r[0], r[1], r[2]) for r in revs]}",
    )
    check(
        "J",
        "alert on the published spike rejected after the revision",
        any(e.status is AlertStage.REJECTED for e in j),
        f"episodes: {[(e.status.value, e.counters.get('support_periods')) for e in j]}",
    )
    replay_info = _replay_check(j_points, targets["J"], by_key["J"], loaded, check)
    report.replay = replay_info
    # K - missing periods
    with transaction(container.session_factory) as session:
        k_missing = session.execute(
            sa.select(sa.func.count())
            .select_from(ObservationRow)
            .where(ObservationRow.target_id == targets["K"], ObservationRow.value.is_(None))
        ).scalar_one()
        k_zero = session.execute(
            sa.select(sa.func.count())
            .select_from(ObservationRow)
            .where(ObservationRow.target_id == targets["K"], ObservationRow.value == 0)
        ).scalar_one()
    expected_missing = sum(1 for v in generated["K"] if v is None)
    check(
        "K",
        "missing values stored as missing, never zero",
        k_missing == expected_missing and k_zero == 0,
        f"{k_missing} missing values (generated {expected_missing}), {k_zero} zeros",
    )
    check(
        "K",
        "gaps did not create alerts",
        not notified(eps["K"]),
        f"{len(notified(eps['K']))} alerting episode(s)",
    )
    # M - repeated processing
    _idempotency_checks(container, service, targets, check)
    # N - out-of-order delivery
    rec = report.run_totals.get("recompute:late_arrival", 0)
    with transaction(container.session_factory) as session:
        n_points = TimeSeriesRepository(session).points(
            target_id=targets["N"], current_only=False, limit=100_000
        )
        from trend_agent.db.repositories.detector_state import DetectorStateRepository
        from trend_agent.detection.engine import EngineState

        n_series = TimeSeriesRepository(session).series_summaries(target_ids=[targets["N"]])[0]
        stored = DetectorStateRepository(session).load(n_series.series_id)
        nulls = load_nulls(session)
    assert stored is not None
    live_state = EngineState.from_payloads(stored.payloads)
    in_order = (
        SeriesEngine(loaded, nulls)
        .run(prepare(knowledge_at(n_points, None), loaded.config, already_known=True), None)
        .state
    )
    check(
        "N",
        "late arrival triggered deterministic recomputation",
        rec >= 1,
        f"{rec} recomputation(s) for late arrivals",
    )
    check(
        "N",
        "state equals in-order processing (no silent corruption)",
        live_state.cusum.as_dict() == in_order.cusum.as_dict()
        and live_state.processed_until_slot == in_order.processed_until_slot
        and (live_state.bocpd is not None and in_order.bocpd is not None)
        and live_state.bocpd.as_dict() == in_order.bocpd.as_dict(),
        f"CUSUM {round(live_state.cusum.g, 4)} vs {round(in_order.cusum.g, 4)}",
    )
    # Output contract and production isolation
    payloads = [e.payload for e in events if e.payload and "contract_error" not in e.payload]
    valid = 0
    for pl in payloads:
        SurgeAlert.model_validate(pl)
        valid += 1
    check(
        "contract",
        "every episode carries a valid, versioned SurgeAlert",
        valid == len(events) and valid > 0,
        f"{valid}/{len(events)} payloads validate as surge-alert/1.0",
    )
    check(
        "contract",
        "synthetic alerts labelled and excluded from production listings",
        all(e.is_synthetic and e.explanation.startswith("[SYNTHETIC]") for e in events)
        and not service.feed(active=False, include_synthetic=False),
        "production feed (include_synthetic=False) is empty",
    )


def _count_triggers(
    points: list[Any], loaded: LoadedDetectionConfig, since: datetime, *, seasonal: bool
) -> int:
    cfg = loaded.config
    if not seasonal:
        baseline = cfg.baseline.model_copy(
            update={"seasonal": cfg.baseline.seasonal.model_copy(update={"enabled": False})}
        )
        cfg = cfg.model_copy(update={"baseline": baseline})
        loaded = LoadedDetectionConfig(path=None, config=cfg, digest="robust-only")
    series = prepare(points, cfg, already_known=True)
    out = SeriesEngine(loaded).run(series, None)
    return sum(1 for ev in out.evaluations if ev.period_start >= since and ev.anomaly.triggered)


def _replay_check(
    points: list[Any],
    target_id: int,
    s: DemoSeries,
    loaded: LoadedDetectionConfig,
    check: Any,
) -> dict[str, Any]:
    series_ids = sorted({p.series_id for p in points})
    meta = EpisodeMeta(
        target_id=target_id,
        query=s.query,
        query_id=0,
        keyword_id=None,
        topic_cluster_id=None,
        category=s.category,
        region="US",
        language="en",
        provider=s.provider,
        measurement=s.measurement,
        is_proxy=s.is_proxy,
        classification=classify(s.measurement),
        is_synthetic=True,
        step=DAY,
    )
    start = START + DAY * BACKFILL_UNTIL
    end = START + DAY * (DAYS + 2)
    digests = []
    seen_before = seen_after = None
    for _ in range(2):
        replayer = DetectionReplayer(loaded, dict.fromkeys(series_ids, meta))
        replay(points, start=start, end=end, step=DAY, consumers=[replayer.consumer()])
        digests.append(replayer.run.digest())
        spike = START + DAY * 100
        before = START + DAY * 103 + timedelta(hours=12)  # revision published day 103 12:00
        stages = [
            (d.as_of, d.decision.stage, d.change.kind if d.change else None)
            for d in replayer.run.decisions
            if d.evaluation.period_start == spike
        ]
        seen_before = [st.value for at, st, _ in stages if at < before and st is not None]
        seen_after = [k for at, _, k in stages if at >= before]
    check(
        "J",
        "replay keeps what was known before the revision",
        bool(seen_before) and "rejected" in (seen_after or []),
        f"stages before the revision {seen_before}; changes after it {seen_after}",
    )
    check(
        "G/J",
        "deterministic replay",
        len(set(digests)) == 1,
        f"decision digest {digests[0]} on two independent replays",
    )
    return {"digest": digests[0], "stages_before_revision": seen_before, "after": seen_after}


def _idempotency_checks(
    container: AppContainer, service: DetectionService, targets: dict[str, int], check: Any
) -> None:
    def counts() -> tuple[int, int, int]:
        with transaction(container.session_factory) as session:
            tids = list(targets.values())
            results = session.execute(
                sa.select(sa.func.count())
                .select_from(AnomalyResultRow)
                .where(AnomalyResultRow.target_id.in_(tids))
            ).scalar_one()
            episodes = session.execute(
                sa.select(sa.func.count())
                .select_from(SurgeEventRow)
                .where(SurgeEventRow.target_id.in_(tids))
            ).scalar_one()
            notes = session.execute(
                sa.select(sa.func.sum(SurgeEventRow.notifications)).where(
                    SurgeEventRow.target_id.in_(tids)
                )
            ).scalar_one()
        return int(results), int(episodes), int(notes or 0)

    before = counts()
    again = service.run(run_type=RunType.DEMO, worker="demo-rerun")
    after = counts()
    check(
        "M",
        "re-running detection on unchanged data changes nothing",
        before == after and again.observations_evaluated == 0,
        f"results/episodes/notifications {before} -> {after}; skipped {dict(again.skipped)}",
    )
    # Lost detector state: everything is re-evaluated, nothing is duplicated.
    with transaction(container.session_factory) as session:
        from trend_agent.db.detection_models import DetectorStateRow

        session.execute(
            sa.delete(DetectorStateRow).where(
                DetectorStateRow.target_id.in_(list(targets.values()))
            )
        )
    redo = service.run(run_type=RunType.DEMO, worker="demo-reprocess")
    after_redo = counts()
    check(
        "M",
        "reprocessing every observation creates no duplicate result or episode",
        after_redo == before and redo.observations_evaluated > 0,
        f"{redo.observations_evaluated} observations re-evaluated; results/episodes/"
        f"notifications {before} -> {after_redo}",
    )
