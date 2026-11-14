"""Input validation (Phase 4-6 contracts) and the constrained recommendation context.

The context contains only: the Phase 5 report's query, intent, *confirmed* entities,
information requirements with coverage, the primary gap, Phase 4 demand evidence (as the
report carries it, never recomputed), the Phase 6 forecast summary (or an explicit
"unavailable"), sentences from credible evidence records that mention the topic, and the
creator profile. Generators see nothing else, and every number in it comes from code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trend_agent.discovery.models import SearchIntent
from trend_agent.recommendations.config import (
    RECOMMENDATION_VERSION,
    LoadedRecommendationConfig,
)
from trend_agent.schemas.content_opportunity import DemandBasis, OpportunityReport
from trend_agent.schemas.forecast import ForecastOutput, ForecastStatus
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    DemandSummary,
    EvidenceRef,
    ForecastSummary,
    WindowSummary,
)
from trend_agent.schemas.verification import (
    CREDIBLE,
    AuthoritativeStatistics,
    EvidenceBundle,
    EvidenceRecord,
)
from trend_agent.verification.config import LoadedVerificationConfig
from trend_agent.verification.entities import EntityCatalog
from trend_agent.verification.registry import EvidenceRegistry
from trend_agent.verification.text import sentences

_DIRECT = {
    DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE.value,
    DemandBasis.DIRECT_SEARCH_CANDIDATE.value,
}
_NO_DEMAND = {
    DemandBasis.INSUFFICIENT_EVIDENCE.value,
    DemandBasis.MANUAL_NO_DEMAND_EVIDENCE.value,
}
_USABLE_FORECAST = {
    ForecastStatus.READY.value,
    ForecastStatus.LIMITED_CONFIDENCE.value,
    ForecastStatus.BASELINE_ONLY.value,
}
_FACT_KINDS = {"official_announcement", "code_release", "curated_reference", "news_report"}

# Phase 5 primary intents -> the discovery intent vocabulary the verification gate uses
_GATE_INTENT = {
    "tutorial": SearchIntent.TUTORIAL,
    "problem_solving": SearchIntent.TROUBLESHOOTING,
    "comparison": SearchIntent.COMPARISON,
    "review": SearchIntent.REVIEW,
    "demonstration": SearchIntent.DEMONSTRATION,
    "news": SearchIntent.NEWS,
    "informational": SearchIntent.EXPLANATION,
}


@dataclass(frozen=True)
class Requirement:
    key: str
    text: str
    priority: str
    status: str  # Phase 5 coverage: covered | partially_covered | not_evidenced | ...

    @property
    def established(self) -> bool:
        return self.status in ("covered", "partially_covered")


@dataclass(frozen=True)
class Fact:
    evidence_id: str
    text: str
    publisher: str | None
    kind: str


@dataclass(frozen=True)
class EvidenceContext:
    """What the verification gate needs (read-only registry view of the report's bundle)."""

    registry: EvidenceRegistry
    vloaded: LoadedVerificationConfig
    bundle: EvidenceBundle
    records: tuple[EvidenceRecord, ...]
    catalog: EntityCatalog
    stats: AuthoritativeStatistics
    now: datetime


@dataclass
class RecommendationContext:
    opportunity_id: str
    report: OpportunityReport
    query: str
    query_id: int | None
    category: str | None
    region: str | None
    language: str | None
    topic_cluster_id: int | None
    primary_intent: str
    sub_intent: str | None
    gate_intent: SearchIntent
    time_sensitive: bool
    entities: tuple[str, ...]  # confirmed entity names, most specific first
    entity_keys: frozenset[str]
    other_entity: str | None  # a second confirmed entity for comparisons (e.g. prior version)
    requirements: tuple[Requirement, ...]
    gap_type: str | None
    gap_strength: str | None
    gap_statement: str | None
    gap_keys: tuple[str, ...]
    facts: tuple[Fact, ...]
    evidence: tuple[EvidenceRef, ...]
    bundle_ids: frozenset[str]
    competing_videos: tuple[str, ...]
    current_exact_formats: tuple[str, ...]
    demand: DemandSummary
    forecast: ForecastSummary
    profile: CreatorProfile
    synthetic: bool
    now: datetime
    limitations: list[str] = field(default_factory=list)
    research_only: bool = False

    @property
    def topic(self) -> str:
        return self.entities[0] if self.entities else self.query

    def fingerprint(self, generator: str, config_tag: str) -> str:
        payload = {
            "report": self.report.opportunity_id,
            "analysed": self.report.analysed_at.isoformat(),
            "forecast": self.forecast.prediction_id,
            "profile": self.profile.model_dump(mode="json"),
            "generator": generator,
            "config": config_tag,
            "version": RECOMMENDATION_VERSION,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Validation:
    eligible: bool
    research_only: bool
    problems: tuple[str, ...]
    limitations: tuple[str, ...]
    forecasts: tuple[ForecastOutput, ...]


def validate_inputs(
    report: OpportunityReport,
    forecasts: Sequence[ForecastOutput],
    *,
    now: datetime,
    loaded: LoadedRecommendationConfig,
) -> Validation:
    """Phase 4-6 inputs checked before anything is generated. A forecast that does not
    belong to this query, is stale, or mixes synthetic and real data is dropped (and the
    recommendation says so); it is never read as "zero growth"."""
    cfg = loaded.config.generation
    problems: list[str] = []
    limitations: list[str] = []
    if report.intent.policy_flags:
        problems.append("blocked by policy: " + ", ".join(report.intent.policy_flags))
    status = report.status.value
    research_only = False
    if status not in cfg.eligible_statuses:
        if status == "insufficient_evidence" and cfg.research_hypotheses:
            research_only = True
            limitations.append("insufficient evidence: research candidate only")
        else:
            problems.append(f"opportunity status '{status}' is not eligible")
    if now - report.analysed_at > timedelta(hours=cfg.max_report_age_hours):
        research_only = True
        limitations.append("the content analysis is older than the configured maximum age")
    if (
        cfg.min_opportunity_score is not None
        and report.score.score is not None
        and report.score.score < cfg.min_opportunity_score
    ):
        problems.append("opportunity score below the configured minimum")
    kept: list[ForecastOutput] = []
    for f in forecasts:
        why = None
        if f.synthetic != report.synthetic:
            why = "forecast synthetic flag differs from the opportunity's"
        elif report.query_id is not None and f.query_id is not None:
            if f.query_id != report.query_id:
                why = "forecast belongs to another query"
        elif f.query.strip().lower() != report.canonical_query.strip().lower():
            why = "forecast belongs to another query"
        if why is None and now - f.prediction_timestamp > timedelta(
            hours=cfg.max_forecast_age_hours
        ):
            why = "forecast is older than the configured maximum age"
        if why is None and f.prediction_timestamp > now + timedelta(minutes=5):
            why = "forecast is dated in the future"
        if why is None:
            kept.append(f)
        elif why not in limitations:
            limitations.append(f"forecast ignored: {why}")
    return Validation(
        eligible=not problems,
        research_only=research_only,
        problems=tuple(problems),
        limitations=tuple(limitations),
        forecasts=tuple(kept),
    )


def demand_summary(report: OpportunityReport) -> DemandSummary:
    d = report.demand
    basis = d.basis.value
    return DemandSummary(
        basis=basis,
        measurement_type=d.measurement_type,
        classification=d.classification,
        alert_stage=d.alert_stage,
        severity=d.severity,
        relative_change=d.relative_change,
        freshness=d.freshness,
        detected_at=d.detected_at,
        window_start=d.window_start,
        window_end=d.window_end,
        surge_event_id=d.surge_event_id,
        direct_youtube_search=basis in _DIRECT,
        indirect=basis not in _DIRECT,
        statement=d.statement,
        limitations=d.limitations,
        evidence_ids=d.evidence_ids,
    )


def forecast_summary(forecasts: Sequence[ForecastOutput]) -> ForecastSummary:
    """The most useful usable forecast (24 h, then 72 h, then 6 h); else unavailable."""
    if not forecasts:
        return ForecastSummary(available=False, status_reasons=("no forecast for this query",))
    usable = [f for f in forecasts if f.status.value in _USABLE_FORECAST]
    if not usable:
        f = forecasts[0]
        return ForecastSummary(
            available=False,
            prediction_id=f.prediction_id,
            status=f.status.value,
            status_reasons=f.status_reasons,
            horizon_hours=f.forecast_horizon_hours,
            target_family=f.target_family,
            is_proxy=f.is_proxy,
            synthetic=f.synthetic,
            issued_at=f.prediction_timestamp,
        )
    order = {24: 0, 72: 1, 6: 2}
    f = sorted(usable, key=lambda o: order.get(o.forecast_horizon_hours, 9))[0]
    traj = f.trajectory[0] if f.trajectory else None
    model = None
    validated = None
    if f.breakout is not None and f.breakout.model is not None:
        model, validated = f.breakout.model.name, f.breakout.model.validated
    elif f.continuation is not None and f.continuation.model is not None:
        model, validated = f.continuation.model.name, f.continuation.model.validated
    pe = f.peak_estimate
    return ForecastSummary(
        available=True,
        prediction_id=f.prediction_id,
        status=f.status.value,
        status_reasons=f.status_reasons,
        horizon_hours=f.forecast_horizon_hours,
        target_family=f.target_family,
        is_proxy=f.is_proxy,
        breakout_probability=f.breakout.probability if f.breakout else None,
        breakout_calibration=f.breakout.calibration_status.value if f.breakout else None,
        continuation_probability=f.continuation.probability if f.continuation else None,
        trajectory_value=traj.value if traj else None,
        trajectory_lower=traj.lower if traj else None,
        trajectory_upper=traj.upper if traj else None,
        peak_status=pe.status.value if pe else None,
        peak_earliest_hours=pe.earliest_hours if pe else None,
        peak_latest_hours=pe.latest_hours if pe else None,
        windows=tuple(
            WindowSummary(
                production_profile=w.production_profile,
                status=w.status.value,
                earliest_publication=w.earliest_publication,
                probability_elevated_at_publication=w.probability_elevated_at_publication,
            )
            for w in f.opportunity_windows
        ),
        model=model,
        validated_model=validated,
        issued_at=f.prediction_timestamp,
        synthetic=f.synthetic,
    )


def _names(report: OpportunityReport, catalog: EntityCatalog) -> tuple[list[str], set[str]]:
    confirmed = [e for e in report.intent.entities if e.state == "confirmed"]
    # items before events before products: the most specific subject first
    rank = {"game_item": 0, "ai_model": 1, "ai_product": 1, "framework": 1, "software": 1}
    confirmed.sort(key=lambda e: rank.get(e.entity_type or "", 2))
    names = [e.name for e in confirmed]
    keys = {e.entity_key for e in confirmed if e.entity_key}
    for k in list(keys):
        ent = catalog.get(k)
        if ent is not None and ent.canonical_name not in names:
            names.append(ent.canonical_name)
    return names, keys


def _other_entity(keys: set[str], catalog: EntityCatalog) -> str | None:
    """A confirmed earlier version of the topic entity (comparison partner), or None."""
    for k in sorted(keys):
        ent = catalog.get(k)
        if ent is None or not ent.version:
            continue
        stem = re.sub(r"[\s-]*v?\d+(\.\d+)*$", "", ent.canonical_name).strip().lower()
        for other in catalog.entities():
            if other.key == ent.key or other.entity_type != ent.entity_type or not other.version:
                continue
            other_stem = re.sub(r"[\s-]*v?\d+(\.\d+)*$", "", other.canonical_name).strip().lower()
            if other_stem == stem and other.status.value == "verified":
                return other.canonical_name
    return None


def _facts(records: Sequence[EvidenceRecord], names: Sequence[str]) -> list[Fact]:
    out: list[Fact] = []
    lowered = [n.lower() for n in names]
    for r in records:
        if (
            r.trust_level not in CREDIBLE
            or r.injection_flags
            or r.evidence_kind.value not in _FACT_KINDS
            or not r.excerpt
        ):
            continue
        for s in sentences(r.excerpt):
            s = s.strip()
            if 12 <= len(s) <= 240 and any(n in s.lower() for n in lowered):
                out.append(Fact(r.evidence_id, s, r.publisher, r.evidence_kind.value))
    return out


def build_context(
    report: OpportunityReport,
    forecasts: Sequence[ForecastOutput],
    evidence: EvidenceContext,
    *,
    profile: CreatorProfile,
    validation: Validation,
) -> RecommendationContext:
    names, keys = _names(report, evidence.catalog)
    by_id = {r.evidence_id: r for r in evidence.records}
    refs = tuple(
        EvidenceRef(
            evidence_id=r.evidence_id,
            kind=r.evidence_kind.value,
            trust=r.trust_level.value,
            publisher=r.publisher,
            url=r.canonical_url,
            published_at=r.published_at,
            note="credible source" if r.trust_level in CREDIBLE else "untrusted source text",
        )
        for r in evidence.records
        # third-party video metadata (titles, channel names) is retention-limited: competing
        # videos are referenced by YouTube ID only, never copied into recommendations
        if r.evidence_kind.value not in ("measurement", "video_metadata")
    )
    gap = report.primary_gap
    limitations = list(validation.limitations)
    forecast = forecast_summary(validation.forecasts)
    if not forecast.available:
        limitations.append(
            "no usable demand forecast: urgency and forecast opportunity are unavailable, not zero"
        )
    demand = demand_summary(report)
    if demand.basis in _NO_DEMAND:
        limitations.append("no demand evidence from Phase 4")
    elif demand.indirect:
        limitations.append(
            "demand evidence is indirect (" + demand.basis + "): not measured YouTube search growth"
        )
    if report.synthetic:
        limitations.append("SYNTHETIC test data: not a real recommendation")
    current_exact = tuple(
        v.video_format
        for v in report.relevant_videos
        if v.freshness == "current" and v.entity_match == "exact"
    )
    return RecommendationContext(
        opportunity_id=report.opportunity_id,
        report=report,
        query=report.canonical_query,
        query_id=report.query_id,
        category=report.category,
        region=report.region,
        language=report.language,
        topic_cluster_id=report.topic_cluster_id,
        primary_intent=report.intent.primary_intent.value,
        sub_intent=report.intent.sub_intent.value if report.intent.sub_intent else None,
        gate_intent=_GATE_INTENT.get(
            report.intent.primary_intent.value,
            SearchIntent.ITEM_LOCATION
            if report.intent.sub_intent is not None
            and report.intent.sub_intent.value == "item_location"
            else SearchIntent.GAME_GUIDE
            if report.intent.primary_intent.value == "gaming_guide"
            else SearchIntent.OTHER,
        ),
        time_sensitive=report.intent.time_sensitive,
        entities=tuple(names),
        entity_keys=frozenset(keys),
        other_entity=_other_entity(keys, evidence.catalog),
        requirements=tuple(
            Requirement(q.requirement_key, q.requirement, q.priority, q.best_status.value)
            for q in report.requirement_coverage
        ),
        gap_type=gap.gap_type.value if gap else None,
        gap_strength=gap.evidence_strength.value if gap else None,
        gap_statement=gap.statement if gap else None,
        gap_keys=gap.requirement_keys if gap else (),
        facts=tuple(_facts([by_id[i] for i in evidence.bundle.evidence_ids if i in by_id], names)),
        evidence=refs,
        bundle_ids=frozenset(evidence.bundle.evidence_ids),
        competing_videos=tuple(v.video_id for v in report.relevant_videos),
        current_exact_formats=current_exact,
        demand=demand,
        forecast=forecast,
        profile=profile,
        synthetic=report.synthetic,
        now=evidence.now,
        limitations=limitations,
        research_only=validation.research_only,
    )
