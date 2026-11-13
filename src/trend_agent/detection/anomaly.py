"""Rapid anomaly detector: a sharp *upward* deviation of one period from its baseline.

Score = robust z of the observation against the causal baseline (``baseline.py``); only
the positive part matters (a drop never produces an upward alert). Thresholds come from
the configuration per provider / granularity / category / readiness / scale family.

A threshold crossing is TRIGGERED only if the change is also meaningful in magnitude:
the absolute increase must reach ``min_absolute_effect`` and the observed value
``min_observed_level`` (a 0-100 index moving from 2 to 8 is statistically surprising
but not a meaningful surge). Otherwise the result is REJECTED with a reason.

Relative difference is reported only when the expected value is large enough for a ratio
to mean something. A p-value is attached only when a calibrated null distribution for
the stratum exists (``significance.NullDistribution``); otherwise it stays None.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trend_agent.detection.baseline import BaselineEstimate
from trend_agent.detection.config import AnomalyConfig, ResolvedThresholds, ScaleFamily
from trend_agent.detection.significance import NullDistribution
from trend_agent.schemas.detection import BaselineReadiness, Reason, ResultStatus

VERSION = "robust_anomaly-v1"
Z95 = 1.959964


@dataclass(frozen=True)
class AnomalyResult:
    status: ResultStatus
    z: float | None
    score: float | None  # max(z, 0)
    observed: float | None
    expected: float | None
    absolute_difference: float | None
    relative_difference: float | None
    alert_threshold: float | None
    watch_threshold: float | None
    p_value: float | None
    p_value_exact: bool
    reasons: tuple[str, ...]
    uncertainty: dict[str, Any]
    thresholds_applied: tuple[str, ...] = ()
    version: str = VERSION

    @property
    def triggered(self) -> bool:
        return self.status is ResultStatus.TRIGGERED

    @property
    def elevated(self) -> bool:
        return self.status in (ResultStatus.TRIGGERED, ResultStatus.WATCH)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "z": _r(self.z),
            "score": _r(self.score),
            "observed": _r(self.observed),
            "expected": _r(self.expected),
            "absolute_difference": _r(self.absolute_difference),
            "relative_difference": _r(self.relative_difference),
            "alert_threshold": self.alert_threshold,
            "watch_threshold": self.watch_threshold,
            "p_value": self.p_value,
            "p_value_exact": self.p_value_exact,
            "reasons": list(self.reasons),
            "uncertainty": self.uncertainty,
            "thresholds_applied": list(self.thresholds_applied),
            "version": self.version,
        }


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def not_applicable(
    status: ResultStatus, reason: str, observed: float | None = None
) -> AnomalyResult:
    return AnomalyResult(
        status, None, None, observed, None, None, None, None, None, None, False, (reason,), {}
    )


def detect(
    value: float | None,
    baseline: BaselineEstimate,
    thresholds: ResolvedThresholds,
    cfg: AnomalyConfig,
    family: ScaleFamily,
    *,
    epsilon: float,
    null: NullDistribution | None = None,
) -> AnomalyResult:
    if value is None:
        return not_applicable(ResultStatus.INSUFFICIENT_DATA, Reason.MISSING_VALUE.value)
    if not baseline.usable:
        return not_applicable(ResultStatus.INSUFFICIENT_DATA, Reason.COLD_START.value, value)
    assert baseline.expected is not None
    assert baseline.dispersion is not None
    expected = baseline.expected
    sigma = baseline.dispersion
    z = baseline.z(value, epsilon)
    diff = value - expected
    rel = diff / expected if expected >= cfg.min_expected_for_relative.get(family, 5.0) else None
    se = baseline.expected_se or 0.0
    predictive = (sigma**2 + se**2) ** 0.5
    uncertainty = {
        "dispersion": round(sigma, 4),
        "dispersion_source": baseline.dispersion_source.value
        if baseline.dispersion_source
        else None,
        "unstable_dispersion": baseline.unstable,
        "expected_interval_95": [round(expected - Z95 * se, 4), round(expected + Z95 * se, 4)],
        "prediction_interval_95": [
            round(max(0.0, expected - Z95 * predictive), 4),
            round(expected + Z95 * predictive, 4),
        ],
        # z if the expected value were at either end of its 95% interval
        "z_range_95": [round((diff - Z95 * se) / sigma, 3), round((diff + Z95 * se) / sigma, 3)],
    }
    score = max(z, 0.0)
    p_value, exact = (None, False)
    if null is not None:
        p_value, exact = null.p_value(score)
    reasons: list[str] = []
    if baseline.readiness is BaselineReadiness.PROVISIONAL:
        reasons.append(Reason.PROVISIONAL_BASELINE.value)
    if baseline.unstable:
        reasons.append(Reason.UNSTABLE_DENOMINATOR.value)
    if z <= -thresholds.watch_z:
        reasons.append(Reason.DOWNWARD_CHANGE.value)
    status = ResultStatus.NOT_TRIGGERED
    if z >= thresholds.watch_z:
        meaningful = True
        if diff < thresholds.min_absolute_effect:
            meaningful = False
            reasons.append(Reason.BELOW_MIN_EFFECT.value)
        if value < thresholds.min_observed_level:
            meaningful = False
            reasons.append(Reason.BELOW_MIN_LEVEL.value)
        if z >= thresholds.alert_z:
            status = ResultStatus.TRIGGERED if meaningful else ResultStatus.REJECTED
        elif meaningful:
            status = ResultStatus.WATCH
    return AnomalyResult(
        status=status,
        z=z,
        score=score,
        observed=value,
        expected=expected,
        absolute_difference=diff,
        relative_difference=rel,
        alert_threshold=thresholds.alert_z,
        watch_threshold=thresholds.watch_z,
        p_value=p_value,
        p_value_exact=exact,
        reasons=tuple(reasons),
        uncertainty=uncertainty,
        thresholds_applied=thresholds.applied,
    )
