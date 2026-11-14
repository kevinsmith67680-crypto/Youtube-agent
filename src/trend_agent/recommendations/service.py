"""Database-backed recommendation generation (Phase 7).

Inputs are read, never recomputed: Phase 5 opportunity reports (and their evidence
bundles), Phase 6 forecasts and the Phase 4 alerts behind them. The workflow is bounded:

* at most ``generation.max_opportunities_per_run`` opportunities per run;
* an opportunity whose inputs did not change since the stored recommendations were made
  (same input fingerprint, same profile, same generator and configuration) is not
  regenerated: the stored ideas are reused, no LLM call is made;
* LLM generation is optional, budgeted per day (``llm.daily_token_budget``) and has bounded
  attempts; a failing or over-budget LLM falls back to the deterministic template generator,
  which is recorded on the run;
* ranking, deduplication and diversity run across every current recommendation (real and
  synthetic ranked separately; they never mix), and the result is stored idempotently.

Alerts are detected from the newly stored recommendations and dispatched in-app (and to
optional email/Telegram channels when configured).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from trend_agent.config import Settings
from trend_agent.db.content_models import ContentAnalysisRunRow
from trend_agent.db.recommendation_models import NotificationRow
from trend_agent.db.repositories.content_analyses import OpportunityRepository
from trend_agent.db.repositories.forecasts import ForecastRepository
from trend_agent.db.repositories.recommendations import RecommendationRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import get_logger, log_event
from trend_agent.recommendations import diversity
from trend_agent.recommendations.config import RECOMMENDATION_VERSION, LoadedRecommendationConfig
from trend_agent.recommendations.context_builder import EvidenceContext
from trend_agent.recommendations.idea_generator import (
    ConceptGenerator,
    LLMConceptGenerator,
    TemplateConceptGenerator,
    Usage,
)
from trend_agent.recommendations.notifications import (
    AlertEvent,
    Channel,
    DispatchReport,
    EmailChannel,
    InAppChannel,
    NotificationCenter,
    StoredAlert,
    TelegramChannel,
    detect_events,
)
from trend_agent.recommendations.orchestrator import RecommendationEngine, cost_usd
from trend_agent.schemas.content_opportunity import OpportunityReport
from trend_agent.schemas.dashboard import GenerateResult
from trend_agent.schemas.forecast import ForecastOutput
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    RecommendationOutput,
    RecommendationStatus,
)
from trend_agent.utils.time import Clock, utc_now

if TYPE_CHECKING:
    from trend_agent.content_analysis.service import ContentAnalysisService
    from trend_agent.verification.service import VerificationService

_log = get_logger(__name__)


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _earliest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


# ------------------------------------------------------------------- alert store
class SqlAlertStore:
    """``AlertStore`` over ``notifications`` / ``notification_deliveries``; one short
    transaction per call (deliveries may wait on the network; no transaction is held)."""

    def __init__(self, factory: SessionFactory, clock: Clock = utc_now) -> None:
        self._factory = factory
        self._clock = clock

    def exists(self, dedup_key: str) -> bool:
        with transaction(self._factory) as s:
            return RecommendationRepository(s).alert_exists(dedup_key)

    def last_for_subject(self, subject_key: str) -> datetime | None:
        with transaction(self._factory) as s:
            return RecommendationRepository(s).last_alert_for_subject(subject_key)

    def add(self, alert: StoredAlert) -> None:
        e = alert.event
        with transaction(self._factory) as s:
            RecommendationRepository(s).add_alert(
                NotificationRow(
                    id=uuid.UUID(alert.id),
                    dedup_key=e.dedup_key[:200],
                    subject_key=e.subject_key[:200],
                    event_type=e.event_type,
                    severity=e.severity,
                    title=e.title,
                    body=e.body,
                    recommendation_id=e.recommendation_id,
                    opportunity_key=e.opportunity_id,
                    is_synthetic=e.synthetic,
                    read=False,
                    created_at=alert.created_at,
                )
            )

    def update_delivery(self, alert_id: str, channel: str, status: dict[str, Any]) -> None:
        with transaction(self._factory) as s:
            RecommendationRepository(s).update_delivery(alert_id, channel, status, self._clock())


def make_channels(settings: Settings) -> list[Channel]:
    """In-app always; email/Telegram only report ``configured`` when credentials exist."""
    return [
        InAppChannel(),
        EmailChannel(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=(
                settings.smtp_password.get_secret_value() if settings.smtp_password else None
            ),
            sender=settings.alert_email_from,
            recipient=settings.alert_email_to,
        ),
        TelegramChannel(
            token=(
                settings.telegram_bot_token.get_secret_value()
                if settings.telegram_bot_token
                else None
            ),
            chat_id=settings.telegram_chat_id,
        ),
    ]


# --------------------------------------------------------------------- service
@dataclass
class _Candidate:
    report: OpportunityReport
    row_id: int
    expires_at: datetime | None
    bundle_id: str | None
    target_id: int | None = None


@dataclass
class VerifyReport:
    recommendation_id: str
    status: str
    still_valid: bool
    current: bool
    superseded_by_newer_evidence: bool
    problems: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


class RecommendationService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        loaded: LoadedRecommendationConfig,
        settings: Settings,
        verification: VerificationService,
        content: Callable[[], ContentAnalysisService],
        clock: Clock = utc_now,
        generator: ConceptGenerator | None = None,
        channels: Sequence[Channel] | None = None,
        include_synthetic: bool = False,
    ) -> None:
        self._factory = session_factory
        self.loaded = loaded
        self.cfg = loaded.config
        self.settings = settings
        self.verification = verification
        self._content_factory = content
        self._content: ContentAnalysisService | None = None
        self._clock = clock
        self._generator = generator
        self.include_synthetic = include_synthetic
        self.user_key = settings.dashboard_user
        # Phase 8: optional spend check (``BudgetGuard.llm_allowed``) for the real LLM path.
        self.llm_budget: Callable[[], tuple[bool, str]] | None = None
        self.alerts_enabled = True
        self.template = TemplateConceptGenerator(
            hooks_per_concept=self.cfg.generation.hooks_per_concept
        )
        self.alert_store = SqlAlertStore(session_factory, clock)
        self.notifier = NotificationCenter(
            list(channels) if channels is not None else make_channels(settings),
            self.alert_store,
            loaded,
            demo_mode=False,
        )

    # ------------------------------------------------------------------ helpers
    def content(self) -> ContentAnalysisService:
        if self._content is None:
            self._content = self._content_factory()
        return self._content

    def profile(self, profile_id: str | None = None) -> CreatorProfile:
        with transaction(self._factory) as s:
            p = RecommendationRepository(s).profile(profile_id)
        return p or CreatorProfile()

    def update_profile(self, profile: CreatorProfile) -> CreatorProfile:
        with transaction(self._factory) as s:
            RecommendationRepository(s).save_profile(profile, default=True, now=self._clock())
        return profile

    def llm_status(self) -> dict[str, Any]:
        llm = self.cfg.llm
        with transaction(self._factory) as s:
            used = RecommendationRepository(s).tokens_since(_start_of_day(self._clock()))
        key = self.settings.anthropic_api_key is not None
        mode = (
            f"assist enabled ({llm.model})"
            if self._generator is None and llm.enabled and key
            else (
                f"injected generator ({self._generator.name})"
                if self._generator is not None
                else "template (deterministic; LLM "
                + ("disabled in config/recommendations.yaml" if not llm.enabled else "key missing")
                + ")"
            )
        )
        return {
            "mode": mode,
            "model": llm.model if llm.enabled else None,
            "tokens_today": used,
            "daily_token_budget": llm.daily_token_budget,
            "usd_per_million_input_tokens": llm.usd_per_million_input_tokens,
            "usd_per_million_output_tokens": llm.usd_per_million_output_tokens,
        }

    def _choose_generator(self, now: datetime, used_this_run: int) -> tuple[ConceptGenerator, str]:
        llm = self.cfg.llm
        if self._generator is not None:
            gen: ConceptGenerator = self._generator
        elif not llm.enabled:
            return self.template, ""
        elif self.settings.anthropic_api_key is None:
            return self.template, "LLM enabled but TREND_AGENT_ANTHROPIC_API_KEY is not set"
        else:
            from trend_agent.content_analysis.llm_client import AnthropicAnalysisLLM

            gen = LLMConceptGenerator(
                AnthropicAnalysisLLM(
                    api_key=self.settings.anthropic_api_key.get_secret_value(),
                    model=llm.model,
                    max_tokens=llm.max_output_tokens,
                ),
                max_attempts=llm.max_attempts,
            )
        if gen.name == self.template.name:
            return gen, ""
        if self._generator is None and self.llm_budget is not None:
            ok, why = self.llm_budget()
            if not ok:
                return self.template, why
        with transaction(self._factory) as s:
            used = RecommendationRepository(s).tokens_since(_start_of_day(now))
        if used + used_this_run >= llm.daily_token_budget:
            return self.template, (
                f"daily LLM token budget reached ({used + used_this_run}/"
                f"{llm.daily_token_budget}); used the deterministic template generator"
            )
        return gen, ""

    def _candidates(
        self, limit: int, category: str | None, statuses: Sequence[str]
    ) -> list[_Candidate]:
        out = []
        with transaction(self._factory) as s:
            for row in OpportunityRepository(s).ranked(
                limit=limit,
                category=category,
                statuses=statuses,
                include_synthetic=self.include_synthetic,
            ):
                run = (
                    s.get(ContentAnalysisRunRow, row.analysis_run_id)
                    if row.analysis_run_id
                    else None
                )
                out.append(
                    _Candidate(
                        OpportunityReport.model_validate(row.report),
                        row.id,
                        row.expires_at,
                        str(run.bundle_id) if run is not None and run.bundle_id else None,
                        run.target_id if run is not None else None,
                    )
                )
        return out

    def forecasts_for(
        self, report: OpportunityReport
    ) -> tuple[list[ForecastOutput], datetime | None]:
        with transaction(self._factory) as s:
            rows = ForecastRepository(s).latest(
                query=report.canonical_query,
                include_synthetic=self.include_synthetic,
                limit=12,
            )
            outs = [ForecastOutput.model_validate(r.output) for r in rows]
            expiry = _earliest(*(r.expires_at for r in rows))
        return outs, expiry

    def evidence_for(
        self, report: OpportunityReport, bundle_id: str | None, target_id: int | None = None
    ) -> tuple[EvidenceContext | None, str]:
        """The report's own evidence bundle (read-only registry view) plus the entity
        catalog and authoritative statistics the Phase 5 analysis used."""
        from trend_agent.content_analysis.service import _AnalysisStore
        from trend_agent.verification.registry import EvidenceRegistry

        if bundle_id is None:
            return None, "the analysis has no stored evidence bundle"
        store = _AnalysisStore(self._factory)
        bundle = store.get_bundle(bundle_id)
        if bundle is None:
            return None, "the analysis' evidence bundle is no longer stored (expired)"
        records = store.get_many(bundle.evidence_ids)
        try:
            if target_id is not None:  # the monitored target the analysis was made for
                inp = self.content().inputs_for_target(
                    target_id,
                    region=report.region,
                    language=report.language,
                    include_synthetic=self.include_synthetic,
                )
            else:
                inp = self.content().inputs_for_query(
                    report.canonical_query,
                    category=report.category,
                    region=report.region,
                    language=report.language,
                    include_synthetic=self.include_synthetic,
                )
        except (LookupError, PermissionError, ValueError) as exc:
            return None, f"entity catalog unavailable: {exc}"
        registry = EvidenceRegistry(
            store, self.verification.signer, self.verification.config, self._clock
        )
        return (
            EvidenceContext(
                registry,
                self.verification.loaded,
                bundle,
                tuple(records.values()),
                inp.catalog,
                inp.stats,
                self._clock(),
            ),
            "",
        )

    # ---------------------------------------------------------------- generate
    def generate(
        self,
        *,
        category: str | None = None,
        all_categories: bool = False,
        max_opportunities: int | None = None,
        dry_run: bool = False,
        trigger: str = "cli",
        force: bool = False,
        profile_id: str | None = None,
    ) -> GenerateResult:
        t0 = time.perf_counter()
        now = self._clock()
        gcfg = self.cfg.generation
        profile = self.profile(profile_id)
        limit = min(
            max_opportunities or gcfg.max_opportunities_per_run, gcfg.max_opportunities_per_run
        )
        statuses = list(gcfg.eligible_statuses)
        if gcfg.research_hypotheses and "insufficient_evidence" not in statuses:
            statuses.append("insufficient_evidence")
        cat = None if all_categories else category
        run_id = uuid.uuid4()
        notes: list[str] = []
        if dry_run:
            notes.append("dry run: deterministic template generator, nothing stored or alerted")
        if not dry_run:
            with transaction(self._factory) as s:
                RecommendationRepository(s).start_run(
                    run_id,
                    started_at=now,
                    trigger=trigger,
                    category=cat,
                    profile_id=profile.profile_id,
                    generator="pending",
                    recommendation_version=RECOMMENDATION_VERSION,
                    configuration_version=self.loaded.version_tag,
                    is_synthetic=self.include_synthetic,
                )
        usage = Usage()
        try:
            res = self._generate(
                now, profile, limit, cat, statuses, dry_run, force, run_id, usage, notes
            )
        except Exception as exc:
            if not dry_run:
                with transaction(self._factory) as s:
                    RecommendationRepository(s).finish_run(
                        run_id,
                        status="failed",
                        completed_at=self._clock(),
                        error=f"{type(exc).__name__}: {exc}"[:2000],
                        llm_calls=usage.calls,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        seconds=round(time.perf_counter() - t0, 3),
                    )
            raise
        res.seconds = round(time.perf_counter() - t0, 3)
        if not dry_run:
            with transaction(self._factory) as s:
                RecommendationRepository(s).finish_run(
                    run_id,
                    status="completed",
                    completed_at=self._clock(),
                    generator=res.generator,
                    considered=res.considered,
                    generated=res.generated,
                    reused=res.reused,
                    approved=res.approved,
                    research=res.research,
                    rejected=res.rejected,
                    suppressed=res.suppressed,
                    skipped={"items": res.skipped, "notes": res.notes},
                    llm_calls=res.llm_calls,
                    input_tokens=res.input_tokens,
                    output_tokens=res.output_tokens,
                    estimated_cost_usd=res.estimated_cost_usd,
                    seconds=res.seconds,
                )
        log_event(
            _log,
            "recommendations.generated",
            run_id=str(run_id),
            considered=res.considered,
            generated=res.generated,
            reused=res.reused,
            llm_calls=res.llm_calls,
            dry_run=dry_run,
        )
        return res

    def _generate(
        self,
        now: datetime,
        profile: CreatorProfile,
        limit: int,
        category: str | None,
        statuses: list[str],
        dry_run: bool,
        force: bool,
        run_id: uuid.UUID,
        usage: Usage,
        notes: list[str],
    ) -> GenerateResult:
        candidates = self._candidates(limit, category, statuses)
        produced: dict[str, list[RecommendationOutput]] = {}
        meta: dict[str, _Candidate] = {}
        expiry: dict[str, datetime | None] = {}
        skipped: list[str] = []
        reused = generated = 0
        generators: set[str] = set()
        reused_opps: set[str] = set()
        for c in candidates:
            rep = c.report
            oid = rep.opportunity_id
            meta[oid] = c
            forecasts, f_exp = self.forecasts_for(rep)
            ev, why = self.evidence_for(rep, c.bundle_id, c.target_id)
            if ev is None:
                skipped.append(f"{rep.canonical_query}: {why}")
                continue
            gen: ConceptGenerator
            if dry_run:
                gen, note = self.template, ""
            else:
                gen, note = self._choose_generator(now, usage.input_tokens + usage.output_tokens)
            if note and note not in notes:
                notes.append(note)
            engine = RecommendationEngine(self.loaded, generator=gen)
            ctx, problems = engine.context(rep, forecasts, ev, profile)
            if ctx is None:
                skipped.append(f"{rep.canonical_query}: {'; '.join(problems)}")
                continue
            fp = ctx.fingerprint(gen.name, self.loaded.version_tag)
            if not force:
                with transaction(self._factory) as s:
                    existing = RecommendationRepository(s).current_for(oid, profile.profile_id, fp)
                    stored = [RecommendationOutput.model_validate(r.concept) for r in existing]
                if stored:
                    produced[oid] = stored
                    reused += len(stored)
                    reused_opps.add(oid)
                    generators.add(stored[0].generator)
                    continue
            oc = engine.recommend(rep, forecasts, ev, profile)
            usage.add(oc.usage)
            if oc.fallback:
                notes.extend(v for v in oc.violations if "template" in v and v not in notes)
            if oc.skipped and not oc.recommendations:
                skipped.append(f"{rep.canonical_query}: {'; '.join(oc.skipped)}")
                continue
            generators.add(oc.generator)
            produced[oid] = oc.recommendations
            generated += len(oc.recommendations)
            expiry[oid] = _earliest(c.expires_at, f_exp)

        # ---- global ranking over every current recommendation (real / synthetic apart)
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            current_rows = repo.current(
                profile_id=profile.profile_id, include_synthetic=self.include_synthetic
            )
            pool: dict[str, RecommendationOutput] = {}
            prev_scores: dict[str, float] = {}
            prev_status: dict[tuple[str, str], str] = {}
            for row in current_rows:
                if row.opportunity_score is not None:
                    prev_scores.setdefault(row.opportunity_key, row.opportunity_score)
                if row.opportunity_key in produced and row.opportunity_key not in reused_opps:
                    prev_status[(row.opportunity_key, row.video_format)] = row.status
                    continue
                if row.opportunity_key not in produced:
                    pool[str(row.id)] = RecommendationOutput.model_validate(row.concept)
        for recs in produced.values():
            for r in recs:
                pool[r.recommendation_id] = r
        final: dict[str, RecommendationOutput] = {}
        for synthetic in (False, True):
            group = [r for r in pool.values() if r.synthetic is synthetic]
            if group:
                sel = diversity.select(group, self.loaded, limit=None)
                for r in [*sel.ranked, *sel.suppressed, *sel.research]:
                    final[r.recommendation_id] = r
        mine = [final[r.recommendation_id] for recs in produced.values() for r in recs]
        status_counts = {s.value: 0 for s in RecommendationStatus}
        for r in mine:
            status_counts[r.verification.status.value] += 1

        new_recs: list[RecommendationOutput] = []
        dispatch: DispatchReport | None = None
        if not dry_run:
            with transaction(self._factory) as s:
                repo = RecommendationRepository(s)
                for oid, recs in produced.items():
                    c = meta[oid]
                    for r in recs:
                        out = final[r.recommendation_id]
                        if repo.save(
                            out,
                            run_id=run_id,
                            opportunity_row_id=c.row_id,
                            opportunity_score=c.report.score.score,
                            expires_at=expiry.get(oid, c.expires_at),
                            now=now,
                        ):
                            new_recs.append(out)
                    repo.supersede(oid, profile.profile_id, [r.recommendation_id for r in recs])
                for rid, r in final.items():
                    if r.opportunity_id not in produced:
                        repo.set_ranking(rid, r.ranking.rank, r.suppression, now)
            dispatch = self._alert(
                new_recs, [c.report for c in candidates], prev_scores, prev_status, profile, now
            )
        if dispatch is not None and dispatch.created:
            notes.append(f"{len(dispatch.created)} alert(s) created")
        return GenerateResult(
            run_id=str(run_id),
            considered=len(candidates),
            generated=generated,
            approved=status_counts["APPROVED"] + status_counts["APPROVED_WITH_UNCERTAINTY"],
            research=sum(1 for r in mine if r.research_hypothesis),
            rejected=status_counts["REJECTED"],
            suppressed=sum(1 for r in mine if r.suppression.suppressed),
            skipped=skipped,
            llm_calls=usage.calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_usd=cost_usd(usage, self.loaded),
            seconds=0.0,
            synthetic=self.include_synthetic,
            generator=",".join(sorted(generators)) or self.template.name,
            reused=reused,
            notes=notes,
            dry_run=dry_run,
        )

    def _alert(
        self,
        new_recs: Sequence[RecommendationOutput],
        reports: Sequence[OpportunityReport],
        prev_scores: dict[str, float],
        prev_status: dict[tuple[str, str], str],
        profile: CreatorProfile,
        now: datetime,
    ) -> DispatchReport:
        # a regenerated idea (same opportunity and format, new evidence) inherits the status
        # of the idea it replaces, so a verification change is reported
        by_rec = {
            r.recommendation_id: st
            for r in new_recs
            if (st := prev_status.get((r.opportunity_id, r.video_format.value))) is not None
        }
        events: list[AlertEvent] = detect_events(
            new_recs,
            reports,
            self.loaded,
            previous_scores=prev_scores,
            previous_status=by_rec,
        )
        if not self.alerts_enabled:  # Phase 8: the operating mode withdrew creator alerts
            return DispatchReport()
        return self.notifier.dispatch(events, profile=profile, now=now)

    # ------------------------------------------------------------------- reads
    def get(self, rid: str) -> RecommendationOutput | None:
        with transaction(self._factory) as s:
            row = RecommendationRepository(s).get(rid)
            return RecommendationOutput.model_validate(row.concept) if row is not None else None

    def list(self, *, limit: int = 20, **filters: Any) -> list[RecommendationOutput]:
        with transaction(self._factory) as s:
            rows, _ = RecommendationRepository(s).search(
                user_key=self.user_key,
                include_synthetic=self.include_synthetic,
                page_size=limit,
                **filters,
            )
            return [RecommendationOutput.model_validate(r.concept) for r in rows]

    def stats(self) -> dict[str, Any]:
        with transaction(self._factory) as s:
            repo = RecommendationRepository(s)
            out = repo.counts(include_synthetic=self.include_synthetic)
            out["runs"] = [
                {
                    "run_id": str(r.id),
                    "started_at": r.started_at.isoformat(),
                    "status": r.status,
                    "trigger": r.trigger,
                    "generator": r.generator,
                    "considered": r.considered,
                    "generated": r.generated,
                    "reused": r.reused,
                    "llm_calls": r.llm_calls,
                    "tokens": r.input_tokens + r.output_tokens,
                    "estimated_cost_usd": r.estimated_cost_usd,
                    "seconds": r.seconds,
                }
                for r in repo.runs(10)
            ]
        out["llm"] = self.llm_status()
        out["versions"] = {
            "recommendation": RECOMMENDATION_VERSION,
            "configuration": self.loaded.version_tag,
            "ranking": self.cfg.ranking.version,
        }
        out["notice"] = (
            "Scores are research-priority indices from versioned, experimental weights, not "
            "probabilities of success. Feedback counts are recorded for evaluation only; they "
            "do not retrain or override anything."
        )
        return out

    def verify(self, rid: str) -> VerifyReport | None:
        """Re-check a stored recommendation now (read-only): its cited evidence still
        resolves in the analysis' bundle (integrity, membership, expiry, source removal),
        the opportunity report still exists and the idea is still current (not superseded by
        newer evidence). The generation-time checks are reported as recorded; an LLM is
        never asked to confirm anything."""
        from trend_agent.content_analysis.service import _AnalysisStore
        from trend_agent.db.repositories.recommendations import DashboardReadRepository
        from trend_agent.verification.registry import EvidenceRegistry

        with transaction(self._factory) as s:
            row = RecommendationRepository(s).get(rid)
            if row is None:
                return None
            rec = RecommendationOutput.model_validate(row.concept)
            current = row.is_current
            found = DashboardReadRepository(s).opportunity(rec.opportunity_id)
        problems: list[str] = []
        checks: list[dict[str, Any]] = [
            {"check": f"generation:{c.check}", "passed": c.passed, "detail": c.detail}
            for c in rec.verification.checks
        ]
        if found is None:
            problems.append("the opportunity report is no longer stored (expired or purged)")
        else:
            _, bundle_id = found
            store = _AnalysisStore(self._factory)
            bundle = store.get_bundle(bundle_id) if bundle_id else None
            ids = [e.evidence_id for e in rec.evidence]
            resolved: list[str] = []
            if bundle is None:
                problems.append("the analysis' evidence bundle is no longer stored")
            elif ids:
                registry = EvidenceRegistry(
                    store, self.verification.signer, self.verification.config, self._clock
                )
                res = registry.resolve(
                    ids, bundle, allow_synthetic=rec.synthetic, now=self._clock()
                )
                resolved = [f"{p.evidence_id}: {p.code.value}" for p in res.problems]
                problems += resolved
            checks.append(
                {
                    "check": "evidence_resolves_now",
                    "passed": bundle is not None and not resolved,
                    "detail": f"{len(ids)} cited evidence record(s)",
                }
            )
        if not current:
            problems.append("superseded: newer evidence produced a newer recommendation")
        checks.append({"check": "current", "passed": current, "detail": ""})
        return VerifyReport(
            recommendation_id=rid,
            status=rec.verification.status.value,
            still_valid=not problems,
            current=current,
            superseded_by_newer_evidence=not current,
            problems=problems,
            checks=checks,
        )

    def purge_expired(self) -> dict[str, int]:
        with transaction(self._factory) as s:
            return RecommendationRepository(s).purge_expired(self._clock())
