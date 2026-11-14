"""Dataset construction, temporal splits, eligibility, peaks and opportunity windows."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import numpy as np
import pytest

from tests.unit.forecasting.helpers import CFG, DLOADED, LOADED, at, bundle
from trend_agent.forecasting import eligibility
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.dataset_builder import Dataset, build_dataset
from trend_agent.forecasting.evaluation import chronological_split, rolling_origins
from trend_agent.forecasting.feature_store import PointInTimeSeries, hours
from trend_agent.forecasting.opportunity_window import windows
from trend_agent.forecasting.peak_estimation import STATE_FEATURES, AnalogModel, wilson
from trend_agent.forecasting.target_definition import LEADS_HOURS
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.forecast import (
    ForecastStatus,
    OpportunityTiming,
    OpportunityWindow,
    PeakEstimate,
    PeakStatus,
)
from trend_agent.schemas.monitoring import MeasurementCategory

M = MeasurementCategory


def _corpus() -> list[syn.SeriesBundle]:  # type: ignore[name-defined]
    return syn.materialise(syn.corpus(hourly=6, daily=3, hourly_days=45, daily_days=120, seed=11))


@lru_cache(maxsize=1)
def small() -> Dataset:
    return build_dataset(_corpus(), LOADED, DLOADED, source="synthetic", audit_rows=8)


# ------------------------------------------------------------------- dataset
def test_dataset_manifest_and_leakage_audit() -> None:
    ds = small()
    man = ds.manifest
    assert ds.n > 100
    assert man["rows"] == ds.n
    assert man["synthetic"] is True
    assert man["source"] == "synthetic"
    assert man["leakage_audit"]["checked"] == 8
    assert man["leakage_audit"]["mismatches"] == 0
    assert man["dataset_version"].startswith("ds-")
    assert man["label_counts"]["breakout_24"]["observed"] > 0
    assert set(np.unique(ds.meta["granularity"])) == {"hour", "day"}
    # daily rows never carry sub-daily labels
    daily = ds.meta["granularity"] == "day"
    assert np.all(np.isnan(ds.labels["breakout_6"][daily]))


def test_every_example_records_identity_availability_and_label_quality() -> None:
    ds = small()
    cols = ("series_id", "query", "query_id", "entity_id", "topic_cluster_id", "category",
            "provider", "measurement", "region", "t", "data_cutoff", "granularity")  # fmt: skip
    for col in cols:
        assert ds.meta[col].shape == (ds.n,)
    t = ds.meta["t"].astype(float)
    cutoff = ds.meta["data_cutoff"].astype(float)
    assert np.all(cutoff <= t + 1e-9)  # nothing published after T was available
    assert np.all(t - cutoff < 24 * 4)  # and the series was fresh at T (eligibility)
    assert set(ds.meta["measurement"]) == {"direct_youtube_search_interest"}
    for h in (6, 24, 72):  # label quality (coverage) and censoring travel with each row
        assert f"coverage_{h}" in ds.labels
    assert "peak_censored" in ds.labels
    assert ds.manifest["dataset_version"].startswith("ds-")


def test_dataset_is_reproducible_and_parallel_safe() -> None:
    a = small()
    b = build_dataset(_corpus(), LOADED, DLOADED, source="synthetic", audit_rows=8, workers=2)
    assert a.manifest["rows_hash"] == b.manifest["rows_hash"]
    assert a.manifest["dataset_version"] == b.manifest["dataset_version"]
    assert np.allclose(a.X, b.X, equal_nan=True)


def test_dataset_end_cutoff_drops_immature_labels() -> None:
    bundles = _corpus()[:2]
    end = bundles[0].points[0].period_end + timedelta(days=20)
    ds = build_dataset(bundles, LOADED, DLOADED, source="synthetic", audit_rows=0, end=end)
    full = build_dataset(bundles, LOADED, DLOADED, source="synthetic", audit_rows=0)
    t = ds.meta["t"].astype(float)
    assert t.max() <= hours(end)
    for h in (6, 24, 72):
        lab = ds.labels[f"breakout_{h}"]
        late = t > hours(end) - h
        # a label whose window has not matured by ``end`` is unknown, never guessed ...
        assert np.all(np.isnan(lab[late]))
    # ... although the complete data would have labelled those rows
    late_full = (full.meta["t"].astype(float) > hours(end) - 72) & (
        full.meta["t"].astype(float) <= hours(end)
    )
    assert np.isfinite(full.labels["breakout_72"][late_full]).any()


def test_chronological_split_with_embargo_and_unseen_groups() -> None:
    full = small()
    ds = full.subset(full.meta["granularity"] == "hour")  # splits are per granularity scope
    h = 24
    emb = h + CFG.splits.embargo_extra_hours
    sp = chronological_split(
        ds,
        train_fraction=0.6,
        calibration_fraction=0.2,
        embargo_hours=emb,
        unseen_group_fraction=0.34,
        seed=3,
    )
    t = ds.meta["t"].astype(float)
    masks = [sp.train, sp.validation, sp.calibration, sp.test]
    total = sum(m.astype(int) for m in masks)
    assert np.all(total <= 1)  # disjoint
    assert t[sp.train].max() < t[sp.validation].min() - emb + 1e-9
    assert t[sp.validation].max() < sp.boundaries["calibration"] - emb + 1e-9
    assert t[sp.calibration].max() < t[sp.test].min() - emb + 1e-9
    assert sp.unseen_groups
    unseen = np.isin(ds.meta["group"].astype(str), list(sp.unseen_groups))
    assert not np.any(unseen & (sp.train | sp.validation | sp.calibration))
    assert np.any(unseen & sp.test)
    assert set(sp.describe()["rows"]) == {"train", "validation", "calibration", "test"}


def test_rolling_origins_are_walk_forward() -> None:
    ds = small()
    t = ds.meta["t"].astype(float)
    folds = rolling_origins(ds, folds=3, embargo_hours=96)
    assert len(folds) == 3
    for train, ev in folds:
        assert t[train].max() + 96 <= t[ev].min() + 1e-9


# --------------------------------------------------------------- eligibility
def _check(spec: syn.Spec, h_after: float, horizon: int | None = None):  # type: ignore[no-untyped-def]
    b = bundle(spec)
    vis = PointInTimeSeries(b.points).at(at(spec, h_after))
    return eligibility.check(vis, b.meta, CFG, horizon)


def test_eligibility_states() -> None:
    ok = syn.Spec(key="el_ok", seed=1, periods=24 * 20)
    assert _check(ok, 24 * 20).status is ForecastStatus.READY
    short = syn.Spec(key="el_short", seed=2, periods=24 * 2)
    assert _check(short, 24 * 2).status is ForecastStatus.INSUFFICIENT_HISTORY
    stale = _check(ok, 24 * 30)  # ten days after the last observation
    assert stale.status is ForecastStatus.SOURCE_UNAVAILABLE
    daily = syn.Spec(key="el_day", seed=3, granularity=TimeGranularity.DAY, periods=90)
    assert _check(daily, 24 * 90 + 12, 6).status is ForecastStatus.UNSUPPORTED_GRANULARITY
    assert _check(daily, 24 * 90 + 12, 24).status is ForecastStatus.READY
    renorm = syn.Spec(key="el_norm", seed=4, periods=24 * 20, normalization_change_at=24 * 18)
    assert _check(renorm, 24 * 20).status is ForecastStatus.FORECAST_UNAVAILABLE
    gappy = syn.Spec(key="el_gap", seed=5, periods=24 * 20, missing_rate=0.6)
    assert _check(gappy, 24 * 20).status is ForecastStatus.INSUFFICIENT_HISTORY


def test_non_target_measurements_are_not_forecast() -> None:
    official = syn.Spec(key="el_off", seed=6, periods=24 * 20, measurement=M.OFFICIAL_EVENT_SIGNAL)
    res = _check(official, 24 * 20)
    assert res.status is ForecastStatus.SOURCE_UNAVAILABLE
    assert "not a forecast target" in res.reasons[0]
    video = bundle(syn.Spec(key="el_vid", seed=7, periods=10, measurement=M.VIDEO_ACTIVITY))
    assert eligibility.target_family(video.meta.measurement).value == "proxy_attention"  # type: ignore[union-attr]
    assert eligibility.source_classification(video.meta).value == "VIDEO_ACTIVITY"


# ---------------------------------------------------------- peaks and windows
def _analogs(
    *, n: int = 200, censored: float = 0.0, passed: float = 0.0, elevated: float = 0.9
) -> AnalogModel:
    rng = np.random.default_rng(0)
    k = len(STATE_FEATURES)
    cens = (np.arange(n) < censored * n).astype(float)
    pas = ((np.arange(n) >= censored * n) & (np.arange(n) < (censored + passed) * n)).astype(float)
    return AnalogModel(
        granularity="hour",
        window_hours=168,
        center=[0.0] * k,
        scale=[1.0] * k,
        states=rng.normal(scale=0.01, size=(n, k)),
        peak_offset=rng.uniform(10, 40, size=n),
        peak_censored=cens,
        peak_passed=pas,
        elevated={d: (rng.random(n) < elevated).astype(float) for d in LEADS_HOURS},
        k=60,
        min_analogs=25,
        max_distance=10.0,
    )


STATE = dict.fromkeys(STATE_FEATURES, 0.0)
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def test_peak_statuses_and_right_censoring() -> None:
    est = _analogs().peak(STATE)
    assert est.status is PeakStatus.ESTIMATED
    assert est.earliest_hours <= est.median_hours <= est.latest_hours  # type: ignore[operator]
    assert "not a lifetime peak" in est.note
    beyond = _analogs(censored=0.8).peak(STATE)
    assert beyond.status is PeakStatus.PEAK_LIKELY_BEYOND_HORIZON
    assert beyond.median_hours is None  # no fabricated timestamp for censored peaks
    assert _analogs(passed=0.8).peak(STATE).status is PeakStatus.PEAK_MAY_HAVE_PASSED
    few = _analogs(n=10).peak(STATE)
    assert few.status is PeakStatus.PEAK_NOT_ESTIMABLE
    far = _analogs().peak(dict.fromkeys(STATE_FEATURES, 1e3))
    assert far.status is PeakStatus.PEAK_NOT_ESTIMABLE
    restored = AnalogModel.from_dict(_analogs().as_dict())
    assert restored.peak(STATE).median_hours == est.median_hours


def _windows(
    model: AnalogModel | None,
    *,
    peak: PeakEstimate | None = None,
    relevant: bool = True,
    elevated_now: bool = False,
) -> dict[str, OpportunityWindow]:
    out = windows(model, STATE, prediction_time=T0, cfg=CFG.windows, peak=peak,
                  relevant=relevant, max_horizon=72, elevated_now=elevated_now)  # fmt: skip
    return {w.production_profile: w for w in out}


def test_window_profiles_and_statuses() -> None:
    w = _windows(_analogs(elevated=0.95))
    assert set(w) == {"QUICK_REACTION", "STANDARD_TUTORIAL", "DEEP_DIVE"}
    assert w["QUICK_REACTION"].earliest_publication == T0 + timedelta(hours=2)
    assert w["DEEP_DIVE"].earliest_publication == T0 + timedelta(hours=24)
    assert all(x.status is OpportunityTiming.LONGER_WINDOW for x in w.values())
    soon = PeakEstimate(status=PeakStatus.ESTIMATED, horizon_hours=168, median_hours=6.0,
                        earliest_hours=2.0, latest_hours=12.0, analogs=60)  # fmt: skip
    urgent = _windows(_analogs(elevated=0.95), peak=soon)
    assert urgent["QUICK_REACTION"].status is OpportunityTiming.URGENT
    late = _windows(_analogs(elevated=0.05), elevated_now=True)
    assert late["DEEP_DIVE"].status is OpportunityTiming.LIKELY_TOO_LATE
    # not elevated and rarely elevated later: no established demand, not "too late"
    quiet = _windows(_analogs(elevated=0.05))
    assert quiet["DEEP_DIVE"].status is OpportunityTiming.UNKNOWN
    na = _windows(_analogs(), relevant=False)
    assert all(x.status is OpportunityTiming.UNKNOWN for x in na.values())
    assert all(x.probability_elevated_at_publication is None for x in na.values())
    none = _windows(None)
    assert all(x.status is OpportunityTiming.UNKNOWN for x in none.values())
    p = w["STANDARD_TUTORIAL"]
    lo, hi = p.probability_interval or (None, None)
    prob = p.probability_elevated_at_publication
    assert lo is not None
    assert hi is not None
    assert prob is not None
    assert lo <= prob <= hi


def test_wilson_interval() -> None:
    assert wilson(0, 0) is None
    lo, hi = wilson(5, 10)  # type: ignore[misc]
    assert lo < 0.5 < hi
    assert wilson(10, 10)[1] == 1.0  # type: ignore[index]
    assert math.isclose(sum(wilson(3, 10)) / 2, 0.33, abs_tol=0.08)  # type: ignore[arg-type]


@pytest.mark.parametrize("lead", [3, 7, 30])
def test_windows_use_next_observable_lead(lead: int) -> None:
    p, _, n = _analogs(elevated=0.95).elevated_at(STATE, lead)
    assert p is not None
    assert n >= 25
