"""Independent targets and point-in-time features (leakage, missingness, comparability)."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np

from tests.unit.forecasting.helpers import CFG, DLOADED, LOADED, at, bundle
from trend_agent.forecasting import synthetic as syn
from trend_agent.forecasting.dataset_builder import _support_views, row_features
from trend_agent.forecasting.feature_engineering import FEATURE_NAMES, SPECS
from trend_agent.forecasting.feature_store import ContentEvent, PointInTimeSeries, hours
from trend_agent.forecasting.target_definition import Reference, label_names, outcome_labels
from trend_agent.schemas.enums import ScaleType, TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory

M = MeasurementCategory


def _meta(g: TimeGranularity = TimeGranularity.HOUR):  # type: ignore[no-untyped-def]
    return bundle(syn.Spec(key="meta", seed=1, periods=10, granularity=g)).meta


def test_breakout_needs_persistence_and_threshold() -> None:
    ref = Reference(
        baseline=20.0,
        spread=2.0,
        threshold=30.0,
        level=21.0,
        elevated=False,
        baseline_observations=100,
    )
    t0 = 1000.0
    t = np.arange(t0 + 1, t0 + 25)
    v = np.full(t.size, 20.0)
    v[3:5] = 35.0  # only two consecutive hours above: not a breakout (p = 3)
    lab = outcome_labels(ref, t, v, t0, _meta(), (6, 24), CFG.targets)
    assert lab["breakout_6"] == 0.0
    v[10:13] = 35.0
    lab = outcome_labels(ref, t, v, t0, _meta(), (6, 24), CFG.targets)
    assert lab["breakout_24"] == 1.0
    assert lab["breakout_6"] == 0.0
    assert math.isnan(lab["continuation_24"])  # not applicable when not elevated


def test_unobserved_future_gives_no_label() -> None:
    ref = Reference(20.0, 2.0, 30.0, 21.0, False, 100)
    t = np.arange(1001.0, 1025.0)
    v = np.full(t.size, np.nan)
    v[:5] = 40.0
    lab = outcome_labels(ref, t, v, 1000.0, _meta(), (24,), CFG.targets)
    assert math.isnan(lab["breakout_24"])  # 5/24 observed < min coverage
    assert lab["coverage_24"] < CFG.targets.min_label_coverage


def test_peak_censoring_and_continuation() -> None:
    ref = Reference(20.0, 2.0, 30.0, 40.0, True, 100)
    t = np.arange(1001.0, 1073.0)
    rising = np.linspace(40, 90, t.size)
    lab = outcome_labels(ref, t, rising, 1000.0, _meta(), (24,), CFG.targets)
    assert lab["peak_censored"] == 1.0  # still rising at the window's end
    assert lab["continuation_24"] == 1.0
    falling = np.linspace(39, 2, t.size)
    lab = outcome_labels(ref, t, falling, 1000.0, _meta(), (24,), CFG.targets)
    assert lab["peak_passed"] == 1.0
    assert lab["continuation_24"] == 0.0


def test_daily_series_has_no_sub_daily_labels() -> None:
    ref = Reference(20.0, 2.0, 30.0, 21.0, False, 30)
    t = 1000.0 + 24 * np.arange(1, 5)
    v = np.full(t.size, 25.0)
    lab = outcome_labels(ref, t, v, 1000.0, _meta(TimeGranularity.DAY), (6, 24, 72), CFG.targets)
    assert math.isnan(lab["breakout_6"])
    assert math.isnan(lab["elevated_lead_2"])
    assert not math.isnan(lab["elevated_lead_24"])


def test_labels_never_appear_as_features() -> None:
    assert not set(label_names(CFG.horizons_hours)) & set(FEATURE_NAMES)
    assert all(SPECS[n].availability for n in FEATURE_NAMES)


def _features(b, t):  # type: ignore[no-untyped-def]
    pit = PointInTimeSeries(b.points)
    return row_features(b, pit, None, _support_views(b, pit.mode), t, LOADED, DLOADED,
                        strict=True)[2]  # fmt: skip


def test_future_perturbation_does_not_change_features() -> None:
    spec = syn.Spec(key="leak", seed=5, periods=24 * 14)
    b = bundle(spec)
    t = at(spec, 24 * 10)
    before = _features(b, t).vector()
    altered = [p.model_copy(update={"value": 99.0}) if p.provider_published_at > t else p
               for p in b.points]  # fmt: skip
    after = _features(replace(b, points=altered), t).vector()
    assert np.allclose(before, after, equal_nan=True)


def test_missing_values_stay_missing_and_content_after_t_ignored() -> None:
    spec = syn.Spec(key="miss", seed=6, periods=24 * 12, missing_rate=0.1)
    b = bundle(spec)
    t = at(spec, 24 * 11)
    b.content = [ContentEvent(t + syn.timedelta(hours=1), "missing_specific_answer", "weak", True)]
    row = _features(b, t)
    assert math.isnan(row.values["gap_strength_ord"])  # analysis completed after T
    assert math.isnan(row.values["keyword_age_h"])  # no catalog fact: NaN, not 0
    assert row.values["missing_rate"] > 0
    assert all(isinstance(v, float) for v in row.values.values())


def test_scale_dependent_features_disabled_for_batch_relative_scales() -> None:
    spec = syn.Spec(key="rel", seed=7, periods=24 * 12, scale=ScaleType.RELATIVE_PER_REQUEST)
    row = _features(bundle(spec), at(spec, 24 * 11))
    assert math.isnan(row.values["level_index"])
    assert math.isnan(row.values["abs_change_24h"])
    assert not math.isnan(row.values["change_24h_log"])
    assert any("scale-dependent" in r for r in row.rejected)


def test_irregular_sampling_uses_actual_timestamps() -> None:
    spec = syn.Spec(
        key="irr",
        seed=8,
        periods=24 * 16,
        base=40,
        noise=0.0,
        ar=0.0,
        diurnal=0.0,
        weekly=0.0,
        events=(syn.Event("adoption", 24 * 9, 1.0, length_h=200),),
    )
    b = bundle(spec)
    t = at(spec, 24 * 9 + 110)  # mid-adoption: steady growth
    full = _features(b, t).values["slope_recent_log_per_h"]
    sparse = replace(b, points=[p for i, p in enumerate(b.points) if i % 3 == 0])
    thin = _features(sparse, t).values["slope_recent_log_per_h"]
    assert full > 0
    assert thin > 0
    assert abs(full - thin) / full < 0.6  # same slope per hour despite 3x fewer samples


def test_age_features_are_capped() -> None:
    spec = syn.Spec(key="age", seed=9, periods=24 * 50)
    v = _features(bundle(spec), at(spec, 24 * 49)).values["series_age_h"]
    assert v <= math.log1p(720) + 1e-9


def test_external_support_change_is_within_series() -> None:
    spec = syn.Spec(key="sup", seed=10, periods=24 * 12)
    ext = syn.Spec(key="sup_ext", seed=11, periods=24 * 12, measurement=M.EXTERNAL_SEARCH_INTEREST)
    b = bundle(spec, 3, {M.EXTERNAL_SEARCH_INTEREST: ext})
    row = _features(b, at(spec, 24 * 11))
    assert not math.isnan(row.values["ext_change_24h_log"])
    assert math.isnan(row.values["cross_level_ratio_log"])  # different measurement: rejected
    assert hours(at(spec, 0)) < hours(at(spec, 1))
