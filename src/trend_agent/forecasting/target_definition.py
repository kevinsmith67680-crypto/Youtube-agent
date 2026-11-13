"""Independent prediction targets (labels), defined from observed values only.

All labels are computed from the series' own values — never from the detector's alert
status. The *reference state* (baseline, threshold, current level) uses only what was
visible at the prediction time T; the *outcome* uses the final known values (latest
revisions) of the periods that end in (T, T + H].

Breakout (task A), for prediction time T and horizon H:

    baseline B   = median of the values visible in the trailing baseline window before T
                   (7 days for hourly, 28 days for daily data)
    spread   s   = 1.4826 * MAD of the same values
    threshold θ  = B + max(r * B, a, k * s)          r = 0.5, a = 10 index points (0-100
                                                     indices only), k = 2
    applicable   = the current level (median of the last p visible values) is below θ
    label        = 1 if at least p consecutive future periods in (T, T+H] reach θ, else 0
                   (p = 3 for hourly, 1 for daily data); NaN if fewer than
                   ``min_label_coverage`` of the future periods were observed, or if
                   the window has not matured (the data does not reach T + H).

Continuation (task C): applicable when the current level is already at or above θ;
label = 1 if the median of the last quarter of the window (T, T+H] is still ≥ θ.

Trajectory (task B): log((y(T+H) + c) / (level + c)) — a baseline-free, scale-free change
of the series' own index (no absolute counts are implied).

Peak (task D): within (T, T + P], the offset of the highest final value; *right-censored*
when the maximum is at (or within ``peak_boundary_tolerance`` of) the last period of the
window — the peak may lie beyond it; *passed* when no future value exceeds the current
level.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from trend_agent.forecasting.config import Targets, granularity_hours
from trend_agent.forecasting.feature_store import SeriesMeta, Visible
from trend_agent.schemas.enums import ScaleType, TimeGranularity

TARGET_VERSION = "targets-1.1.0"
LEADS_HOURS = (2, 6, 8, 12, 24, 48, 72)  # publication leads used by opportunity windows
_BOUNDED = (ScaleType.CONSISTENT_INDEX, ScaleType.RELATIVE_PER_REQUEST)


def baseline_window(cfg: Targets, granularity: TimeGranularity) -> float:
    return float(cfg.baseline_window_hours.get(granularity, 168))


def bounded_index(meta: SeriesMeta) -> bool:
    return meta.scale_type in _BOUNDED


@dataclass(frozen=True)
class Reference:
    """What is known at T and used to define outcomes (no future information)."""

    baseline: float | None
    spread: float | None
    threshold: float | None
    level: float | None
    elevated: bool | None
    baseline_observations: int
    reason: str | None = None


def reference(vis: Visible, meta: SeriesMeta, cfg: Targets) -> Reference:
    step = granularity_hours(meta.granularity) or 24.0
    now = vis.as_of
    from trend_agent.forecasting.feature_store import hours

    t_now = hours(now)
    ok = np.isfinite(vis.values)
    window = (
        ok & (vis.t_end > t_now - baseline_window(cfg, meta.granularity)) & (vis.t_end <= t_now)
    )
    vals = vis.values[window]
    need = cfg.min_baseline_observations.get(meta.granularity, 10)
    if vals.size < need:
        return Reference(None, None, None, None, None, int(vals.size), "insufficient_baseline")
    base = float(np.median(vals))
    spread = float(1.4826 * np.median(np.abs(vals - base)))
    absolute = cfg.breakout_absolute_index if bounded_index(meta) else 0.0
    threshold = base + max(cfg.breakout_relative * base, absolute, cfg.breakout_noise_k * spread)
    p = cfg.breakout_persistence.get(meta.granularity, 1)
    recent = vis.values[ok][-p:]
    if recent.size == 0 or (t_now - float(vis.t_end[ok][-1])) > max(24.0, 3 * step):
        return Reference(base, spread, threshold, None, None, int(vals.size), "stale_level")
    level = float(np.median(recent))
    return Reference(base, spread, threshold, level, level >= threshold, int(vals.size))


def _future(
    t: np.ndarray, v: np.ndarray, start: float, end: float
) -> tuple[np.ndarray, np.ndarray]:
    m = (t > start + 1e-9) & (t <= end + 1e-9)
    return t[m], v[m]


def _matured(final_t: np.ndarray, window_end: float) -> bool:
    """The outcome window is over: data reaches its end. Gaps inside a matured window are
    handled by the coverage rule; a window that runs past the data is never labelled (a
    "no breakout" there could be a breakout that has not happened yet)."""
    return bool(final_t.size) and float(final_t.max()) >= window_end - 1e-6


def _run_reaches(values: np.ndarray, threshold: float, p: int) -> bool:
    run = 0
    for x in values:
        run = run + 1 if (math.isfinite(x) and x >= threshold) else 0
        if run >= p:
            return True
    return False


def outcome_labels(
    ref: Reference,
    final_t: np.ndarray,
    final_v: np.ndarray,
    t_now: float,
    meta: SeriesMeta,
    horizons: tuple[int, ...],
    cfg: Targets,
) -> dict[str, float]:
    """Labels for one prediction time (NaN = not applicable or not observable)."""
    out: dict[str, float] = {}
    step = granularity_hours(meta.granularity) or 24.0
    p = cfg.breakout_persistence.get(meta.granularity, 1)
    nan = float("nan")
    for h in horizons:
        for k in ("breakout", "continuation", "trajectory", "coverage"):
            out[f"{k}_{h}"] = nan
        if ref.threshold is None or ref.level is None:
            continue
        if h < step - 1e-9:
            continue  # not a whole number of periods: unsupported for this granularity
        ft, fv = _future(final_t, final_v, t_now, t_now + h)
        expected = max(1, round(h / step))
        observed = int(np.sum(np.isfinite(fv)))
        coverage = observed / expected
        out[f"coverage_{h}"] = coverage
        if coverage < cfg.min_label_coverage or not _matured(final_t, t_now + h):
            continue
        assert ref.elevated is not None
        if not ref.elevated:
            out[f"breakout_{h}"] = 1.0 if _run_reaches(fv, ref.threshold, min(p, expected)) else 0.0
        else:
            tail = max(1, round(expected * cfg.continuation_tail_fraction))
            tail_vals = fv[np.isfinite(fv)][-tail:]
            out[f"continuation_{h}"] = float(np.median(tail_vals) >= ref.threshold)
        at_h = fv[np.isclose(ft, t_now + h, rtol=0.0, atol=1e-6)] if ft.size else np.array([])
        if at_h.size and math.isfinite(at_h[0]):
            c = cfg.trajectory_offset
            out[f"trajectory_{h}"] = math.log((float(at_h[0]) + c) / (ref.level + c))
    # Peak within the peak window (right-censoring explicit).
    for k in ("peak_offset", "peak_censored", "peak_passed", "peak_coverage"):
        out[k] = nan
    for d in LEADS_HOURS:
        out[f"elevated_lead_{d}"] = nan
    if ref.threshold is None or ref.level is None:
        return out
    pw = cfg.peak_window_hours
    ft, fv = _future(final_t, final_v, t_now, t_now + pw)
    expected = max(1, round(pw / step))
    finite = np.isfinite(fv)
    out["peak_coverage"] = float(np.sum(finite)) / expected
    if (
        out["peak_coverage"] >= cfg.min_label_coverage
        and finite.any()
        and _matured(final_t, t_now + pw)
    ):
        vals = np.where(finite, fv, -np.inf)
        i = int(np.argmax(vals))
        vmax = float(vals[i])
        last = int(np.flatnonzero(finite)[-1])
        out["peak_offset"] = float(ft[i] - t_now)
        out["peak_censored"] = float(
            i == last or float(fv[last]) >= cfg.peak_boundary_tolerance * vmax
        )
        out["peak_passed"] = float(vmax < ref.level)
    for d in LEADS_HOURS:
        if d < step - 1e-9 or abs(d / step - round(d / step)) > 1e-9:
            continue  # this lead is not an observable period end for the granularity
        hit = final_v[np.isclose(final_t, t_now + d, rtol=0.0, atol=1e-6)]
        if hit.size and math.isfinite(hit[0]):
            out[f"elevated_lead_{d}"] = float(hit[0] >= ref.threshold)
    return out


def label_names(horizons: tuple[int, ...]) -> list[str]:
    names = [f"{k}_{h}" for h in horizons for k in ("breakout", "continuation", "trajectory")]
    names += [f"coverage_{h}" for h in horizons]
    names += ["peak_offset", "peak_censored", "peak_passed", "peak_coverage"]
    names += [f"elevated_lead_{d}" for d in LEADS_HOURS]
    return names


def definition(cfg: Targets, granularity: TimeGranularity) -> dict[str, object]:
    """Machine-readable target definition stored with every model."""
    return {
        "version": TARGET_VERSION,
        "baseline": (
            f"median of visible values in the trailing {baseline_window(cfg, granularity):.0f} h"
        ),
        "threshold": (
            f"B + max({cfg.breakout_relative}*B, {cfg.breakout_absolute_index} index points "
            f"(bounded indices), {cfg.breakout_noise_k}*1.4826*MAD)"
        ),
        "persistence": cfg.breakout_persistence.get(granularity, 1),
        "min_label_coverage": cfg.min_label_coverage,
        "continuation_tail_fraction": cfg.continuation_tail_fraction,
        "trajectory": (
            f"log((y(T+H) + {cfg.trajectory_offset}) / (level + {cfg.trajectory_offset}))"
        ),
        "peak_window_hours": cfg.peak_window_hours,
        "peak_censoring": (
            f"max at the last period or last value >= {cfg.peak_boundary_tolerance}*max"
        ),
        "independent_of_detector": True,
    }
