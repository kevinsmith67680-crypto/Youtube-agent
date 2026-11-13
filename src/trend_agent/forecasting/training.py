"""Model training and selection on chronological splits (separate from inference).

For one (target family, granularity, horizon, task) the trainer fits:

* breakout / continuation: climatology, a regularised logistic regression and a small
  LightGBM grid (early stopping on the inner validation segment). Scores are calibrated
  on the calibration segment and evaluated once on the untouched test segment;
* trajectory: persistence, seasonal naive, damped trend and a LightGBM regressor of the
  log change, with split-conformal intervals from calibration residuals.

Each candidate gets explicit acceptance checks (``acceptance``). LightGBM is VALIDATED only
if it beats the logistic baseline on held-out Brier score with a bootstrap interval that
excludes zero and does not lose precision@K; models trained on synthetic data stay
``data_origin = synthetic``. Models are serialised as plain parameters (logistic, calibrator)
and LightGBM's own text format — never pickle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from trend_agent.forecasting import calibration as cal_mod
from trend_agent.forecasting.baselines import climatology, damped_trend
from trend_agent.forecasting.config import ForecastingConfig
from trend_agent.forecasting.dataset_builder import Dataset
from trend_agent.forecasting.evaluation import (
    Split,
    average_precision,
    best_f1_threshold,
    classification_metrics,
    paired_bootstrap,
    regression_metrics,
    slices,
)
from trend_agent.forecasting.uncertainty import ConformalInterval
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.forecast import CalibrationStatus
from trend_agent.schemas.model_metadata import ModelType

_EPS = 1e-6


# --------------------------------------------------------------------------- models
@dataclass
class LogisticModel:
    names: tuple[str, ...]
    medians: list[float]
    mean: list[float]
    scale: list[float]
    coef: list[float]  # values then missingness indicators
    intercept: float
    c: float

    def design(self, x: np.ndarray) -> np.ndarray:
        miss = ~np.isfinite(x)
        filled = np.where(miss, np.array(self.medians), x)
        z = (filled - np.array(self.mean)) / np.array(self.scale)
        return np.hstack([z, miss.astype(float)])

    def score(self, x: np.ndarray) -> np.ndarray:
        logits = self.design(x) @ np.array(self.coef) + self.intercept
        out: np.ndarray = 1.0 / (1.0 + np.exp(-logits))
        return out

    def contributions(self, row: np.ndarray) -> dict[str, float]:
        d = self.design(row.reshape(1, -1))[0]
        k = len(self.names)
        coef = np.array(self.coef)
        return {n: float(coef[j] * d[j] + coef[k + j] * d[k + j]) for j, n in enumerate(self.names)}

    @classmethod
    def fit(cls, x: np.ndarray, y: np.ndarray, names: tuple[str, ...], c: float) -> LogisticModel:
        from sklearn.linear_model import LogisticRegression

        medians = [float(np.nanmedian(col)) if np.isfinite(col).any() else 0.0 for col in x.T]
        miss = ~np.isfinite(x)
        filled = np.where(miss, np.array(medians), x)
        mean = filled.mean(axis=0)
        scale = filled.std(axis=0)
        scale[scale < 1e-9] = 1.0
        z = np.hstack([(filled - mean) / scale, miss.astype(float)])
        lr = LogisticRegression(C=c, class_weight="balanced", max_iter=2000)
        lr.fit(z, y.astype(int))
        return cls(
            names,
            medians,
            mean.tolist(),
            scale.tolist(),
            lr.coef_[0].tolist(),
            float(lr.intercept_[0]),
            c,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "logistic",
            **{
                k: getattr(self, k)
                for k in ("names", "medians", "mean", "scale", "coef", "intercept", "c")
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LogisticModel:
        return cls(
            tuple(d["names"]),
            d["medians"],
            d["mean"],
            d["scale"],
            d["coef"],
            float(d["intercept"]),
            float(d["c"]),
        )


@dataclass
class LightGBMModel:
    names: tuple[str, ...]
    text: str  # LightGBM's own model format (loaded with Booster(model_str=...))
    best_iteration: int
    params: dict[str, Any]
    objective: str
    _booster: Any = field(default=None, repr=False)

    @property
    def booster(self) -> Any:
        if self._booster is None:
            import lightgbm as lgb

            self._booster = lgb.Booster(model_str=self.text)
        return self._booster

    def score(self, x: np.ndarray) -> np.ndarray:
        out: np.ndarray = self.booster.predict(x, num_iteration=self.best_iteration)
        return out

    def contributions(self, row: np.ndarray) -> dict[str, float]:
        contrib = self.booster.predict(
            row.reshape(1, -1), num_iteration=self.best_iteration, pred_contrib=True
        )[0]
        return {n: float(contrib[j]) for j, n in enumerate(self.names)}

    def importance(self) -> dict[str, float]:
        gains = self.booster.feature_importance(importance_type="gain")
        return {n: float(g) for n, g in zip(self.names, gains, strict=True)}

    @classmethod
    def fit(
        cls,
        x_train: np.ndarray,
        y_train: np.ndarray,
        x_val: np.ndarray,
        y_val: np.ndarray,
        names: tuple[str, ...],
        params: dict[str, Any],
        *,
        objective: str,
        max_rounds: int,
        early_stopping: int,
    ) -> LightGBMModel:
        import lightgbm as lgb

        p = dict(params)
        p["objective"] = objective
        p["seed"] = int(p.get("seed", 6))
        train = lgb.Dataset(x_train, label=y_train, feature_name=list(names), free_raw_data=False)
        valid = lgb.Dataset(x_val, label=y_val, reference=train, free_raw_data=False)
        booster = lgb.train(
            p,
            train,
            num_boost_round=max_rounds,
            valid_sets=[valid],
            callbacks=[lgb.early_stopping(early_stopping, verbose=False)],
        )
        best = int(booster.best_iteration or booster.current_iteration())
        return cls(names, booster.model_to_string(num_iteration=best), best, p, objective, booster)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "lightgbm",
            "names": list(self.names),
            "best_iteration": self.best_iteration,
            "params": self.params,
            "objective": self.objective,
        }


# ---------------------------------------------------------------------- results
@dataclass
class Candidate:
    name: str
    model_type: ModelType
    task: str  # breakout | continuation | trajectory
    model: Any  # LogisticModel | LightGBMModel | dict (baseline parameters)
    calibrator: cal_mod.Calibrator | None
    interval: ConformalInterval | None
    threshold: float | None
    metrics: dict[str, Any]
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    comparisons: dict[str, Any] = field(default_factory=dict)
    test_predictions: np.ndarray | None = None

    @property
    def validated(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


@dataclass
class TrainingReport:
    task: str
    horizon: int
    granularity: str
    family: str
    rows: dict[str, int]
    candidates: list[Candidate]
    selected: str | None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "horizon_hours": self.horizon,
            "granularity": self.granularity,
            "family": self.family,
            "rows": self.rows,
            "selected": self.selected,
            "notes": self.notes,
            "candidates": [
                {
                    "name": c.name,
                    "type": c.model_type.value,
                    "validated": c.validated,
                    "checks": c.checks,
                    "reasons": c.reasons,
                    "metrics": {k: v for k, v in c.metrics.items() if k != "reliability"},
                    "comparisons": c.comparisons,
                }
                for c in self.candidates
            ],
        }


def _masks(split: Split, applicable: np.ndarray) -> dict[str, np.ndarray]:
    return {
        k: getattr(split, k) & applicable for k in ("train", "validation", "calibration", "test")
    }


# ------------------------------------------------------------------ classification
def train_classifier(
    ds: Dataset, split: Split, *, task: str, horizon: int, cfg: ForecastingConfig
) -> TrainingReport:
    label = ds.labels[f"{task}_{horizon}"]
    applicable = np.isfinite(label)
    m = _masks(split, applicable)
    rows = {k: int(v.sum()) for k, v in m.items()}
    pos = {k: int(np.nansum(label[v])) for k, v in m.items()}
    granularity = str(ds.meta["granularity"][0]) if ds.n else "unknown"
    family = str(ds.meta["family"][0]) if ds.n else "unknown"
    report = TrainingReport(
        task,
        horizon,
        granularity,
        family,
        {**rows, **{f"{k}_positives": v for k, v in pos.items()}},
        [],
        None,
    )
    lg = cfg.lightgbm
    if rows["train"] < lg.min_training_examples or pos["train"] < lg.min_training_positives:
        report.notes.append(
            f"insufficient training data: {rows['train']} rows, {pos['train']} positives "
            f"(need {lg.min_training_examples} / {lg.min_training_positives})"
        )
        return report
    x, y = ds.X, label
    k = cfg.promotion.precision_at_k
    xt, yt = x[m["train"]], y[m["train"]]
    xv, yv = x[m["validation"]], y[m["validation"]]
    xc, yc = x[m["calibration"]], y[m["calibration"]]
    xs, ys = x[m["test"]], y[m["test"]]
    groups = ds.meta["series_id"][m["test"]]
    names = ds.feature_names

    def finish(
        c: Candidate, raw_cal: np.ndarray, raw_test: np.ndarray, raw_train: np.ndarray
    ) -> None:
        calib = cal_mod.fit(raw_cal, yc, cfg.calibration) if yc.size else None
        c.calibrator = calib
        if calib is not None and calib.status is CalibrationStatus.CALIBRATED:
            p_cal = calib.apply(raw_cal)
            p_test = calib.apply(raw_test)
            assert p_cal is not None
            assert p_test is not None
        else:
            p_cal, p_test = raw_cal, raw_test
        c.threshold = best_f1_threshold(yc, p_cal) if yc.size else 0.5
        c.metrics = classification_metrics(
            ys, p_test, threshold=c.threshold, k=k, bins=cfg.calibration.bins
        )
        c.metrics["train_pr_auc"] = average_precision(yt, raw_train)
        c.metrics["calibration_status"] = (
            calib.status.value if calib else CalibrationStatus.INSUFFICIENT_CALIBRATION_DATA.value
        )
        c.test_predictions = p_test
        age = ds.column("series_age_h")[m["test"]]
        meta_test = {k: ds.meta[k][m["test"]] for k in ("category", "provider", "granularity")}
        meta_test["history"] = np.where(age < math.log1p(336.0), "new_series", "established")
        c.metrics["slices"] = slices(
            meta_test, np.ones(ys.shape, dtype=bool), ys, p_test, c.threshold, k
        )
        report.candidates.append(c)

    base = climatology(np.concatenate([yt, yv]))
    clim = Candidate(
        "climatology", ModelType.CLIMATOLOGY, task, {"rate": base}, None, None, None, {}
    )
    finish(clim, np.full(yc.shape, base), np.full(ys.shape, base), np.full(yt.shape, base))
    clim.calibrator = None  # a constant cannot be calibrated; it is the reference
    clim.metrics["calibration_status"] = CalibrationStatus.NOT_APPLICABLE.value
    # logistic: C chosen on the validation segment (log loss)
    best_lr, best_loss = None, math.inf
    for c_val in (0.05, 0.3, 1.0):
        lr = LogisticModel.fit(xt, yt, names, c_val)
        pv = np.clip(lr.score(xv), _EPS, 1 - _EPS)
        loss = float(-np.mean(yv * np.log(pv) + (1 - yv) * np.log(1 - pv))) if yv.size else 0.0
        if loss < best_loss:
            best_lr, best_loss = lr, loss
    assert best_lr is not None
    lr_c = Candidate(
        "logistic_regression",
        ModelType.LOGISTIC_REGRESSION,
        task,
        best_lr,
        None,
        None,
        None,
        {},
        {"C": best_lr.c, "class_weight": "balanced"},
    )
    finish(lr_c, best_lr.score(xc), best_lr.score(xs), best_lr.score(xt))
    # LightGBM: small grid, early stopping on validation, chosen by validation PR-AUC
    neg, pos_n = float(np.sum(yt == 0)), float(np.sum(yt == 1))
    best_gbm, best_ap, best_params = None, -1.0, {}
    if yv.size and yv.sum() > 0:
        for g in lg.grid:
            params = {**lg.base, **g, "scale_pos_weight": min(10.0, math.sqrt(neg / max(pos_n, 1)))}
            gbm = LightGBMModel.fit(
                xt,
                yt,
                xv,
                yv,
                names,
                params,
                objective="binary",
                max_rounds=lg.max_rounds,
                early_stopping=lg.early_stopping_rounds,
            )
            ap = average_precision(yv, gbm.score(xv)) or 0.0
            if ap > best_ap:
                best_gbm, best_ap, best_params = gbm, ap, {**g, "rounds": gbm.best_iteration}
    if best_gbm is not None:
        gbm_c = Candidate(
            "lightgbm",
            ModelType.LIGHTGBM_CLASSIFIER,
            task,
            best_gbm,
            None,
            None,
            None,
            {},
            {**best_params, "validation_pr_auc": round(best_ap, 4)},
        )
        finish(gbm_c, best_gbm.score(xc), best_gbm.score(xs), best_gbm.score(xt))
    else:
        report.notes.append("LightGBM not trained: no validation positives for early stopping")
    acceptance(report, ys, groups, cfg)
    return report


def acceptance(
    report: TrainingReport, ys: np.ndarray, groups: np.ndarray, cfg: ForecastingConfig
) -> None:
    """Explicit, quantitative checks; a model failing any is not VALIDATED."""
    pr = cfg.promotion
    k = pr.precision_at_k
    by = {c.name: c for c in report.candidates}
    clim = by.get("climatology")
    lr = by.get("logistic_regression")
    for c in report.candidates:
        if c.model_type is ModelType.CLIMATOLOGY:
            c.checks = {"reference_only": False}
            c.reasons.append("climatology is the reference, not a deployable forecast")
            continue
        mt = c.metrics
        checks = {
            "enough_test_examples": mt["examples"] >= pr.min_test_examples,
            "enough_test_positives": mt["positives"] >= pr.min_test_positives,
            "calibrated": mt.get("calibration_status") == CalibrationStatus.CALIBRATED.value,
            "ece_within_limit": mt.get("ece") is not None and mt["ece"] <= pr.max_ece,
        }
        train_ap, test_ap = mt.get("train_pr_auc"), mt.get("pr_auc")
        gap = (train_ap - test_ap) if train_ap is not None and test_ap is not None else None
        mt["overfit_gap_pr_auc"] = round(gap, 4) if gap is not None else None
        if (
            clim is not None
            and c.test_predictions is not None
            and clim.test_predictions is not None
        ):
            cmp = paired_bootstrap(
                ys,
                c.test_predictions,
                clim.test_predictions,
                groups,
                metric="brier",
                samples=pr.bootstrap_samples,
                seed=17,
                ci=pr.ci_level,
            )
            c.comparisons["brier_vs_climatology"] = cmp
            skill = mt.get("brier_skill_vs_reference")
            if clim.metrics.get("brier"):
                skill = 1 - (mt["brier"] or 0) / clim.metrics["brier"]
                mt["brier_skill_vs_reference"] = round(skill, 4)
            checks["beats_climatology"] = (
                cmp["upper"] is not None and cmp["upper"] < 0 and (skill or 0) >= pr.min_brier_skill
            )
        if c.model_type is ModelType.LIGHTGBM_CLASSIFIER and lr is not None:
            assert c.test_predictions is not None
            assert lr.test_predictions is not None
            cmp = paired_bootstrap(
                ys,
                c.test_predictions,
                lr.test_predictions,
                groups,
                metric="brier",
                samples=pr.bootstrap_samples,
                seed=19,
                ci=pr.ci_level,
            )
            c.comparisons["brier_vs_logistic"] = cmp
            p_k, l_k = mt.get(f"precision_at_{k}"), lr.metrics.get(f"precision_at_{k}")
            checks["beats_logistic_brier"] = cmp["upper"] is not None and cmp["upper"] < 0
            checks["precision_at_k_not_worse"] = (
                p_k is not None and l_k is not None and p_k >= l_k - pr.max_precision_drop
            )
        # Overfitting = a large in-sample/held-out gap *and* no held-out advantage over the
        # model's reference (climatology for logistic, logistic for LightGBM). A large gap
        # alone is common for trees and is reported, not blocking.
        reference_ok = checks.get(
            "beats_logistic_brier"
            if c.model_type is ModelType.LIGHTGBM_CLASSIFIER
            else "beats_climatology",
            False,
        )
        checks["not_overfitted"] = gap is not None and (gap <= pr.max_overfit_gap or reference_ok)
        if gap is not None and gap > pr.max_overfit_gap:
            c.reasons.append(
                f"note: in-sample PR-AUC exceeds held-out PR-AUC by {gap:.2f} (diagnostic)"
            )
        c.checks = checks
        c.reasons += [f"failed: {name}" for name, ok in checks.items() if not ok]
    valid = [c for c in report.candidates if c.validated]
    if valid:
        report.selected = min(valid, key=lambda c: c.metrics["brier"]).name
    else:
        report.notes.append("no candidate met the acceptance criteria; baseline-only forecasts")


# ------------------------------------------------------------------- trajectory
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
    for name, p in preds.items():
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
    # acceptance: beat the best baseline on MAE (bootstrap CI excludes 0) with sane coverage
    baselines = [c for c in report.candidates if c.model_type is not ModelType.LIGHTGBM_REGRESSOR]
    ref = min(baselines, key=lambda c: c.metrics.get("mae") or math.inf)
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
            checks["beats_best_baseline"] = cmp["upper"] is not None and cmp["upper"] < 0
        else:
            checks["is_best_baseline"] = True
        c.checks = checks
        c.reasons += [f"failed: {n}" for n, ok in checks.items() if not ok]
    valid = [c for c in report.candidates if c.validated]
    report.selected = min(valid, key=lambda c: c.metrics["mae"]).name if valid else ref.name
    if not valid:
        report.notes.append(
            f"no candidate validated; {ref.name} is used as the baseline trajectory"
        )
    return report
