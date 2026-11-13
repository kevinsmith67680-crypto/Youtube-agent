"""Models, calibration, uncertainty, drift and metrics (deterministic, offline)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tests.unit.forecasting.helpers import CFG
from trend_agent.forecasting import calibration, drift
from trend_agent.forecasting.baselines import MAX_LOG_CHANGE, climatology, damped_trend
from trend_agent.forecasting.config import ForecastingConfig, horizon_supported
from trend_agent.forecasting.evaluation import (
    best_f1_threshold,
    classification_metrics,
    ece,
    paired_bootstrap,
    regression_metrics,
)
from trend_agent.forecasting.model_selection import feature_reference, lead_time, stability
from trend_agent.forecasting.prediction import build_model
from trend_agent.forecasting.training import LightGBMModel, LogisticModel
from trend_agent.forecasting.uncertainty import ConformalInterval, OODReference
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.forecast import CalibrationStatus

NAMES = ("a", "b", "c")


def _data(n: int = 2000, rate: float = 0.08, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    logit = math.log(rate / (1 - rate)) + 1.5 * x[:, 0] - 0.8 * x[:, 1]
    y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(float)
    x[rng.random(n) < 0.1, 2] = np.nan  # missing values stay missing
    return x, y


# ----------------------------------------------------------------------- models
def test_logistic_is_deterministic_and_roundtrips() -> None:
    x, y = _data()
    a = LogisticModel.fit(x, y, NAMES, c=0.5)
    b = LogisticModel.fit(x, y, NAMES, c=0.5)
    assert np.allclose(a.score(x), b.score(x))
    rebuilt = build_model({"model": a.as_dict()}, None)
    assert np.allclose(rebuilt.score(x), a.score(x))
    contrib = a.contributions(x[0])
    assert set(contrib) == set(NAMES)


def test_class_imbalance_is_balanced_then_calibrated() -> None:
    x, y = _data(n=4000, rate=0.04, seed=1)
    m = LogisticModel.fit(x[:2500], y[:2500], NAMES, c=0.5)
    raw = m.score(x[2500:])
    # class_weight="balanced" inflates raw scores well above the 4 % base rate ...
    assert raw.mean() > 3 * y.mean()
    cal = calibration.fit(raw, y[2500:], CFG.calibration)
    p = cal.apply(raw)
    assert p is not None
    # ... and calibration maps them back to the observed frequency
    assert abs(p.mean() - y[2500:].mean()) < 0.02
    assert cal.ece_after is not None
    assert cal.ece_before is not None
    assert cal.ece_after < cal.ece_before


def test_lightgbm_deterministic_text_roundtrip() -> None:
    x, y = _data(n=3000, rate=0.1, seed=2)
    params = {"num_leaves": 7, "learning_rate": 0.1, "min_data_in_leaf": 40, "verbose": -1,
              "deterministic": True, "num_threads": 1, "seed": 6}  # fmt: skip
    kw = {"objective": "binary", "max_rounds": 60, "early_stopping": 10}
    a = LightGBMModel.fit(x[:2000], y[:2000], x[2000:], y[2000:], NAMES, params, **kw)  # type: ignore[arg-type]
    b = LightGBMModel.fit(x[:2000], y[:2000], x[2000:], y[2000:], NAMES, params, **kw)  # type: ignore[arg-type]
    assert np.allclose(a.score(x), b.score(x))
    rebuilt = build_model({"model": a.as_dict()}, a.text)
    assert np.allclose(rebuilt.score(x), a.score(x))
    assert set(a.contributions(x[0])) == set(NAMES)
    with pytest.raises(Exception, match="text missing"):
        build_model({"model": a.as_dict()}, None)


def test_unknown_model_kind_refused() -> None:
    with pytest.raises(Exception, match="unknown model kind"):
        build_model({"model": {"kind": "pickle"}}, None)


# ------------------------------------------------------------------ calibration
def test_calibration_statuses_depend_on_support() -> None:
    rng = np.random.default_rng(3)
    s = rng.random(100)
    few = (rng.random(100) < 0.05).astype(float)
    c = calibration.fit(s, few, CFG.calibration)
    assert c.status is CalibrationStatus.INSUFFICIENT_CALIBRATION_DATA
    assert c.apply(s) is None  # no probability without enough positives
    y = (rng.random(300) < s.repeat(3)[:300]).astype(float)
    sig = calibration.fit(s.repeat(3)[:300], y, CFG.calibration)
    assert sig.method == "sigmoid"
    assert sig.status is CalibrationStatus.CALIBRATED
    big = rng.random(5000)
    yb = (rng.random(5000) < big**2).astype(float)
    iso = calibration.fit(big, yb, CFG.calibration)
    assert iso.method == "isotonic"
    p = iso.apply(big)
    assert p is not None
    assert np.all(np.diff(p[np.argsort(big)]) >= -1e-12)  # monotone
    assert calibration.Calibrator.from_dict(iso.as_dict()).apply(big) == pytest.approx(p)
    lo_hi = iso.interval(float(p[0]))
    assert lo_hi is not None
    assert lo_hi[0] <= lo_hi[1]


def test_ece_and_reliability() -> None:
    y = np.array([0, 0, 1, 1] * 50, dtype=float)
    assert ece(y, np.full(y.size, 0.5)) == pytest.approx(0.0, abs=1e-9)
    assert ece(y, np.where(y == 1, 0.0, 1.0)) == pytest.approx(1.0)


# ------------------------------------------------------------------ uncertainty
def test_conformal_interval_coverage_and_minimum() -> None:
    rng = np.random.default_rng(4)
    assert ConformalInterval.fit(rng.normal(size=10), np.zeros(10), 0.8).half_width is None
    y = rng.normal(size=4000)
    ci = ConformalInterval.fit(y[:2000], np.zeros(2000), 0.8)
    lo, hi = ci.bounds(0.0)
    assert lo is not None
    assert hi is not None
    cover = np.mean((y[2000:] >= lo) & (y[2000:] <= hi))
    assert 0.76 <= cover <= 0.84  # exchangeable data: close to the nominal level
    assert ConformalInterval.from_dict(ci.as_dict()).half_width == ci.half_width
    assert ci.bounds(float("nan")) == (None, None)


def test_out_of_distribution_detection() -> None:
    rng = np.random.default_rng(5)
    x = rng.normal(size=(500, 3))
    ref = OODReference.fit(x, NAMES, {"ai"}, {"hour"}, CFG.uncertainty)
    assert ref.assess(np.array([0.1, -0.2, 0.3]), "ai", "hour")["status"] == "in_distribution"
    far = ref.assess(np.array([40.0, 0.0, 0.0]), "ai", "hour")
    assert far["status"] == "out_of_distribution"  # 1/3 of features > reject share
    assert far["features"] == ["a"]
    novel = ref.assess(np.array([0.0, 0.0, 0.0]), "gaming", "hour")
    assert novel["status"] == "out_of_distribution"
    assert "category gaming" in novel["novel"][0]
    assert ref.assess(np.zeros(3), "ai", "day")["status"] == "out_of_distribution"
    missing = ref.assess(np.array([np.nan, 0.0, np.nan]), "ai", "hour")
    assert missing["status"] == "in_distribution"  # NaN is missing, not out of range
    again = OODReference.from_dict(ref.as_dict())
    assert again.assess(np.array([40.0, 0, 0]), "ai", "hour")["status"] == "out_of_distribution"


# ------------------------------------------------------------------------ drift
def test_psi_and_data_drift() -> None:
    rng = np.random.default_rng(6)
    train = rng.normal(size=(5000, 3))
    ref = feature_reference(train, NAMES)
    same = drift.psi(ref["a"], rng.normal(size=2000))
    shifted = drift.psi(ref["a"], rng.normal(loc=2.0, size=2000))
    assert same is not None
    assert shifted is not None
    assert same < 0.05
    assert shifted > 0.5
    stable = drift.data_drift(ref, rng.normal(size=(300, 3)), NAMES, CFG.drift)
    assert stable["status"] == "stable"
    moved = rng.normal(loc=3.0, size=(300, 3))
    report = drift.data_drift(ref, moved, NAMES, CFG.drift)
    assert report["status"] == "data_drift"
    assert set(report["alerts"]) == set(NAMES)
    few = drift.data_drift(ref, moved[:5], NAMES, CFG.drift)
    assert few["status"] == "insufficient_data"


def test_concept_drift_and_conservative_recommendation() -> None:
    rng = np.random.default_rng(7)
    y = (rng.random(500) < 0.1).astype(float)
    good = np.clip(np.where(y == 1, 0.6, 0.06), 0, 1)
    metrics = {"brier": 0.05, "base_rate": 0.1}
    ok = drift.concept_drift(y, good, model_metrics=metrics, cfg=CFG.drift)
    assert ok["status"] == "stable"
    bad = np.full(y.size, 0.9)
    worse = drift.concept_drift(y, bad, model_metrics=metrics, cfg=CFG.drift)
    assert worse["status"] == "worse_than_climatology"
    rec = drift.recommendation({"status": "stable"}, worse)
    assert rec["action"] == "revert_to_baseline"
    assert rec["automatic"] is False
    shifted_y = (rng.random(500) < 0.3).astype(float)
    shift = drift.concept_drift(
        shifted_y, np.where(shifted_y == 1, 0.7, 0.1), model_metrics=metrics, cfg=CFG.drift
    )
    assert shift["base_rate_shift"]
    assert drift.recommendation({}, shift)["action"] == "retrain"
    immature = drift.concept_drift(y[:10], good[:10], model_metrics=metrics, cfg=CFG.drift)
    assert immature["status"] == "insufficient_matured_labels"
    assert drift.recommendation({"status": "stable"}, immature)["action"] == "none"


# ---------------------------------------------------------------------- metrics
def test_paired_bootstrap_detects_better_model() -> None:
    rng = np.random.default_rng(8)
    y = (rng.random(3000) < 0.2).astype(float)
    good = np.where(y == 1, 0.7, 0.1)
    rate = np.full(y.size, 0.2)
    groups = np.repeat(np.arange(60), 50)
    d = paired_bootstrap(y, good, rate, groups, metric="brier", samples=200, seed=1, ci=0.9)
    assert d["upper"] is not None
    assert d["upper"] < 0
    same = paired_bootstrap(y, rate, rate, groups, metric="brier", samples=200, seed=1, ci=0.9)
    assert same["difference"] == 0


def test_classification_and_regression_metrics() -> None:
    y = np.array([0, 0, 0, 1, 1, 0, 1, 0, 0, 0], dtype=float)
    p = np.array([0.1, 0.2, 0.1, 0.9, 0.8, 0.3, 0.4, 0.2, 0.1, 0.05])
    m = classification_metrics(y, p, threshold=0.5, k=3)
    assert m["precision_at_3"] == pytest.approx(1.0)  # 0.9, 0.8, 0.4 are all positives
    m4 = classification_metrics(y, p, threshold=0.5, k=4)
    assert m4["precision_at_4"] == pytest.approx(0.75)
    assert m["false_positive_rate"] == 0
    assert m["pr_auc"] is not None
    thr = best_f1_threshold(y, p)
    assert 0.3 < thr <= 0.4
    r = regression_metrics(np.array([0.0, 1.0, 2.0]), np.array([0.0, 1.5, 2.0]))
    assert r["mae"] == pytest.approx(1 / 6, abs=1e-3)


def test_lead_time_and_stability() -> None:
    t = np.arange(10, dtype=float) * 6
    series = np.zeros(10)
    y = np.array([0, 0, 1, 1, 1, 0, 0, 0, 0, 0], dtype=float)
    p = np.array([0.1, 0.2, 0.7, 0.8, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
    lt = lead_time(t, series, y, p, 0.5)
    assert lt["episodes"] == 1
    assert lt["flagged_share"] == 1.0
    assert lt["mean_lead_hours"] == 12.0  # first alert 2 rows before the run's last row
    assert stability(t, series, p) == pytest.approx(np.mean(np.abs(np.diff(p))), abs=1e-5)


def test_baselines() -> None:
    assert damped_trend(float("nan"), 24, TimeGranularity.HOUR) == 0.0
    assert damped_trend(1.0, 72, TimeGranularity.HOUR) == MAX_LOG_CHANGE  # bounded
    assert 0 < damped_trend(0.001, 24, TimeGranularity.HOUR) < 0.024  # damped
    assert climatology(np.array([0.0, 1.0, np.nan, 1.0])) == pytest.approx(2 / 3)


# ----------------------------------------------------------------------- config
def test_configuration_validation() -> None:
    with pytest.raises(ValueError, match="positive"):
        ForecastingConfig(horizons_hours=(0, 24))
    with pytest.raises(ValueError, match="sum to 1"):
        ForecastingConfig(category_allocation={"ai": 0.5})
    with pytest.raises(ValueError, match="availability_mode"):
        ForecastingConfig(availability_mode="wallclock")
    with pytest.raises(ValueError, match="unknown_key"):
        ForecastingConfig.model_validate({"unknown_key": 1})
    assert horizon_supported(24, TimeGranularity.DAY)
    assert not horizon_supported(6, TimeGranularity.DAY)  # never interpolated
    assert not horizon_supported(36, TimeGranularity.DAY)
    assert not horizon_supported(24, TimeGranularity.MONTH)
