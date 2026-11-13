"""Deterministic, evidence-backed forecast explanations (no LLM involved).

Every sentence is filled from structured values: the target definition, the model's
registered held-out metrics, the calibration record, the uncertainty assessment and the
feature contributions (LightGBM's TreeSHAP ``pred_contrib`` or logistic coefficient x
standardised value). Contributions describe the model's arithmetic, not causes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from trend_agent.forecasting.feature_engineering import SPECS
from trend_agent.schemas.forecast import (
    BreakoutForecast,
    FeatureContribution,
    ForecastExplanation,
    OpportunityWindow,
    TrajectoryPoint,
)
from trend_agent.schemas.model_metadata import DataOrigin, ModelMetadata

_SOURCE_TEXT = {
    "youtube_search_interest": "relative YouTube Search interest",
    "external_search_interest": "relative external (non-YouTube) search interest",
    "proxy_attention": "proxy attention (video/community activity — not search demand)",
}


def top_contributions(
    contributions: Mapping[str, float], values: Mapping[str, float | None], n: int = 5
) -> tuple[FeatureContribution, ...]:
    ranked = sorted(contributions.items(), key=lambda kv: -abs(kv[1]))[:n]
    return tuple(
        FeatureContribution(feature=k, value=values.get(k), contribution=round(v, 5))
        for k, v in ranked
        if abs(v) > 0
    )


def explain(
    *,
    query: str,
    family: str,
    horizon: int,
    granularity: str,
    breakout: BreakoutForecast | None,
    trajectory: Sequence[TrajectoryPoint],
    windows: Sequence[OpportunityWindow],
    model: ModelMetadata | None,
    contributions: Mapping[str, float],
    values: Mapping[str, float | None],
    uncertainty: Mapping[str, Any],
    limitations: Sequence[str],
    persistence: int,
) -> ForecastExplanation:
    src = _SOURCE_TEXT.get(family, family)
    if breakout is not None and breakout.applicable and breakout.threshold_index is not None:
        target = (
            f"Whether {src} for '{query}' reaches at least {breakout.threshold_index:.1f} "
            f"(baseline {breakout.baseline_index:.1f}) for {persistence} consecutive "
            f"{granularity} period(s) within the next {horizon} h."
        )
    elif breakout is not None and not breakout.applicable:
        target = (
            f"Interest is already elevated; the forecast concerns whether {src} for '{query}' "
            f"stays elevated over the next {horizon} h, and its trajectory."
        )
    else:
        target = f"The trajectory of {src} for '{query}' over the next {horizon} h."
    if model is not None:
        m = model.metrics or {}
        parts = []
        if m.get("brier") is not None:
            parts.append(f"held-out Brier {m['brier']}")
        if m.get("brier_skill_vs_reference") is not None:
            parts.append(f"skill vs climatology {m['brier_skill_vs_reference']}")
        if m.get("pr_auc") is not None:
            parts.append(f"PR-AUC {m['pr_auc']}")
        if m.get("mae") is not None:
            parts.append(f"MAE {m['mae']} (log change)")
        cmp = next(iter(model.metrics.get("comparisons", {}).values()), None) if False else None
        del cmp
        baseline = (
            f"{model.model_name} on its untouched test period: " + ", ".join(parts)
            if parts
            else f"{model.model_name}: no held-out metrics recorded"
        )
        validation = (
            f"model status {model.status.value}; trained on {model.data_origin.value} data"
            + (
                " — synthetic models are never production-validated"
                if model.data_origin is DataOrigin.SYNTHETIC
                else ""
            )
        )
    else:
        baseline = "no validated model for this scope: baseline output only"
        validation = "no validated model"
    if breakout is not None and breakout.probability is not None:
        calibration = (
            f"{breakout.calibration_status.value}: probability {breakout.probability:.2f}"
            + (
                f" (observed rate in its calibration bin: {breakout.probability_interval[0]:.2f}-"
                f"{breakout.probability_interval[1]:.2f})"
                if breakout.probability_interval
                else ""
            )
        )
    elif breakout is not None:
        calibration = f"{breakout.calibration_status.value}: no probability is reported"
    else:
        calibration = "not applicable"
    traj = next(iter(trajectory), None)
    unc = f"distribution check: {uncertainty.get('ood_status', 'unknown')}"
    if traj is not None and traj.lower is not None and traj.upper is not None:
        unc += (
            f"; {traj.lead_hours} h value {traj.value:.1f} with a nominal "
            f"{(traj.interval_level or 0):.0%} interval {traj.lower:.1f}-{traj.upper:.1f} "
            f"({traj.interval_method}; nominal, not guaranteed)"
        )
    statuses = {w.production_profile: w.status.value for w in windows}
    if statuses:
        reasoning = "; ".join(
            f"{k}: {v}"
            + (
                f" ({w.probability_elevated_at_publication:.0%} of comparable states still "
                "elevated at publication)"
                if (w := next(x for x in windows if x.production_profile == k))
                and w.probability_elevated_at_publication is not None
                else ""
            )
            for k, v in statuses.items()
        )
    else:
        reasoning = "no opportunity-window estimate"
    return ForecastExplanation(
        predicted_target=target,
        horizon=f"{horizon} h",
        data_type=src + (" (synthetic test data)" if "synthetic" in " ".join(limitations) else ""),
        top_features=top_contributions(contributions, values),
        baseline_comparison=baseline,
        calibration=calibration,
        uncertainty=unc,
        limitations=tuple(limitations),
        validation=validation,
        opportunity_reasoning=reasoning,
    )


def describe_feature(name: str) -> str:
    spec = SPECS.get(name)
    return f"{name} ({spec.unit}): {spec.definition}" if spec else name
