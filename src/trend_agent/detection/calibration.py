"""Empirical calibration of detector statistics on a training split (walk-forward).

Calibration answers "how often would this statistic be this large if nothing happened?"
using data known to contain no surge (synthetic null series, or stored history from a
period declared in-control). It produces, per stratum (provider, granularity,
measurement, scale family, detector):

* the **upper tail of the null distribution** of the anomaly score (robust z >= 0) and of
  the CUSUM statistic - used for empirical p-values (``significance.NullDistribution``);
* an **ARL table** for the CUSUM: alarms per period on null residual sequences for a grid
  of h at the configured k (autocorrelation and heavy tails included, because the real
  sequences are used), next to Siegmund's normal-theory approximation;
* an **exceedance table** for anomaly thresholds and the per-series rate implied by the
  false-alert budget (``target_false_alerts_per_day`` across the monitored scope).

Only the training split is ever used; thresholds are never tuned on the test split.
Calibrations fitted on synthetic data are flagged ``source = synthetic`` and are applied
to synthetic series only.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from scipy import stats

from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.detection.cusum import siegmund_arl0
from trend_agent.detection.engine import SeriesEngine
from trend_agent.detection.preprocessing import prepare
from trend_agent.detection.significance import NullDistribution, NullLookup, stratum_key
from trend_agent.schemas.detection import BaselineReadiness, DetectorType
from trend_agent.schemas.time_series import StoredPoint

H_GRID = (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 14.0)
K_GRID = (0.5, 0.75, 1.0, 1.25)
Z_GRID = (3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0)


@dataclass
class NullSample:
    anomaly: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    cusum: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    residuals: list[list[float | None]] = field(default_factory=list)
    series: int = 0
    periods: int = 0


def collect(
    points: Mapping[int, Sequence[StoredPoint]], loaded: LoadedDetectionConfig
) -> NullSample:
    """Run the engine over null series; keep statistics where the baseline is READY."""
    engine = SeriesEngine(loaded)
    sample = NullSample()
    for sid in sorted(points):
        series = prepare(list(points[sid]), loaded.config, already_known=True)
        if series.family is None:
            continue
        out = engine.run(series, None)
        seq: list[float | None] = []
        sample.series += 1
        for ev in out.evaluations:
            if ev.baseline.readiness is not BaselineReadiness.READY or ev.anomaly.z is None:
                seq.append(None)
                continue
            sample.periods += 1
            key = stratum_key(
                series.provider,
                series.granularity.value,
                series.measurement.value,
                series.family.value,
                DetectorType.ROBUST_ANOMALY.value,
            )
            sample.anomaly[key].append(max(ev.anomaly.z, 0.0))
            seq.append(ev.anomaly.z)
            if ev.cusum.statistic is not None:
                ckey = stratum_key(
                    series.provider,
                    series.granularity.value,
                    series.measurement.value,
                    series.family.value,
                    DetectorType.CUSUM.value,
                )
                sample.cusum[ckey].append(ev.cusum.statistic)
        sample.residuals.append(seq)
    return sample


def fit_tail(scores: Sequence[float], tail_quantile: float) -> dict[str, Any]:
    """Empirical upper tail + generalised Pareto fit of the exceedances (POT)."""
    arr = np.sort(np.asarray(scores, dtype=np.float64))
    threshold = float(np.quantile(arr, tail_quantile))
    tail = arr[arr >= threshold]
    gpd: dict[str, Any] = {"ok": False}
    excess = tail[tail > threshold] - threshold
    if excess.size >= 50 and float(np.ptp(excess)) > 0:
        shape, _, scale = stats.genpareto.fit(excess, floc=0.0)
        rate = tail.size / arr.size
        # Tail-calibration check (rounded index data have ties, so a KS test against a
        # continuous law rejects at any sample size): the fitted tail must predict the
        # observed number of exceedances at high in-sample quantiles within a factor 2.
        checks = []
        for q in (0.99, 0.995, 0.999):
            x = float(np.quantile(arr, q))
            observed = int(np.count_nonzero(arr >= x))
            predicted = (
                arr.size
                * rate
                * float(stats.genpareto.sf(max(x - threshold, 0.0), shape, scale=scale))
            )
            checks.append(
                {
                    "quantile": q,
                    "x": round(x, 4),
                    "observed": observed,
                    "predicted": round(predicted, 1),
                }
            )
        ok = shape < 0.5 and all(
            c["observed"] >= 5 and 0.5 <= c["predicted"] / c["observed"] <= 2.0 for c in checks
        )
        gpd = {
            "shape": round(float(shape), 6),
            "scale": round(float(scale), 6),
            "exceedances": int(excess.size),
            "checks": checks,
            "ok": bool(ok),
        }
    return {
        "n_total": int(arr.size),
        "tail_quantile": tail_quantile,
        "tail_threshold": round(threshold, 6),
        "tail": [round(float(x), 6) for x in tail],
        "max": round(float(arr[-1]), 6) if arr.size else None,
        "gpd": gpd,
    }


def cusum_arl_table(
    residuals: Sequence[Sequence[float | None]],
    *,
    k_grid: Sequence[float] = K_GRID,
    z_clip: float,
    max_gap: int,
    h_grid: Sequence[float] = H_GRID,
) -> list[dict[str, Any]]:
    """Empirical in-control ARL of the clipped one-sided CUSUM for each (k, h).

    The statistic is reset after every alarm, exactly as in detection; the real null
    residual sequences are used, so autocorrelation, heavy tails and rounding are included
    (Siegmund's value assumes i.i.d. N(0, 1) residuals and is shown for comparison).
    """
    rows = []
    for k in k_grid:
        for h in h_grid:
            if z_clip - k >= h:
                continue  # a single value could alarm alone: not allowed
            alarms = 0
            periods = 0
            for seq in residuals:
                g = 0.0
                gap = 0
                for z in seq:
                    if z is None:
                        gap += 1
                        continue
                    if gap > max_gap:
                        g = 0.0
                    gap = 0
                    periods += 1
                    g = max(0.0, g + min(z, z_clip) - k)
                    if g > h:
                        alarms += 1
                        g = 0.0
            rows.append(
                {
                    "k": k,
                    "h": h,
                    "alarms": alarms,
                    "periods": periods,
                    "arl0_empirical": round(periods / alarms, 1) if alarms else None,
                    "arl0_lower_bound": periods if not alarms else None,
                    "arl0_siegmund_normal": round(siegmund_arl0(k, h), 1),
                }
            )
    return rows


def exceedance_table(
    scores: Sequence[float], z_grid: Sequence[float] = Z_GRID
) -> list[dict[str, Any]]:
    arr = np.asarray(scores, dtype=np.float64)
    n = max(1, arr.size)
    return [
        {
            "z": z,
            "exceedances": int(np.count_nonzero(arr >= z)),
            "rate_per_series_period": round(float(np.count_nonzero(arr >= z)) / n, 6),
            "normal_theory_rate": round(0.5 * math.erfc(z / math.sqrt(2.0)), 8),
        }
        for z in z_grid
    ]


@dataclass
class CalibrationResult:
    version: str
    source: str
    dataset: str
    created_at: datetime
    strata: dict[str, dict[str, Any]]
    cusum_table: list[dict[str, Any]]
    anomaly_table: dict[str, list[dict[str, Any]]]
    recommended_h: float | None
    recommended_for_k: dict[str, float | None]
    budget_rate: float
    series: int
    periods: int
    train_start: datetime | None = None
    train_end: datetime | None = None

    def lookup(self) -> NullLookup:
        nulls = NullLookup()
        for key, params in self.strata.items():
            nulls.nulls[key] = NullDistribution.from_parameters(
                key, self.version, self.source, params
            )
        return nulls

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "dataset": self.dataset,
            "series": self.series,
            "periods": self.periods,
            "strata": {
                k: {
                    "n_total": v["n_total"],
                    "tail_threshold": v["tail_threshold"],
                    "max": v["max"],
                    "gpd": v.get("gpd"),
                }
                for k, v in self.strata.items()
            },
            "cusum_arl_table": self.cusum_table,
            "anomaly_exceedance": self.anomaly_table,
            "recommended_h_for_target_arl0": self.recommended_h,
            "recommended_h_by_k": self.recommended_for_k,
            "budget_rate_per_series_period": self.budget_rate,
            "train_range": [
                self.train_start.isoformat() if self.train_start else None,
                self.train_end.isoformat() if self.train_end else None,
            ],
        }


def calibrate(
    points: Mapping[int, Sequence[StoredPoint]],
    loaded: LoadedDetectionConfig,
    *,
    version: str,
    source: str,
    dataset: str,
    now: datetime,
    scope_series: int = 1000,
    periods_per_day: float = 1.0,
) -> CalibrationResult:
    cfg = loaded.config
    sample = collect(points, loaded)
    strata: dict[str, dict[str, Any]] = {}
    for key, scores in list(sample.anomaly.items()) + list(sample.cusum.items()):
        if len(scores) >= cfg.calibration.min_null_scores:
            strata[key] = fit_tail(scores, cfg.calibration.null_tail_quantile)
    table = cusum_arl_table(
        sample.residuals, z_clip=cfg.cusum.z_clip, max_gap=cfg.cusum.max_gap_periods
    )

    def smallest_h(k: float) -> float | None:
        for row in table:
            arl = row["arl0_empirical"] or row["arl0_lower_bound"] or 0
            if row["k"] == k and arl >= cfg.cusum.target_arl0:
                return float(row["h"])
        return None

    by_k = {str(k): smallest_h(k) for k in K_GRID}
    recommended = smallest_h(cfg.cusum.k)
    starts = [p.period_start for pts in points.values() for p in pts]
    budget_rate = cfg.multiple_testing.target_false_alerts_per_day / (
        scope_series * periods_per_day
    )
    return CalibrationResult(
        version=version,
        source=source,
        dataset=dataset,
        created_at=now,
        strata=strata,
        cusum_table=table,
        anomaly_table={k: exceedance_table(v) for k, v in sample.anomaly.items()},
        recommended_h=recommended,
        recommended_for_k=by_k,
        budget_rate=round(budget_rate, 8),
        series=sample.series,
        periods=sample.periods,
        train_start=min(starts) if starts else None,
        train_end=max(starts) if starts else None,
    )
