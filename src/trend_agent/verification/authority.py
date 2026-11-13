"""Deterministic statistical authority.

Search-interest growth, anomaly and CUSUM scores, severity, freshness, significance,
detector confidence and baseline readiness come only from the Phase 4 engine. This
module copies them into ``AuthoritativeStatistics`` and checks every trend, search or
statistics statement a generator wrote against them:

* absolute search volumes are never available (all search data is a relative index);
* any number about growth or a score must equal the engine's value, otherwise it is an
  override and is removed; matching numbers are still re-rendered by code;
* "search demand is rising" needs DIRECT or EXTERNAL search-interest evidence, and
  "YouTube search" needs DIRECT YouTube search interest - proxy video activity or
  suggestion presence is never presented as measured search growth;
* "confirmed"/"significant" wording needs a CONFIRMED stage / STATISTICALLY_SUPPORTED.

Published trend wording is always the deterministic statement built here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.schemas.detection import SupportStatus
from trend_agent.schemas.evidence import STAGE_RANK, AlertStage, SurgeClassification
from trend_agent.schemas.monitoring import (
    SEARCH_INTEREST_CATEGORIES,
    MeasurementCategory,
)
from trend_agent.schemas.surge import SurgeAlert, TargetSurgeView
from trend_agent.schemas.verification import (
    AuthoritativeStatistics,
    ClaimStatus,
    ViolationCode,
)
from trend_agent.verification.claims import Features, NumberKind
from trend_agent.verification.config import VerificationConfig

_CATEGORY_WORDS = {
    MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST: "YouTube search interest",
    MeasurementCategory.EXTERNAL_SEARCH_INTEREST: "search interest on another search property",
    MeasurementCategory.VIDEO_ACTIVITY: "video activity",
    MeasurementCategory.SEARCH_SUGGESTION_PRESENCE: "search-suggestion presence",
    MeasurementCategory.OFFICIAL_EVENT_SIGNAL: "official announcements",
    MeasurementCategory.COMMUNITY_ACTIVITY: "community activity",
}


def _primary(alerts: Sequence[SurgeAlert]) -> SurgeAlert | None:
    if not alerts:
        return None

    def rank(a: SurgeAlert) -> tuple[int, int, float]:
        direct = int(a.measurement_type is MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST)
        search = int(a.measurement_type in SEARCH_INTEREST_CATEGORIES)
        return (direct * 2 + search, STAGE_RANK.get(a.alert_status, 0), a.severity)

    return max(alerts, key=rank)


def statement(stats: AuthoritativeStatistics, query: str) -> str:
    q = f"“{query}”"
    if not stats.available or stats.classification is None or stats.highest_stage is None:
        return (
            f"No open surge episode exists for {q}; no claim about rising search demand can "
            "be made from the statistical engine."
        )
    stage = stats.highest_stage.value.upper()
    pct = (
        f" {stats.relative_change * 100:+.0f}% against its expected level"
        if stats.relative_change is not None
        else ""
    )
    sev = (
        f" (severity {stats.severity:.0f}/100, an operational ranking index)"
        if stats.severity is not None
        else ""
    )
    if stats.classification is SurgeClassification.DIRECT_SEARCH_SURGE:
        return (
            f"YouTube search interest for {q} is{pct or ' elevated'} - a relative index, not a "
            f"number of searches. Detection stage: {stage}{sev}."
        )
    if stats.classification is SurgeClassification.EXTERNAL_SEARCH_SURGE:
        return (
            f"Search interest for {q} on another search property (not YouTube Search) "
            f"is{pct or ' elevated'} - a relative index. Detection stage: {stage}{sev}. "
            "This is not measured YouTube search growth."
        )
    cats = ", ".join(_CATEGORY_WORDS.get(c, c.value) for c in stats.evidence_categories)
    return (
        f"Indirect signals for {q} rose ({cats}). This is a potential YouTube demand "
        f"opportunity at stage {stage}, not measured YouTube search growth."
    )


def build_statistics(
    view: TargetSurgeView | None,
    *,
    target_id: int | None,
    query: str,
    evidence_ids: Mapping[int, str] | None = None,
) -> AuthoritativeStatistics:
    """Copy (never recompute) the engine's view of one target."""
    if view is None or not view.alerts:
        stats = AuthoritativeStatistics(target_id=target_id, available=False)
        return stats.model_copy(update={"statement": statement(stats, query)})
    alerts = list(view.alerts)
    primary = _primary(alerts)
    assert primary is not None
    search_alert = primary.measurement_type in SEARCH_INTEREST_CATEGORIES and not primary.is_proxy
    ids = evidence_ids or {}
    stats = AuthoritativeStatistics(
        target_id=target_id,
        available=True,
        classification=view.combined_classification,
        highest_stage=view.highest_stage,
        evidence_categories=view.evidence_categories,
        measures_youtube_search=(
            view.combined_classification is SurgeClassification.DIRECT_SEARCH_SURGE
        ),
        measures_search=any(c in SEARCH_INTEREST_CATEGORIES for c in view.evidence_categories),
        severity=primary.severity,
        maximum_severity=primary.maximum_severity,
        relative_change=primary.baseline.relative_difference if search_alert else None,
        observed=primary.baseline.observed if search_alert else None,
        expected=primary.baseline.expected if search_alert else None,
        scale=primary.baseline.scale,
        confidence_status=primary.confidence_status,
        baseline_readiness=primary.baseline.readiness,
        freshness=primary.freshness,
        detected_at=primary.detected_at,
        last_updated_at=max(a.last_updated_at for a in alerts),
        event_ids=tuple(a.event_id for a in alerts),
        evidence_ids=tuple(ids[a.event_id] for a in alerts if a.event_id in ids),
        synthetic=view.synthetic,
    )
    return stats.model_copy(update={"statement": statement(stats, query)})


