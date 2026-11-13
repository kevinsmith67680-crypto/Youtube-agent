"""Calibrated p-values, multiple-testing control and the empirical alert budget.

**p-values are never manufactured from heuristic scores.** A p-value exists only when a
calibration (``calibration.py``) fitted an empirical null distribution of the detector
statistic on a training split of the *same kind of data* (provider, granularity,
measurement, scale family, detector). ``p = (1 + #{null >= s}) / (n + 1)`` is a valid
(conservative) empirical p-value under the assumption that the evaluated statistic is
exchangeable with the null sample. Only scores in the stored upper tail get an exact
value; lower scores get the conservative p = 1. A calibration fitted on synthetic data is
applied to synthetic series only, never to real data.

**Batch FDR.** Within one detection run, all hypotheses with valid p-values form one
family; Benjamini-Hochberg (valid under independence and positive regression dependence -
typical of correlated queries in one topic) or Benjamini-Yekutieli (any dependence) is
applied with level q. This controls the expected false-discovery proportion *of that run*.
Repeating it every run is **not** an online FDR guarantee over time.

**Alert budget.** Independently of p-values, new notifications (episodes reaching
CANDIDATE or CONFIRMED) are capped per rolling 24 hours; the cap and the per-day false
alert target are reported with every run. When no calibration exists this is the only
error-rate control, and alerts are labelled EMPIRICALLY_THRESHOLDED.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy import stats

from trend_agent.schemas.detection import MultipleTestingMethod

EXTRAPOLATION_LIMIT = 0.01


def stratum_key(
    provider: str, granularity: str, measurement: str, family: str, detector: str
) -> str:
    return f"{provider}|{granularity}|{measurement}|{family}|{detector}"


@dataclass(frozen=True)
class NullDistribution:
    """Upper tail of a calibrated null distribution of one detector statistic.

    Within the observed tail the p-value is empirical: ``(1 + #{null >= s}) / (n + 1)``.
    Beyond the largest null value an empirical p-value cannot go below 1/(n+1), which would
    make FDR control over hundreds of hypotheses impossible. There the tail is extrapolated
    with a generalised Pareto distribution fitted to the exceedances over the tail
    threshold (peaks-over-threshold, the standard extreme-value approach) - but only if the
    fit passed a Kolmogorov-Smirnov check at calibration time; otherwise the conservative
    bound 1/(n+1) is used. Extrapolation never goes more than two orders of magnitude below
    1/(n+1). The basis is reported with every p-value.
    """

    stratum: str
    calibration_version: str
    source: str  # "synthetic" | "stored_history"
    n_total: int
    tail_threshold: float
    tail: npt.NDArray[np.float64]  # sorted ascending, all >= tail_threshold
    gpd_shape: float | None = None
    gpd_scale: float | None = None
    gpd_ok: bool = False

    def p_value(self, score: float) -> tuple[float, bool]:
        """(p, exact). Scores below the stored tail get the conservative p = 1."""
        p, basis = self.p_value_with_basis(score)
        return p, basis != "below_tail"

    def p_value_with_basis(self, score: float) -> tuple[float, str]:
        if score < self.tail_threshold or self.tail.size == 0:
            return 1.0, "below_tail"
        if score <= float(self.tail[-1]):
            count_ge = int(self.tail.size - np.searchsorted(self.tail, score, side="left"))
            return (1 + count_ge) / (self.n_total + 1), "empirical"
        floor = 1.0 / (self.n_total + 1)
        if self.gpd_ok and self.gpd_shape is not None and self.gpd_scale is not None:
            rate = self.tail.size / self.n_total
            sf = float(
                stats.genpareto.sf(
                    score - self.tail_threshold, self.gpd_shape, scale=self.gpd_scale
                )
            )
            # Extrapolate at most two orders of magnitude below the empirical resolution
            # (a short-tailed fit would otherwise claim p ~ 0 past its endpoint).
            return min(floor, max(rate * sf, floor * EXTRAPOLATION_LIMIT)), "gpd_extrapolated"
        return floor, "conservative_bound"

    def quantile(self, alpha: float) -> float | None:
        """Smallest score whose empirical exceedance probability is <= alpha (None: unknown)."""
        k = int(np.floor(alpha * (self.n_total + 1))) - 1  # allowed null exceedances
        if k < 0 or k >= self.tail.size:
            return None
        return float(self.tail[self.tail.size - 1 - k]) + 1e-12

    def applies_to(self, synthetic: bool) -> bool:
        return (self.source == "synthetic") == synthetic

    @classmethod
    def from_parameters(
        cls, stratum: str, version: str, source: str, params: dict[str, Any]
    ) -> NullDistribution:
        gpd = params.get("gpd") or {}
        return cls(
            stratum=stratum,
            calibration_version=version,
            source=source,
            n_total=int(params["n_total"]),
            tail_threshold=float(params["tail_threshold"]),
            tail=np.sort(np.asarray(params["tail"], dtype=np.float64)),
            gpd_shape=gpd.get("shape"),
            gpd_scale=gpd.get("scale"),
            gpd_ok=bool(gpd.get("ok", False)),
        )


@dataclass
class NullLookup:
    """Calibrations by stratum (only applied to data of the matching nature)."""

    nulls: dict[str, NullDistribution] = field(default_factory=dict)

    def get(self, key: str, *, synthetic: bool) -> NullDistribution | None:
        null = self.nulls.get(key)
        if null is None or not null.applies_to(synthetic):
            return None
        return null


@dataclass(frozen=True)
class Hypothesis:
    key: str
    p_value: float
    exact: bool


@dataclass
class FamilyResult:
    method: MultipleTestingMethod
    q: float
    family_size: int
    accepted: set[str]
    adjusted: dict[str, float]

    def is_accepted(self, key: str) -> bool:
        return key in self.accepted

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "q": self.q,
            "family_size": self.family_size,
            "accepted": len(self.accepted),
        }


def control(
    hypotheses: Sequence[Hypothesis], method: MultipleTestingMethod, q: float
) -> FamilyResult:
    """Apply BH / BY to one family; returns the accepted hypothesis keys."""
    if not hypotheses:
        return FamilyResult(method, q, 0, set(), {})
    ps = np.array([h.p_value for h in hypotheses], dtype=np.float64)
    adjusted = stats.false_discovery_control(
        ps, method="by" if method is MultipleTestingMethod.BY else "bh"
    )
    accepted = {h.key for h, a in zip(hypotheses, adjusted, strict=True) if a <= q}
    return FamilyResult(
        method,
        q,
        len(hypotheses),
        accepted,
        {h.key: float(a) for h, a in zip(hypotheses, adjusted, strict=True)},
    )


@dataclass
class AlertBudget:
    """Rolling 24 h cap on new notifications (CANDIDATE / CONFIRMED)."""

    limit_per_day: int
    used_last_24h: int = 0
    target_false_alerts_per_day: float = 2.0
    held_back: int = 0

    def allow(self) -> bool:
        return self.used_last_24h < self.limit_per_day

    def consume(self) -> None:
        self.used_last_24h += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit_per_day": self.limit_per_day,
            "used_last_24h": self.used_last_24h,
            "held_back": self.held_back,
            "target_false_alerts_per_day": self.target_false_alerts_per_day,
        }
