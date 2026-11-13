"""Simple baselines every model must beat (evaluated honestly, on the same test rows).

Trajectory baselines predict the log change of the series' own index over the horizon:

* persistence      — 0 (the next value stays at the current level);
* seasonal naive   — the value one season before the target time (24 h for hourly, 7 d for
                     daily data), using the newest such value that was visible at T;
* damped trend     — the recent robust slope extrapolated with geometric damping and
                     bounded to ±log(3), so a short burst never extrapolates without limit.

Breakout baselines: climatology (the training base rate) and a regularised logistic
regression (``training.fit_logistic``).
"""

from __future__ import annotations

import math

import numpy as np

from trend_agent.forecasting.feature_store import Visible, hours
from trend_agent.schemas.enums import TimeGranularity

MAX_LOG_CHANGE = math.log(3.0)


def seasonal_naive(vis: Visible, granularity: TimeGranularity, horizon: float) -> float:
    """log((y_season + 1) / (level + 1)), or NaN when no comparable season is visible."""
    season = 24.0 if granularity is TimeGranularity.HOUR else 168.0
    step = 1.0 if granularity is TimeGranularity.HOUR else 24.0
    ok = np.isfinite(vis.values)
    if not ok.any():
        return float("nan")
    t_last = float(vis.t_end[ok][-1])
    level = float(vis.values[ok][-1])
    target = hours(vis.as_of) + horizon
    k = 1
    while target - k * season > t_last + 1e-9:
        k += 1
    want = target - k * season
    hit = ok & (np.abs(vis.t_end - want) <= 0.5 * step)
    if not hit.any():
        return float("nan")
    y = float(vis.values[hit][-1])
    return math.log((y + 1.0) / (level + 1.0))


def damped_trend(slope_log_per_h: float, horizon: float, granularity: TimeGranularity) -> float:
    if not math.isfinite(slope_log_per_h):
        return 0.0
    phi = 0.97 if granularity is TimeGranularity.HOUR else 0.8  # per period
    step = 1.0 if granularity is TimeGranularity.HOUR else 24.0
    periods = horizon / step
    damp = phi * (1 - phi**periods) / (1 - phi)
    change = slope_log_per_h * step * damp
    return float(max(-MAX_LOG_CHANGE, min(MAX_LOG_CHANGE, change)))


def climatology(train_y: np.ndarray) -> float:
    y = train_y[np.isfinite(train_y)]
    return float(y.mean()) if y.size else float("nan")
