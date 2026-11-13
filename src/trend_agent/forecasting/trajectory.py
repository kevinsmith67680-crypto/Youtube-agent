"""Trajectory forecasting: baselines and LightGBM for the log change of the series' index.

Training (``train_trajectory``) fits, on the same chronological splits as the classifiers,
persistence, seasonal naive, damped trend and a LightGBM regressor (Huber loss) of
``log((y(T+H) + 1) / (level + 1))``, each with a split-conformal interval from calibration
residuals, and applies explicit acceptance checks: enough test examples, observed interval
coverage within 0.10 of nominal, and an MAE that beats the reference baseline (lowest
*calibration-period* MAE) with a series-block bootstrap interval excluding zero.

Inference (``forecast_points``) turns the predicted change back into an index value of the
same source, bounded to 0-100 for bounded indices, with the model's conformal interval or,
for unregistered persistence, empirical quantiles of the series' own past changes. Values
are relative interest of the measured source, never counts.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import numpy as np

from trend_agent.forecasting.baselines import damped_trend, seasonal_naive
from trend_agent.forecasting.config import ForecastingConfig
from trend_agent.forecasting.dataset_builder import Dataset
from trend_agent.forecasting.evaluation import Split, paired_bootstrap, regression_metrics
from trend_agent.forecasting.feature_engineering import FEATURE_NAMES
from trend_agent.forecasting.target_definition import bounded_index
from trend_agent.forecasting.training import Candidate, LightGBMModel, TrainingReport, _masks
from trend_agent.forecasting.uncertainty import ConformalInterval
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.forecast import TrajectoryPoint
from trend_agent.schemas.model_metadata import ModelType


def train_trajectory(
    ds: Dataset, split: Split, *, horizon: int, cfg: ForecastingConfig
) -> TrainingReport:
    y = ds.labels[f"trajectory_{horizon}"]
    applicable = np.isfinite(y)
    m = _masks(split, applicable)
    rows = {k: int(v.sum()) for k, v in m.items()}
    granularity = str(ds.meta["granularity"][0]) if ds.n else "unknown"
    family = str(ds.meta["family"][0]) if ds.n else "unknown"
    report = TrainingReport("trajectory", horizon, granularity, family, rows, [], None)
    if rows["train"] < cfg.lightgbm.min_training_examples:
        report.notes.append(f"insufficient training data: {rows['train']} rows")
        return report
    g = TimeGranularity(granularity) if granularity in ("hour", "day") else TimeGranularity.DAY
    slope = ds.column("slope_recent_log_per_h")
    seasonal = ds.baselines.get(f"seasonal_{horizon}", np.full(ds.n, np.nan))
    preds = {
        "persistence": np.zeros(ds.n),
        "seasonal_naive": np.where(np.isfinite(seasonal), seasonal, 0.0),
        "damped_trend": np.array([damped_trend(s, float(horizon), g) for s in slope]),
    }
    level = cfg.uncertainty.interval_level
    groups = ds.meta["series_id"][m["test"]]
    ys = y[m["test"]]
    pers_mae = float(np.mean(np.abs(ys))) if ys.size else None
    types = {
        "persistence": ModelType.PERSISTENCE,
        "seasonal_naive": ModelType.SEASONAL_NAIVE,
        "damped_trend": ModelType.DAMPED_TREND,
    }
    cal_mae: dict[str, float] = {}
    for name, p in preds.items():
        yc_, pc_ = y[m["calibration"]], p[m["calibration"]]
        cal_mae[name] = float(np.mean(np.abs(pc_ - yc_))) if yc_.size else math.inf
        interval = ConformalInterval.fit(y[m["calibration"]], p[m["calibration"]], level)
        lo = p[m["test"]] - (interval.half_width or np.nan)
        hi = p[m["test"]] + (interval.half_width or np.nan)
        c = Candidate(
            name,
            types[name],
            "trajectory",
            {"baseline": name},
            None,
            interval,
            None,
            regression_metrics(ys, p[m["test"]], reference_mae=pers_mae, lower=lo, upper=hi),
        )
        c.test_predictions = p[m["test"]]
        report.candidates.append(c)
    lg = cfg.lightgbm
    best, best_mae, best_params = None, math.inf, {}
    for grid in lg.grid:
        params = {**lg.base, **grid}
        gbm = LightGBMModel.fit(
            ds.X[m["train"]],
            y[m["train"]],
            ds.X[m["validation"]],
            y[m["validation"]],
            ds.feature_names,
            params,
            objective="huber",
            max_rounds=lg.max_rounds,
            early_stopping=lg.early_stopping_rounds,
        )
        mae = float(np.mean(np.abs(gbm.score(ds.X[m["validation"]]) - y[m["validation"]])))
        if mae < best_mae:
            best, best_mae, best_params = gbm, mae, {**grid, "rounds": gbm.best_iteration}
    if best is not None:
        p_cal = best.score(ds.X[m["calibration"]])
        interval = ConformalInterval.fit(y[m["calibration"]], p_cal, level)
        p_test = best.score(ds.X[m["test"]])
        lo = p_test - (interval.half_width or np.nan)
        hi = p_test + (interval.half_width or np.nan)
        c = Candidate(
            "lightgbm_regressor",
            ModelType.LIGHTGBM_REGRESSOR,
            "trajectory",
            best,
            None,
            interval,
            None,
            regression_metrics(ys, p_test, reference_mae=pers_mae, lower=lo, upper=hi),
            {**best_params, "validation_mae": round(best_mae, 4)},
        )
        c.test_predictions = p_test
        report.candidates.append(c)
    # acceptance: beat the reference baseline on MAE (bootstrap CI excludes 0) with sane
    # coverage. The reference baseline is the one with the lowest *calibration-period* MAE:
    # the untouched test period never chooses a model.
    baselines = [c for c in report.candidates if c.model_type is not ModelType.LIGHTGBM_REGRESSOR]
    ref = min(baselines, key=lambda c: cal_mae.get(c.name, math.inf))
    for c in baselines:
        c.metrics["calibration_mae"] = round(cal_mae.get(c.name, math.inf), 4)
    for c in report.candidates:
        cov = c.metrics.get("interval_coverage")
        checks = {
            "enough_test_examples": c.metrics.get("examples", 0) >= cfg.promotion.min_test_examples,
            "interval_coverage_plausible": cov is not None and abs(cov - level) <= 0.1,
        }
        if c is not ref and c.test_predictions is not None and ref.test_predictions is not None:
            cmp = paired_bootstrap(
                ys,
                c.test_predictions,
                ref.test_predictions,
                groups,
                metric="mae",
                samples=cfg.promotion.bootstrap_samples,
                seed=23,
                ci=cfg.promotion.ci_level,
            )
            c.comparisons[f"mae_vs_{ref.name}"] = cmp
            checks["beats_reference_baseline"] = cmp["upper"] is not None and cmp["upper"] < 0
        else:
            checks["is_reference_baseline"] = True
        c.checks = checks
        c.reasons += [f"failed: {n}" for n, ok in checks.items() if not ok]
    gbm_ok = next(
        (
            c
            for c in report.candidates
            if c.model_type is ModelType.LIGHTGBM_REGRESSOR and c.validated
        ),
        None,
    )
    report.selected = gbm_ok.name if gbm_ok is not None else ref.name
    if gbm_ok is None:
        report.notes.append(
            f"no candidate validated; {ref.name} is used as the baseline trajectory"
        )
    return report


def forecast_points(
    model: tuple[Any, Any, dict[str, Any]] | None,
    x: np.ndarray,
    vis: Any,
    meta: Any,
    ref: Any,
    h: int,
    cfg: ForecastingConfig,
) -> tuple[TrajectoryPoint, ...]:
    """Trajectory point at T + h from a registered champion ``(metadata, scorer, payload)``,
    or from unregistered persistence when ``model`` is None."""
    tm_meta: Any = model[0] if model is not None else None
    tm_model: Any = model[1] if model is not None else None
    tm_payload: dict[str, Any] = model[2] if model is not None else {}
    level = float(ref.level)
    name = tm_meta.model_type.value if tm_meta is not None else "persistence"
    if name == "lightgbm_regressor" and tm_model is not None:
        change = float(tm_model.score(x.reshape(1, -1))[0])
    elif name == "seasonal_naive":
        change = seasonal_naive(vis, meta.granularity, float(h))
        change = 0.0 if math.isnan(change) else change
    elif name == "damped_trend":
        slope = float(x[FEATURE_NAMES.index("slope_recent_log_per_h")])
        change = damped_trend(slope, float(h), meta.granularity)
    else:
        change = 0.0
    interval = (
        ConformalInterval.from_dict(tm_payload["interval"])
        if tm_meta is not None and tm_payload.get("interval")
        else None
    )
    method = interval.method if interval and interval.half_width is not None else "none"
    lo_c = hi_c = None
    if interval is not None and interval.half_width is not None:
        lo_c, hi_c = change - interval.half_width, change + interval.half_width
    else:
        emp = own_history_interval(vis, h, cfg.uncertainty.interval_level, meta.granularity)
        if emp is not None:
            lo_c, hi_c = change + emp[0], change + emp[1]
            method = "empirical_quantiles_of_own_history"

    def to_level(c: float | None) -> float | None:
        if c is None:
            return None
        v = (level + 1.0) * math.exp(c) - 1.0
        if bounded_index(meta):
            v = min(100.0, max(0.0, v))
        return round(max(0.0, v), 2)

    return (
        TrajectoryPoint(
            lead_hours=h,
            forecast_time=vis.as_of + timedelta(hours=h),
            value=to_level(change),
            lower=to_level(lo_c),
            upper=to_level(hi_c),
            interval_level=cfg.uncertainty.interval_level if lo_c is not None else None,
            interval_method=method,
            model=tm_meta.model_name if tm_meta is not None else "persistence (unregistered)",
        ),
    )


def own_history_interval(
    vis: Any, h: int, level: float, g: TimeGranularity
) -> tuple[float, float] | None:
    """Quantiles of the series' own past h-hour log changes (persistence residuals)."""
    ok = np.isfinite(vis.values)
    t, v = vis.t_end[ok], vis.values[ok]
    if t.size < 20:
        return None
    pos = {round(float(a), 3): float(b) for a, b in zip(t, v, strict=True)}
    changes = [
        math.log((pos[round(float(a) + h, 3)] + 1.0) / (float(b) + 1.0))
        for a, b in zip(t, v, strict=True)
        if round(float(a) + h, 3) in pos
    ]
    if len(changes) < 20:
        return None
    lo, hi = np.quantile(changes, [(1 - level) / 2, 1 - (1 - level) / 2])
    del g
    return float(lo), float(hi)


__all__ = ["forecast_points", "own_history_interval", "train_trajectory"]
