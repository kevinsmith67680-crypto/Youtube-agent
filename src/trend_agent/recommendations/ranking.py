"""Transparent recommendation ranking with explicit missing-evidence handling.

Seven components (0-1 each) keep their raw inputs. The composite is the weighted mean of
the *available* components x 100, multiplied by an evidence-quality factor (verification
status and Phase 5 evidence strength) so an attractive concept cannot compensate for weak
evidence. A missing component is excluded and listed (or set to 0.5 under a "neutral"
policy); it is never scored as zero growth or as a maximum. Weights are versioned,
experimental engineering choices; the score is a ranking index, not a probability of
success.
"""

from __future__ import annotations

from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.recommendations.context_builder import RecommendationContext
from trend_agent.recommendations.format_selector import CATALOG, intent_alignment
from trend_agent.schemas.recommendation import (
    Feasibility,
    PublishingUrgency,
    Ranking,
    RecommendationStatus,
    ScoreComponent,
    VideoFormat,
)

_DEMAND = {
    "confirmed_direct_youtube_search_surge": 1.0,
    "direct_search_candidate": 0.75,
    "external_search_surge": 0.55,
    "potential_youtube_demand_opportunity": 0.35,
    "cold_start_growth_candidate": 0.3,
    "watch": 0.2,
}
_GAP_TYPE = {
    "no_relevant_results": 1.0,
    "missing_specific_answer": 0.9,
    "version_mismatch": 0.9,
    "potentially_outdated": 0.75,
    "insufficient_practical_demonstration": 0.8,
    "format_gap": 0.7,
    "language_or_region_gap": 0.6,
    "low_relevance": 0.6,
    "no_verifiable_gap": 0.1,
}
_GAP_STRENGTH = {"strong": 1.0, "moderate": 0.75, "weak": 0.45}
_WINDOW = {"URGENT": 0.9, "TIMELY": 0.8, "LONGER_WINDOW": 0.7, "LIKELY_TOO_LATE": 0.1}


def _demand(ctx: RecommendationContext) -> tuple[float | None, dict[str, object], str]:
    v = _DEMAND.get(ctx.demand.basis)
    raw: dict[str, object] = {
        "basis": ctx.demand.basis,
        "stage": ctx.demand.alert_stage,
        "freshness": ctx.demand.freshness,
    }
    if v is None:
        return None, raw, "no Phase 4 demand evidence"
    if ctx.demand.freshness in ("stale", "expired"):
        v *= 0.7
    return round(v, 4), raw, f"Phase 4 demand basis {ctx.demand.basis} (not recomputed)"


def _gap(ctx: RecommendationContext) -> tuple[float | None, dict[str, object], str]:
    raw: dict[str, object] = {"gap_type": ctx.gap_type, "strength": ctx.gap_strength}
    if ctx.gap_type is None or ctx.gap_type == "insufficient_evidence":
        return None, raw, "no assessable content gap"
    v = _GAP_TYPE.get(ctx.gap_type, 0.5) * _GAP_STRENGTH.get(ctx.gap_strength or "", 0.45)
    return round(v, 4), raw, f"Phase 5 gap {ctx.gap_type} with {ctx.gap_strength} evidence"


def _forecast(
    ctx: RecommendationContext, feas: Feasibility
) -> tuple[float | None, dict[str, object], str]:
    f = ctx.forecast
    raw: dict[str, object] = {
        "status": f.status,
        "breakout_probability": f.breakout_probability,
        "calibration": f.breakout_calibration,
        "continuation_probability": f.continuation_probability,
        "window": feas.demand_window.status if feas.demand_window else None,
        "proxy": f.is_proxy,
    }
    if not f.available:
        return None, raw, "no usable forecast: excluded, not zero"
    parts = []
    if f.breakout_probability is not None and f.breakout_calibration == "CALIBRATED":
        parts.append(f.breakout_probability)
    if f.continuation_probability is not None:
        parts.append(f.continuation_probability)
    w = feas.demand_window
    if w is not None and w.status in _WINDOW:
        parts.append(_WINDOW[w.status])
    if not parts:
        return None, raw, "forecast has no calibrated probability or estimable window"
    v = sum(parts) / len(parts)
    note = "Phase 6 calibrated probability and/or opportunity window"
    if f.is_proxy:
        v *= 0.8
        note += " (proxy attention, discounted)"
    return round(v, 4), raw, note


def _creator(
    ctx: RecommendationContext, fmt: VideoFormat
) -> tuple[float | None, dict[str, object], str]:
    p = ctx.profile
    raw: dict[str, object] = {"profile": p.profile_id}
    v = 1.0 if (ctx.category or "") in p.preferred_categories else 0.3
    if p.preferred_formats:
        v = 0.7 * v + 0.3 * (1.0 if fmt in p.preferred_formats else 0.4)
    topics = [t.lower() for t in p.topic_preferences]
    if topics:
        hit = any(t in ctx.query.lower() for t in topics)
        v = 0.8 * v + 0.2 * (1.0 if hit else 0.5)
        raw["topic_match"] = hit
    return round(v, 4), raw, "category, format and topic preferences of the creator profile"


