"""Historical baselines: what value was expected for a period, and how uncertain that is.

Both models use only observations from periods *before* the evaluated one (causal); the
evaluated value is never part of its own baseline.

**Robust rolling** (short or irregular history)::

    median_t = median of usable values in the W periods before t
    MAD_t    = median(|x_i - median_t|) over the same values
    sigma_t  = 1.4826 * MAD_t               (consistent with a normal sd)
    z_t      = (x_t - median_t) / (sigma_t + epsilon)

If MAD is zero (over half the window equals the median, e.g. a flat or heavily rounded
signal) the mean absolute deviation (x 1.2533) is used; if that is also ~0, or below the
configured floors, a floor is used (absolute floor per scale family and a fraction of the
level; for counts a Poisson floor sqrt(expected)). MAD-zero and floor-based dispersions are
flagged *unstable* and can never support a high-confidence alert.

**Seasonal profile** (established series): an additive weekday profile for daily data or
hour-of-day profile for hourly data - never a finer cycle than the data resolution. The
profile is refitted once per calendar block (ISO week for daily data, UTC day for hourly
data) on the ``window_cycles`` cycles before the block:

1. trend = centred moving median over one full cycle (seasonality-free);
2. profile s_p = median of (x - trend) per phase, centred;
3. Kruskal-Wallis test of a phase effect on the detrended values;
4. one-step-ahead errors of the seasonal predictor (level = median of the deseasonalised
   values over the previous ``level_cycles`` cycles, plus s_p) and of the robust rolling
   predictor are compared; the seasonal model is selected only if the phase effect is
   significant (p < alpha) **and** it reduces the robust error dispersion by at least
   ``min_dispersion_reduction``.

Selection is deterministic (same data -> same model) and its reason is recorded.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from numpy.lib.stride_tricks import sliding_window_view

from trend_agent.detection.config import (
    BaselineConfig,
    RobustBaselineConfig,
    ScaleFamily,
)
from trend_agent.detection.preprocessing import FloatArray, cycle_block, phase_of
from trend_agent.detection.statistical_tests import (
    kruskal_wallis,
    nan_median_rows,
    nan_stats_rows,
)
from trend_agent.schemas.detection import BaselineModel, BaselineReadiness, DispersionSource
from trend_agent.schemas.enums import TimeGranularity

MODEL_VERSIONS = {
    BaselineModel.ROBUST_ROLLING: "robust_rolling-v1",
    BaselineModel.SEASONAL_PROFILE: "seasonal_profile-v1",
    BaselineModel.NONE: "none",
}
MAD_SCALE = 1.4826
MEAN_ABS_SCALE = 1.2533
MEDIAN_SE = 1.2533  # standard error of a median ~ 1.2533 sigma / sqrt(n)
UNSTABLE_SOURCES = frozenset({DispersionSource.MEAN_ABS_DEV, DispersionSource.FLOOR})


@dataclass(frozen=True)
class BaselineEstimate:
    model: BaselineModel
    expected: float | None
    dispersion: float | None
    dispersion_source: DispersionSource | None
    sample_count: int
    readiness: BaselineReadiness
    resolution: TimeGranularity
    level: float | None = None
    seasonal_component: float | None = None
    expected_se: float | None = None
    selection_reason: str = ""

    @property
    def version(self) -> str:
        return MODEL_VERSIONS[self.model]

    @property
    def unstable(self) -> bool:
        return self.dispersion_source in UNSTABLE_SOURCES

    @property
    def usable(self) -> bool:
        return self.readiness is not BaselineReadiness.INSUFFICIENT and self.expected is not None

    def z(self, value: float, epsilon: float) -> float:
        assert self.expected is not None
        assert self.dispersion is not None
        return (value - self.expected) / (self.dispersion + epsilon)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.value,
            "model_version": self.version,
            "expected": _r(self.expected),
            "dispersion": _r(self.dispersion),
            "dispersion_source": self.dispersion_source.value if self.dispersion_source else None,
            "unstable_dispersion": self.unstable,
            "sample_count": self.sample_count,
            "readiness": self.readiness.value,
            "resolution": self.resolution.value,
            "level": _r(self.level),
            "seasonal_component": _r(self.seasonal_component),
            "expected_se": _r(self.expected_se),
            "selection_reason": self.selection_reason,
        }


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass(frozen=True)
class SeasonalFit:
    block: int
    eligible: bool
    selected: bool
    profile: tuple[float, ...]
    dispersion: float
    robust_dispersion: float
    reduction: float
    kw_statistic: float | None
    kw_p_value: float | None
    n: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "eligible": self.eligible,
            "selected": self.selected,
            "profile": [round(x, 6) for x in self.profile],
            "dispersion": round(self.dispersion, 6),
            "robust_dispersion": round(self.robust_dispersion, 6),
            "reduction": round(self.reduction, 6),
            "kw_statistic": _r(self.kw_statistic, 6),
            "kw_p_value": None if self.kw_p_value is None else float(f"{self.kw_p_value:.6g}"),
            "n": self.n,
            "reason": self.reason,
        }


def window_stats(
    values: FloatArray, indices: Iterable[int], width: int
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    """For each index i: (count, median, MAD, mean |dev|) of values[i-width:i] (NaN-aware)."""
    idx = np.asarray(list(indices), dtype=np.int64)
    if idx.size == 0:
        empty = np.zeros(0, dtype=np.float64)
        return empty, empty, empty, empty
    padded = np.concatenate([np.full(width, np.nan), values])
    windows = sliding_window_view(padded, width)[idx]
    counts, med, mad, mean_abs = nan_stats_rows(windows)
    return counts, med, mad, mean_abs


def dispersion_with_floors(
    mad: float,
    mean_abs: float,
    level: float,
    expected: float,
    family: ScaleFamily,
    cfg: RobustBaselineConfig,
) -> tuple[float, DispersionSource]:
    if mad > 0:
        sigma, source = MAD_SCALE * mad, DispersionSource.MAD
    elif mean_abs > 0:
        sigma, source = MEAN_ABS_SCALE * mean_abs, DispersionSource.MEAN_ABS_DEV
    else:
        sigma, source = 0.0, DispersionSource.FLOOR
    floor_abs = cfg.dispersion_floor_abs.get(family, 1.0)
    floor_rel = cfg.dispersion_floor_rel * abs(level)
    floor = max(floor_abs, floor_rel)
    poisson = float(np.sqrt(max(expected, 1.0))) if family is ScaleFamily.COUNT else 0.0
    if sigma >= max(floor, poisson):
        return sigma, source
    if poisson >= floor:
        return poisson, DispersionSource.POISSON
    return floor, DispersionSource.FLOOR


class BaselineModeler:
    """Computes baseline estimates for slots of one series (causal, deterministic)."""

    def __init__(
        self, cfg: BaselineConfig, granularity: TimeGranularity, family: ScaleFamily
    ) -> None:
        self.cfg = cfg
        self.granularity = granularity
        self.family = family
        self.window = cfg.robust.window_periods.get(granularity, 28)
        cycle = cfg.seasonal.cycle_periods.get(granularity) if cfg.seasonal.enabled else None
        self.cycle = cycle if cycle and cycle >= 2 else None

    # ----------------------------------------------------------------- seasonal
    def fit_block(self, values: FloatArray, first_slot: int, block: int) -> SeasonalFit:
        """Fit the seasonal profile for ``block`` from the cycles before it."""
        assert self.cycle is not None
        c = self.cycle
        s = self.cfg.seasonal
        r = self.cfg.robust
        block_start_slot = _block_start(block, self.granularity)
        end = block_start_slot - first_slot  # exclusive index: data strictly before the block
        start = max(0, end - s.window_cycles * c)

        def no(reason: str, n: int = 0) -> SeasonalFit:
            return SeasonalFit(
                block, False, False, (0.0,) * c, 0.0, 0.0, 0.0, None, None, n, reason
            )

        if end - start < s.min_cycles * c:
            return no(f"history {max(0, end - start)} periods < {s.min_cycles} cycles of {c}")
        seg = values[start:end]
        all_phases = phases_for(first_slot, len(values), self.granularity)
        phases = all_phases[start:end]
        valid = np.isfinite(seg)
        per_phase = np.array([np.count_nonzero(valid & (phases == p)) for p in range(c)])
        n = int(np.count_nonzero(valid))
        if per_phase.min() < s.min_per_phase:
            return no(f"fewest values in one phase {int(per_phase.min())} < {s.min_per_phase}", n)
        half = c // 2
        width = 2 * half + 1
        padded = np.concatenate([np.full(half, np.nan), seg, np.full(half, np.nan)])
        neigh = sliding_window_view(padded, width)
        trend, trend_n = nan_median_rows(np.ascontiguousarray(neigh))
        trend[trend_n < half + 1] = np.nan
        detrended = seg - trend
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            profile = np.array(
                [np.nanmedian(detrended[phases == p]) for p in range(c)], dtype=np.float64
            )
        if not np.all(np.isfinite(profile)):
            return no("profile undefined for some phase", n)
        profile = profile - float(np.median(profile))
        groups = [detrended[(phases == p) & np.isfinite(detrended)] for p in range(c)]
        groups = [g for g in groups if g.size >= 2]
        kw_h: float | None = None
        kw_p: float | None = None
        if len(groups) >= 2 and np.ptp(np.concatenate(groups)) > 0:
            kw_h, kw_p = kruskal_wallis(groups)
        # One-step-ahead errors of both predictors inside the fit window (causal).
        level_w = s.level_cycles * c
        idx = np.arange(start, end)
        deseason = values - profile[all_phases]
        cnt_l, lvl, _, _ = window_stats(deseason, idx, level_w)
        cnt_r, med_r, _, _ = window_stats(values, idx, self.window)
        e_seasonal = seg - (lvl + profile[phases])
        e_robust = seg - med_r
        ok_s = valid & (cnt_l >= c)
        ok_r = valid & (cnt_r >= r.provisional_min_samples)
        if np.count_nonzero(ok_s) < s.min_cycles or np.count_nonzero(ok_r) < s.min_cycles:
            return no("too few one-step errors to compare predictors", n)
        sig_s = MAD_SCALE * _mad(e_seasonal[ok_s])
        sig_r = MAD_SCALE * _mad(e_robust[ok_r])
        reduction = 1.0 - sig_s / sig_r if sig_r > 0 else 0.0
        significant = kw_p is not None and kw_p < s.significance_alpha
        selected = significant and reduction >= s.min_dispersion_reduction
        if selected:
            reason = (
                f"phase effect significant (Kruskal-Wallis H={kw_h:.1f}, p={kw_p:.2g}) and "
                f"one-step error dispersion reduced {reduction:.0%} vs robust rolling"
            )
        elif not significant:
            reason = "no significant phase effect" + (
                f" (Kruskal-Wallis p={kw_p:.2g})" if kw_p is not None else ""
            )
        else:
            reason = (
                f"phase effect significant but dispersion reduction {reduction:.0%} < "
                f"{s.min_dispersion_reduction:.0%}"
            )
        return SeasonalFit(
            block,
            True,
            selected,
            tuple(float(x) for x in profile),
            float(sig_s),
            float(sig_r),
            float(reduction),
            kw_h,
            kw_p,
            n,
            reason,
        )

    # ----------------------------------------------------------------- estimates
    def estimates(
        self,
        values: FloatArray,
        first_slot: int,
        indices: list[int],
        fits: dict[int, SeasonalFit],
    ) -> list[BaselineEstimate]:
        """Baselines for ``indices`` (positions in ``values``) using only earlier values."""
        r = self.cfg.robust
        counts, med, mad, mean_abs = window_stats(values, indices, self.window)
        level_counts: dict[int, tuple[float, float]] = {}
        if self.cycle is not None:
            c = self.cycle
            level_w = self.cfg.seasonal.level_cycles * c
            needed: dict[int, list[int]] = {}
            for i in indices:
                block = cycle_block(first_slot + i, self.granularity)
                assert block is not None
                if block not in fits:
                    fits[block] = self.fit_block(values, first_slot, block)
                if fits[block].selected:
                    needed.setdefault(block, []).append(i)
            phases = phases_for(first_slot, len(values), self.granularity)
            for block, idx in needed.items():
                profile = np.array(fits[block].profile)
                deseason = values - profile[phases]
                cnt, lvl, _, _ = window_stats(deseason, idx, level_w)
                for j, n_l, level in zip(idx, cnt, lvl, strict=True):
                    level_counts[j] = (float(n_l), float(level))
        out: list[BaselineEstimate] = []
        for k, i in enumerate(indices):
            n = int(counts[k])
            if i in level_counts and self.cycle is not None:
                n_l, level = level_counts[i]
                block = cycle_block(first_slot + i, self.granularity)
                assert block is not None
                fit = fits[block]
                if n_l >= self.cycle and np.isfinite(level):
                    phase = phase_of(first_slot + i, self.granularity)
                    assert phase is not None
                    component = fit.profile[phase]
                    expected = max(0.0, level + component)
                    sigma, source = dispersion_with_floors(
                        fit.dispersion / MAD_SCALE,
                        0.0,
                        level,
                        expected,
                        self.family,
                        r,
                    )
                    out.append(
                        BaselineEstimate(
                            model=BaselineModel.SEASONAL_PROFILE,
                            expected=expected,
                            dispersion=sigma,
                            dispersion_source=source,
                            sample_count=fit.n,
                            readiness=BaselineReadiness.READY,
                            resolution=self.granularity,
                            level=level,
                            seasonal_component=component,
                            expected_se=MEDIAN_SE * sigma / float(np.sqrt(max(n_l, 1.0))),
                            selection_reason=fit.reason,
                        )
                    )
                    continue
            if n < r.provisional_min_samples:
                out.append(
                    BaselineEstimate(
                        model=BaselineModel.NONE,
                        expected=None,
                        dispersion=None,
                        dispersion_source=None,
                        sample_count=n,
                        readiness=BaselineReadiness.INSUFFICIENT,
                        resolution=self.granularity,
                        selection_reason=(
                            f"{n} prior value(s) in the last {self.window} periods < "
                            f"{r.provisional_min_samples}: no statistical baseline"
                        ),
                    )
                )
                continue
            m = float(med[k])
            expected = max(0.0, m)
            sigma, source = dispersion_with_floors(
                float(mad[k]), float(mean_abs[k]), m, expected, self.family, r
            )
            readiness = (
                BaselineReadiness.READY if n >= r.min_samples else BaselineReadiness.PROVISIONAL
            )
            reason = f"robust rolling median of {n} prior values (window {self.window} periods)"
            if self.cycle is not None:
                block = cycle_block(first_slot + i, self.granularity)
                block_fit = fits.get(block) if block is not None else None
                if block_fit is not None:
                    reason += f"; seasonal model not used: {block_fit.reason}"
            out.append(
                BaselineEstimate(
                    model=BaselineModel.ROBUST_ROLLING,
                    expected=expected,
                    dispersion=sigma,
                    dispersion_source=source,
                    sample_count=n,
                    readiness=readiness,
                    resolution=self.granularity,
                    level=m,
                    expected_se=MEDIAN_SE * sigma / float(np.sqrt(n)),
                    selection_reason=reason,
                )
            )
        return out


def phases_for(first_slot: int, n: int, granularity: TimeGranularity) -> npt.NDArray[np.int64]:
    """Vectorised ``phase_of`` for slots first_slot .. first_slot + n - 1."""
    slots = np.arange(first_slot, first_slot + n, dtype=np.int64)
    if granularity is TimeGranularity.DAY:
        return (slots + 3) % 7
    if granularity is TimeGranularity.HOUR:
        return slots % 24
    raise ValueError(f"no seasonal phase for {granularity.value}")


def _mad(x: FloatArray) -> float:
    if x.size == 0:
        return 0.0
    m = float(np.median(x))
    return float(np.median(np.abs(x - m)))


def _block_start(block: int, granularity: TimeGranularity) -> int:
    if granularity is TimeGranularity.DAY:
        return block * 7 - 3
    if granularity is TimeGranularity.HOUR:
        return block * 24
    raise ValueError(f"no seasonal blocks for {granularity.value}")
