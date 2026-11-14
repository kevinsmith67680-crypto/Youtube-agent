"""Production feasibility: planning hours, requirements, creator fit and urgency.

Hours are planning assumptions from the format catalogue and the configured production
profiles, widened for unresolved research and for formats that need a verified result.
The creator is never assumed to own software or hardware they did not list. Urgency comes
from the Phase 6 opportunity window for the matching production profile; without one it is
UNKNOWN (never inferred from category or wording).
"""

from __future__ import annotations

from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.recommendations.context_builder import RecommendationContext
from trend_agent.recommendations.format_selector import FormatSpec
from trend_agent.schemas.recommendation import (
    Complexity,
    Feasibility,
    ProductionProfile,
    PublishingUrgency,
    WindowSummary,
)

_URGENCY = {
    "URGENT": PublishingUrgency.PUBLISH_SOON,
    "TIMELY": PublishingUrgency.WITHIN_DAYS,
    "LONGER_WINDOW": PublishingUrgency.FLEXIBLE,
    "LIKELY_TOO_LATE": PublishingUrgency.LIKELY_TOO_LATE,
}


def _fill(items: tuple[str, ...], topic: str, other: str | None) -> tuple[str, ...]:
    return tuple(i.format(topic=topic, other=other or "the comparison tool") for i in items)


def _owned(requirement: str, owned: list[str]) -> bool:
    low = requirement.lower()
    return any(o.strip() and o.strip().lower() in low for o in owned)


def evaluate(
    spec: FormatSpec, ctx: RecommendationContext, loaded: LoadedRecommendationConfig
) -> Feasibility:
    cfg = loaded.config.feasibility
    low, high = spec.hours
    unknown = [r for r in ctx.requirements if not r.established]
    research_extra = len(unknown) * cfg.unknown_requirement_hours
    high_mult = cfg.verified_result_testing_factor if spec.requires_verified_results else 1.0
    est_low = round(low + research_extra * 0.5, 1)
    est_high = round(high * high_mult + research_extra, 1)
    expected = (est_low + est_high) / 2
    profile = (
        ProductionProfile.QUICK_RESPONSE
        if expected <= cfg.profiles[ProductionProfile.QUICK_RESPONSE].hours * 1.5
        else ProductionProfile.STANDARD_TUTORIAL
        if expected <= cfg.profiles[ProductionProfile.STANDARD_TUTORIAL].hours * 1.5
        else ProductionProfile.DEEP_TECHNICAL_VIDEO
    )
    complexity = (
        Complexity.LOW
        if est_high <= 5
        else Complexity.MEDIUM
        if est_high <= 14
        else Complexity.HIGH
    )
    # the breakdown sums to the expected hours: the extra research for unknown requirements
    # (its mid-range share) is research time, the rest splits by the usual proportions
    extra = research_extra * 0.75
    base = max(expected - extra, 0.0)
    testing = base * 0.25 if spec.requires_verified_results else 0.0
    breakdown = {
        "research": round(base * 0.2 + extra, 1),
        "testing": round(testing, 1),
        "recording": round(base * 0.3, 1),
        "editing": round(base * 0.5 - testing, 1),
    }
    other = ctx.other_entity if "{other}" in " ".join(spec.software) else None
    software = _fill(spec.software, ctx.topic, other)
    hardware = _fill(spec.hardware, ctx.topic, other)
    p = ctx.profile
    owned = [*p.equipment, *p.skills]
    needs = [*software, *hardware]
    has: bool | None = None
    if owned and needs:
        has = all(_owned(n, owned) for n in needs)
    fits: bool | None = None
    if p.available_hours_per_week is not None:
        fits = expected <= p.available_hours_per_week
    window: WindowSummary | None = None
    reasons: list[str] = []
    want = cfg.window_profile.get(profile)
    if ctx.forecast.available:
        window = next((w for w in ctx.forecast.windows if w.production_profile == want), None)
    if window is not None and window.status in _URGENCY:
        urgency = _URGENCY[window.status]
        reasons.append(
            f"Phase 6 opportunity window for {want}: {window.status}"
            + (
                f" ({window.probability_elevated_at_publication:.0%} of comparable states still "
                "elevated at publication)"
                if window.probability_elevated_at_publication is not None
                else ""
            )
        )
        if ctx.forecast.is_proxy:
            reasons.append("the window is for proxy attention, not search demand")
    else:
        urgency = PublishingUrgency.UNKNOWN
        reasons.append(
            "no estimable demand window for this production profile"
            + ("" if ctx.forecast.available else " (no usable forecast)")
        )
        if ctx.time_sensitive:
            reasons.append("the search intent is time-sensitive, but no window is estimable")
    notes = [
        f"{loaded.config.feasibility.profiles[profile].hours:g} h is a planning assumption for "
        f"{profile.value}, not a guaranteed production time"
    ]
    if spec.requires_verified_results:
        notes.append("needs a recorded, verified result before anything is claimed")
    if has is None and needs:
        notes.append("equipment and software are not stated in the creator profile: unknown")
    if unknown:
        notes.append(f"{len(unknown)} information requirement(s) need research before filming")
    return Feasibility(
        complexity=complexity,
        production_profile=profile,
        profile_hours=cfg.profiles[profile].hours,
        estimated_hours_low=est_low,
        estimated_hours_high=est_high,
        breakdown_hours=breakdown,
        required_software=software,
        required_hardware=hardware,
        requires_verified_results=spec.requires_verified_results,
        creator_has_requirements=has,
        fits_creator_capacity=fits,
        demand_window=window,
        urgency=urgency,
        urgency_reasons=tuple(reasons),
        uncertain=urgency is PublishingUrgency.UNKNOWN or has is None or bool(unknown),
        notes=tuple(notes),
    )