def _feasibility(feas: Feasibility) -> tuple[float | None, dict[str, object], str]:
    expected = (feas.estimated_hours_low + feas.estimated_hours_high) / 2
    raw: dict[str, object] = {
        "expected_hours": round(expected, 1),
        "complexity": feas.complexity.value,
        "fits_capacity": feas.fits_creator_capacity,
        "urgency": feas.urgency.value,
    }
    v = max(0.15, min(1.0, 1.0 - (expected - 3.0) / 30.0))
    if feas.fits_creator_capacity is False:
        v *= 0.5
    if feas.urgency is PublishingUrgency.LIKELY_TOO_LATE:
        v *= 0.4
    return round(v, 4), raw, "planning hours vs capacity and the demand window"


def _originality(
    ctx: RecommendationContext, fmt: VideoFormat
) -> tuple[float | None, dict[str, object], str]:
    fam = CATALOG[fmt].family
    family_names = {
        "install": "tutorial",
        "tutorial": "tutorial",
        "migration": "tutorial",
        "location": "guide",
        "event": "guide",
        "challenge": "guide",
        "comparison": "comparison",
        "benchmark": "comparison",
        "review": "review",
    }
    current = list(ctx.current_exact_formats)
    raw: dict[str, object] = {"current_exact_videos": len(current), "family": fam}
    if not ctx.competing_videos:
        return None, raw, "no inspected results to compare against"
    if not current:
        return 1.0, raw, "no current, exact-entity video in the inspected results"
    same = sum(1 for f in current if f == family_names.get(fam, fam))
    v = 1.0 - same / max(len(current), 1) * 0.8
    raw["same_format_current"] = same
    return round(v, 4), raw, "share of current exact-entity results already in this format"


def rank(
    ctx: RecommendationContext,
    fmt: VideoFormat,
    feas: Feasibility,
    status: RecommendationStatus,
    title_coverage: float,
    loaded: LoadedRecommendationConfig,
) -> Ranking:
    cfg = loaded.config.ranking
    align = intent_alignment(fmt, ctx.primary_intent, ctx.sub_intent)
    values = {
        "demand_evidence": _demand(ctx),
        "content_gap": _gap(ctx),
        "intent_alignment": (
            round(0.6 * align + 0.4 * title_coverage, 4),
            {"format_alignment": align, "title_query_coverage": title_coverage},
            "format serves the intent; title uses the query's words",
        ),
        "forecast_opportunity": _forecast(ctx, feas),
        "creator_relevance": _creator(ctx, fmt),
        "production_feasibility": _feasibility(feas),
        "concept_originality": _originality(ctx, fmt),
    }
    components: list[ScoreComponent] = []
    total = weight = 0.0
    missing: list[str] = []
    for name, w in cfg.weights.items():
        v, raw, note = values[name]
        available = v is not None
        if not available and cfg.missing_policy.get(name) == "neutral":
            v, note = 0.5, note + " (neutral 0.5 under the missing-evidence policy)"
        if v is None:
            missing.append(name)
            components.append(
                ScoreComponent(
                    name=name,
                    value=None,
                    weight=w,
                    contribution=None,
                    available=False,
                    raw=raw,  # type: ignore[arg-type]
                    note=note,
                )
            )
            continue
        total += w * v
        weight += w
        components.append(
            ScoreComponent(
                name=name,
                value=v,
                weight=w,
                contribution=round(w * v * 100, 2),
                available=available,
                raw=raw,  # type: ignore[arg-type]
                note=note,
            )
        )
    factor = cfg.evidence_quality_factor.get(status.value, 0.0)
    factor *= cfg.gap_evidence_factor.get(
        ctx.report.evidence_quality.value if ctx.report.evidence_quality else "weak", 0.8
    )
    reasons: list[str] = []
    if status not in (
        RecommendationStatus.APPROVED,
        RecommendationStatus.APPROVED_WITH_UNCERTAINTY,
    ):
        reasons.append(
            f"verification status {status.value}: shown as a research candidate, not ranked"
        )
    if weight < cfg.min_available_weight:
        reasons.append(
            f"only {weight:.0%} of the weighted evidence is available "
            f"(minimum {cfg.min_available_weight:.0%})"
        )
    if cfg.require_demand_evidence and values["demand_evidence"][0] is None:
        reasons.append("no Phase 4 demand evidence")
    eligible = not reasons
    composite = round(total / weight * 100 * factor, 1) if weight and eligible else None
    best = sorted(
        (c for c in components if c.contribution is not None), key=lambda c: -(c.contribution or 0)
    )[:2]
    explanation = (
        f"Ranked by {', '.join(c.name.replace('_', ' ') for c in best)}"
        + (
            f"; excluded as missing: {', '.join(m.replace('_', ' ') for m in missing)}"
            if missing
            else ""
        )
        + f"; evidence-quality factor {factor:.2f}."
        if eligible
        else "Not ranked: " + "; ".join(reasons) + "."
    )
    limitations = [f"{m.replace('_', ' ')} unavailable (excluded, not zero)" for m in missing]
    return Ranking(
        composite_score=composite,
        eligible=eligible,
        eligibility_reasons=tuple(reasons),
        version=cfg.version,
        components=tuple(components),
        available_weight=round(weight, 4),
        evidence_quality_factor=round(factor, 4),
        explanation=explanation,
        limitations=tuple(limitations),
    )