@dataclass(frozen=True)
class TrendCheck:
    status: ClaimStatus
    violations: tuple[ViolationCode, ...]
    reasons: tuple[str, ...]


def stats_stale(stats: AuthoritativeStatistics, now: datetime, config: VerificationConfig) -> bool:
    if not stats.available or stats.last_updated_at is None:
        return False
    from trend_agent.schemas.verification import EvidenceKind

    fresh = config.freshness.fresh.get(EvidenceKind.SURGE_DETECTION, timedelta(days=3))
    return now - stats.last_updated_at > fresh * config.freshness.usable_factor


def check_trend(
    f: Features, stats: AuthoritativeStatistics, *, now: datetime, config: VerificationConfig
) -> TrendCheck:
    """Verify a trend/search/statistics statement against the engine (never the reverse)."""
    t = f.trend
    assert t is not None
    v: list[ViolationCode] = []
    reasons: list[str] = []
    contradicted = False
    tol = config.matching
    if t.volume:
        v.append(ViolationCode.SEARCH_VOLUME)
        reasons.append(
            "absolute search volumes are not available; search interest is a relative index"
        )
    open_stage = stats.available and stats.highest_stage is not None
    stage_rank = STAGE_RANK.get(stats.highest_stage, 0) if stats.highest_stage else 0
    for n in f.numbers:
        if n.kind is NumberKind.SEARCH_COUNT:
            continue
        if n.kind is NumberKind.PERCENT and (t.growth or t.search):
            if stats.relative_change is None:
                v.append(ViolationCode.STATISTIC_OVERRIDE)
                reasons.append(f"{n.raw}: the engine reports no search-interest change figure")
            elif abs(n.value - stats.relative_change * 100) > tol.percent_tolerance:
                v.append(ViolationCode.STATISTIC_OVERRIDE)
                contradicted = True
                reasons.append(
                    f"{n.raw} differs from the engine's {stats.relative_change * 100:+.1f}%"
                )
        elif n.kind is NumberKind.MULTIPLIER:
            if stats.relative_change is None:
                v.append(ViolationCode.STATISTIC_OVERRIDE)
                reasons.append(f"{n.raw}: the engine reports no search-interest ratio")
            elif (
                abs(n.value - (1 + stats.relative_change)) > tol.relative_tolerance * n.value + 0.05
            ):
                v.append(ViolationCode.STATISTIC_OVERRIDE)
                contradicted = True
                reasons.append(
                    f"{n.raw} differs from the engine's ratio {1 + stats.relative_change:.2f}x"
                )
        elif n.kind is NumberKind.STATISTIC:
            expected = stats.severity if n.unit and "severity" in n.unit else None
            if expected is None or abs(n.value - expected) > max(
                1.0, tol.relative_tolerance * expected
            ):
                v.append(ViolationCode.STATISTIC_OVERRIDE)
                if expected is not None:
                    contradicted = True
                reasons.append(f"{n.raw}: statistics are supplied only by the detection engine")
        elif n.kind in (NumberKind.PLAIN, NumberKind.COUNT) and (t.growth or t.search):
            v.append(ViolationCode.UNSUPPORTED_NUMBER)
            reasons.append(f"{n.raw}: no stored statistic matches this number")
    if any("significan" in s or "statistical" in s for s in t.statistical) and (
        stats.confidence_status is not SupportStatus.STATISTICALLY_SUPPORTED
    ):
        v.append(ViolationCode.STATISTIC_OVERRIDE)
        reasons.append("the engine has not established statistical significance")
    if t.growth or t.search:
        if not open_stage:
            if t.growth:
                v.append(ViolationCode.UNSUPPORTED_TREND)
                reasons.append("the statistical engine has no open surge episode for this query")
        elif t.search and t.youtube and not stats.measures_youtube_search:
            code = (
                ViolationCode.EXTERNAL_AS_YOUTUBE
                if stats.classification is SurgeClassification.EXTERNAL_SEARCH_SURGE
                else ViolationCode.PROXY_AS_SEARCH
            )
            v.append(code)
            reasons.append(
                "the evidence is not YouTube search interest: "
                + ", ".join(c.value for c in stats.evidence_categories)
            )
        elif t.search and not stats.measures_search:
            v.append(ViolationCode.PROXY_AS_SEARCH)
            reasons.append(
                "only indirect signals rose ("
                + ", ".join(c.value for c in stats.evidence_categories)
                + "); this is not measured search demand"
            )
        elif t.growth and stage_rank < STAGE_RANK[AlertStage.CANDIDATE]:
            v.append(ViolationCode.UNSUPPORTED_TREND)
            reasons.append("the episode is only under WATCH; growth is not established")
        if t.decline and open_stage:
            v.append(ViolationCode.UNSUPPORTED_TREND)
            contradicted = True
            reasons.append("the engine reports an increase, not a decline")
        if t.confirmed_wording and stats.highest_stage is not AlertStage.CONFIRMED and t.growth:
            v.append(ViolationCode.STATISTIC_OVERRIDE)
            reasons.append("the surge is not CONFIRMED by the engine")
    if stats_stale(stats, now, config):
        v.append(ViolationCode.STALE_EVIDENCE)
        reasons.append("the engine's last update is older than the freshness limit")
    if stats.synthetic:
        reasons.append("statistics come from synthetic test data")
    if contradicted:
        status = ClaimStatus.CONTRADICTED
    elif v:
        status = ClaimStatus.UNVERIFIED
    else:
        status = ClaimStatus.VERIFIED
    return TrendCheck(status, tuple(dict.fromkeys(v)), tuple(reasons))
