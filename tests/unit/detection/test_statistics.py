"""Statistical core: preprocessing, baselines, anomaly, CUSUM, change points, cold start."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from scipy import stats

from tests.unit.detection.helpers import CFG, LOADED, noisy, point, run_engine
from tests.unit.monitoring.helpers import KEY_B
from trend_agent.detection import change_points as cp
from trend_agent.detection import cusum
from trend_agent.detection.anomaly import detect
from trend_agent.detection.baseline import BaselineModeler, window_stats
from trend_agent.detection.cold_start import CorroborationContext, assess
from trend_agent.detection.config import (
    ChangePointConfig,
    CusumConfig,
    DetectionConfig,
    LoadedDetectionConfig,
    ScaleFamily,
    StratumContext,
)
from trend_agent.detection.engine import EngineState, SeriesEngine
from trend_agent.detection.preprocessing import knowledge_at, phase_of, prepare, slot_of
from trend_agent.detection.statistical_tests import kruskal_wallis, nan_stats_rows
from trend_agent.discovery.models import EvidenceType, NoveltyKind
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.detection import (
    BaselineModel,
    BaselineReadiness,
    ColdStartState,
    DispersionSource,
    PreprocessStatus,
    Reason,
    ResultStatus,
)
from trend_agent.schemas.enums import SampleQuality, ScaleType, TimeGranularity
from trend_agent.schemas.evidence import AlertStage
from trend_agent.schemas.monitoring import MeasurementCategory

READY = BaselineReadiness.READY
DAY_ = point(1).period_start - point(0).period_start


# ----------------------------------------------------------------------------- primitives
def test_vectorised_primitives_match_numpy_and_scipy() -> None:
    rng = np.random.default_rng(3)
    w = rng.normal(size=(50, 28))
    w[rng.random(w.shape) < 0.2] = np.nan
    w[0, :] = np.nan  # an empty row stays NaN, never zero
    counts, med, mad, _ = nan_stats_rows(w)
    with np.errstate(all="ignore"), pytest.warns(RuntimeWarning):
        ref_med = np.nanmedian(w, axis=1)
    np.testing.assert_allclose(med[1:], ref_med[1:])
    assert math.isnan(med[0])
    assert counts[0] == 0
    ref_mad = [np.median(np.abs(r[np.isfinite(r)] - np.median(r[np.isfinite(r)]))) for r in w[1:]]
    np.testing.assert_allclose(mad[1:], ref_mad)
    groups = [rng.normal(loc=i * 0.3, size=20).round(1) for i in range(5)]
    h, p = kruskal_wallis(groups)
    ref = stats.kruskal(*groups)
    assert h == pytest.approx(ref.statistic)
    assert p == pytest.approx(ref.pvalue)


def test_grid_and_phases() -> None:
    monday = point(0).period_start
    assert phase_of(slot_of(monday, TimeGranularity.DAY), TimeGranularity.DAY) == 0
    assert phase_of(slot_of(point(6).period_start, TimeGranularity.DAY), TimeGranularity.DAY) == 6


# ----------------------------------------------------------------------------- preprocessing
def test_knowledge_at_uses_only_available_revisions() -> None:
    first = point(0, 40.0)  # published day 2 12:00
    pub = first.provider_published_at
    assert pub is not None
    revised = point(
        0,
        60.0,
        revision=2,
        revision_kind="revision",
        provider_published_at=pub + 2 * DAY_,
        collected_at=first.collected_at + 5 * DAY_,
        ingested_at=first.ingested_at + 5 * DAY_,
    )
    conflict = point(0, 99.0, revision=3, revision_kind="conflict")
    everything = [first, revised, conflict]
    assert knowledge_at(everything, pub - DAY_) == []
    assert [p.value for p in knowledge_at(everything, pub)] == [40.0]
    assert [p.value for p in knowledge_at(everything, pub + 2 * DAY_)] == [60.0]
    # agent mode: available only once we had collected/ingested it
    assert knowledge_at([first], pub, AvailabilityMode.AGENT) == []


def test_missing_values_and_periods_are_never_zero() -> None:
    pts = [point(i, None if i % 4 == 0 else 40.0 + i % 3) for i in range(30) if i != 17]
    s = prepare(pts, CFG, already_known=True)
    assert s.missing_periods == 1
    assert s.missing_values == 8
    assert np.isnan(s.values[17])
    assert np.isnan(s.values[0])
    assert not np.any(s.values[np.isfinite(s.values)] == 0)
    assert any("never zero-filled" in r for r in s.reasons)
    assert s.status is PreprocessStatus.PARTIALLY_READY


def test_partial_periods_excluded_and_invalid_scale_rejected() -> None:
    pts = noisy(20)
    pts.append(point(20, 90.0, sample_quality=SampleQuality.PARTIAL_PERIOD))
    s = prepare(pts, CFG, already_known=True)
    assert s.partial_periods == 1
    assert np.isnan(s.values[-1])
    rel = ScaleType.RELATIVE_PER_REQUEST
    bad = [*noisy(20, scale_type=rel), point(20, 140.0, scale_type=rel)]
    assert prepare(bad, CFG, already_known=True).status is PreprocessStatus.INVALID_SCALE


def test_mixed_normalisation_contexts_are_incompatible() -> None:
    a = noisy(20)
    b = [point(i, 70.0, normalization_key=KEY_B, series_id=2) for i in range(20, 30)]
    s = prepare(a + b, CFG, already_known=True)
    assert s.status is PreprocessStatus.INCOMPATIBLE
    assert Reason.NORMALIZATION_CHANGED.value in s.reasons
    out = run_engine(a + b)
    assert out.evaluations == []
    assert out.skipped


def test_sparse_and_irregular_series() -> None:
    sparse = prepare([point(i, 10.0 + i) for i in range(4)], CFG, already_known=True)
    assert sparse.status is PreprocessStatus.INSUFFICIENT_DATA
    irregular = prepare([point(i * 3, 40.0) for i in range(25)], CFG, already_known=True)
    assert irregular.status is PreprocessStatus.IRREGULAR_SAMPLING
    out = run_engine([point(i * 3, 40.0 + (i % 3)) for i in range(25)])
    last = out.evaluations[-1]
    assert last.cusum.status is ResultStatus.INSUFFICIENT_DATA
    assert Reason.IRREGULAR_CADENCE.value in last.cusum.reasons
    presence = prepare(
        [point(i, 1.0, scale_type=ScaleType.PRESENCE) for i in range(20)], CFG, already_known=True
    )
    assert presence.status is PreprocessStatus.UNSUPPORTED_SCALE


# ----------------------------------------------------------------------------- baselines
def test_robust_baseline_uses_prior_values_only() -> None:
    values = np.array([10.0, 12.0, 11.0, 13.0, 50.0])
    counts, med, mad, _ = window_stats(values, [4], 4)
    assert counts[0] == 4
    assert med[0] == 11.5
    assert mad[0] == 1.0
    pts = noisy(40)
    out = run_engine(pts)
    e = out.evaluations[30]
    window = [float(p.value or 0.0) for p in pts[2:30]]
    assert e.baseline.expected == pytest.approx(float(np.median(window)))
    assert e.baseline.sample_count == 28
    assert e.baseline.readiness is READY
    assert out.evaluations[3].baseline.readiness is BaselineReadiness.INSUFFICIENT
    assert out.evaluations[9].baseline.readiness is BaselineReadiness.PROVISIONAL


def test_zero_mad_never_produces_infinite_or_confident_scores() -> None:
    flat = [point(i, 20.0) for i in range(30)] + [point(30, 28.0)]
    out = run_engine(flat)
    e = out.evaluations[-1]
    assert e.baseline.dispersion_source is DispersionSource.FLOOR
    assert e.baseline.unstable
    assert math.isfinite(e.anomaly.z or 0)
    assert e.anomaly.z == pytest.approx(8.0 / (1.0 + CFG.baseline.robust.epsilon))
    # mostly flat with a few deviations: MAD = 0, mean absolute deviation used, still unstable
    mostly = [point(i, 20.0 if i % 5 else 23.0) for i in range(30)] + [point(30, 40.0)]
    e2 = run_engine(mostly).evaluations[-1]
    assert e2.baseline.dispersion_source in (DispersionSource.MEAN_ABS_DEV, DispersionSource.FLOOR)
    assert e2.baseline.unstable


def test_seasonal_baseline_selected_with_enough_cycles_only() -> None:
    rng = np.random.default_rng(5)
    weekly = [
        point(i, float(round(40 + (18 if i % 7 in (5, 6) else 0) + rng.normal(0, 2))))
        for i in range(120)
    ]
    out = run_engine(weekly)
    early, late = out.evaluations[30], out.evaluations[-1]
    assert early.baseline.model is BaselineModel.ROBUST_ROLLING
    assert late.baseline.model is BaselineModel.SEASONAL_PROFILE
    assert "Kruskal-Wallis" in late.baseline.selection_reason
    weekend = [e for e in out.evaluations[60:] if e.period_start.weekday() >= 5]
    assert all(not e.anomaly.triggered for e in weekend)
    # no weekly effect -> the seasonal model is not selected
    flat = run_engine(noisy(120, seed=9)).evaluations[-1]
    assert flat.baseline.model is BaselineModel.ROBUST_ROLLING
    assert "no significant phase effect" in flat.baseline.selection_reason


def test_no_hour_of_day_profile_for_daily_data() -> None:
    modeler = BaselineModeler(CFG.baseline, TimeGranularity.WEEK, ScaleFamily.INDEX)
    assert modeler.cycle is None
    assert BaselineModeler(CFG.baseline, TimeGranularity.DAY, ScaleFamily.INDEX).cycle == 7
    assert BaselineModeler(CFG.baseline, TimeGranularity.HOUR, ScaleFamily.INDEX).cycle == 24


# ----------------------------------------------------------------------------- anomaly
def test_rapid_anomaly_detects_upward_spike_with_meaningful_effect() -> None:
    pts = [*noisy(40), point(40, 75.0)]
    e = run_engine(pts).evaluations[-1]
    assert e.anomaly.status is ResultStatus.TRIGGERED
    assert e.baseline.expected is not None
    assert e.anomaly.absolute_difference == pytest.approx(75.0 - e.baseline.expected)
    assert e.anomaly.relative_difference is not None
    assert "prediction_interval_95" in e.anomaly.uncertainty
    assert e.anomaly.p_value is None  # no calibration -> no p-value


def test_downward_change_never_creates_upward_alert() -> None:
    e = run_engine([*noisy(40), point(40, 5.0)]).evaluations[-1]
    assert e.anomaly.status is ResultStatus.NOT_TRIGGERED
    assert Reason.DOWNWARD_CHANGE.value in e.anomaly.reasons
    assert e.anomaly.score == 0.0


def test_small_absolute_change_is_rejected_not_alerted() -> None:
    low = [point(i, 2.0 + (i % 2)) for i in range(40)] + [point(40, 8.0)]
    e = run_engine(low).evaluations[-1]
    assert e.anomaly.status is ResultStatus.REJECTED
    assert Reason.BELOW_MIN_EFFECT.value in e.anomaly.reasons
    assert Reason.BELOW_MIN_LEVEL.value in e.anomaly.reasons


def test_thresholds_vary_by_stratum() -> None:
    ctx = StratumContext(
        provider="p",
        granularity=TimeGranularity.DAY,
        category="ai",
        readiness=READY,
        scale_family=ScaleFamily.INDEX,
        measurement=MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
    )
    ready = CFG.thresholds(ctx)
    provisional = CFG.thresholds(
        ctx.model_copy(update={"readiness": BaselineReadiness.PROVISIONAL})
    )
    hourly = CFG.thresholds(ctx.model_copy(update={"granularity": TimeGranularity.HOUR}))
    counts = CFG.thresholds(ctx.model_copy(update={"scale_family": ScaleFamily.COUNT}))
    assert provisional.alert_z > hourly.alert_z > ready.alert_z
    assert counts.min_absolute_effect != ready.min_absolute_effect
    assert "provisional-baseline" in provisional.applied


def test_anomaly_missing_value_and_cold_start() -> None:
    from trend_agent.detection.baseline import BaselineEstimate

    est = BaselineEstimate(
        BaselineModel.NONE, None, None, None, 3, BaselineReadiness.INSUFFICIENT, TimeGranularity.DAY
    )
    th = CFG.thresholds(
        StratumContext(
            provider="p",
            granularity=TimeGranularity.DAY,
            category=None,
            readiness=BaselineReadiness.PROVISIONAL,
            scale_family=ScaleFamily.INDEX,
            measurement=MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        )
    )
    assert detect(None, est, th, CFG.anomaly, ScaleFamily.INDEX, epsilon=1e-9).reasons == (
        Reason.MISSING_VALUE.value,
    )
    assert detect(30.0, est, th, CFG.anomaly, ScaleFamily.INDEX, epsilon=1e-9).reasons == (
        Reason.COLD_START.value,
    )


# ----------------------------------------------------------------------------- CUSUM
def test_cusum_update_formula_clipping_and_single_value_cannot_alarm() -> None:
    cfg = CusumConfig(k=1.0, h=6.0, z_clip=3.0)
    st = cusum.CusumState()
    step = cusum.update(st, 0, 50.0, cfg)  # extreme single value is clipped
    assert step.statistic == pytest.approx(2.0)
    assert not step.alarm
    cusum.update(st, 1, 0.5, cfg)
    assert st.g == pytest.approx(1.5)
    cusum.update(st, 2, -5.0, cfg)
    assert st.g == 0.0
    assert st.run_start_slot is None
    with pytest.raises(ValueError, match="single value"):
        CusumConfig(k=0.5, h=2.0, z_clip=3.0)


def test_cusum_alarm_resets_and_refractory_prevents_repeat_alarms() -> None:
    cfg = CusumConfig(k=1.0, h=6.0, z_clip=3.0, refractory_periods=5)
    st = cusum.CusumState()
    steps = [cusum.update(st, s, 3.0, cfg) for s in range(12)]
    alarms = [s for s, step in enumerate(steps) if step.alarm]
    assert alarms[0] == 3  # 2+2+2 = 6 is not > 6; the fourth elevated period alarms
    assert st.refractory_until is not None
    assert all(a - alarms[0] > 5 for a in alarms[1:])  # no alarm within the refractory window
    assert any(
        step.status is ResultStatus.WATCH and Reason.REFRACTORY.value in step.reasons
        for step in steps
    )
    assert steps[3].onset_slot == 0


def test_cusum_gap_resets_statistic() -> None:
    cfg = CusumConfig(k=1.0, h=6.0, z_clip=3.0, max_gap_periods=3)
    st = cusum.CusumState()
    cusum.update(st, 0, 3.0, cfg)
    cusum.update(st, 1, 3.0, cfg)
    step = cusum.update(st, 10, 1.5, cfg)
    assert Reason.GAP_RESET.value in step.reasons
    assert st.g == pytest.approx(0.5)


def test_siegmund_matches_known_value() -> None:
    assert cusum.siegmund_arl0(0.5, 5.0) == pytest.approx(938, rel=0.01)


def test_sustained_shift_detected_by_cusum_not_single_point() -> None:
    rng = np.random.default_rng(11)
    pts = [point(i, float(round(40 + rng.normal(0, 3) + (7 if i >= 40 else 0)))) for i in range(70)]
    out = run_engine(pts)
    after = list(out.evaluations[40:])
    assert any(e.cusum.triggered for e in after)
    first = next(e for e in after if e.cusum.triggered)
    assert first.anomaly.status is not ResultStatus.TRIGGERED or first.persistence > 1


# ----------------------------------------------------------------------------- change points
def test_bocpd_detects_upward_level_shift_but_not_a_lone_spike() -> None:
    cfg = ChangePointConfig()
    rng = np.random.default_rng(2)
    x = list(40 + rng.normal(0, 2, 60)) + list(60 + rng.normal(0, 2, 6))
    st = cp.init_state(x[0], 0, 2.0, cfg)
    steps = [cp.update(st, v, i, cfg, floor=1.0, min_effect=8.0) for i, v in enumerate(x[1:], 1)]
    assert steps[-1].triggered
    assert steps[-1].change_slot == 60
    assert steps[-1].pre_level == pytest.approx(40, abs=2)
    assert steps[-1].post_level == pytest.approx(60, abs=2)
    spike = list(40 + rng.normal(0, 2, 60))
    spike[50] = 90
    st2 = cp.init_state(spike[0], 0, 2.0, cfg)
    s2 = [cp.update(st2, v, i, cfg, floor=1.0, min_effect=8.0) for i, v in enumerate(spike[1:], 1)]
    assert not any(s.triggered for s in s2)
    down = list(40 + rng.normal(0, 2, 60)) + list(20 + rng.normal(0, 2, 6))
    st3 = cp.init_state(down[0], 0, 2.0, cfg)
    s3 = [cp.update(st3, v, i, cfg, floor=1.0, min_effect=8.0) for i, v in enumerate(down[1:], 1)]
    assert not s3[-1].triggered
    assert Reason.DOWNWARD_CHANGE.value in s3[-1].reasons


def test_bocpd_state_round_trip_is_exact_and_truncated() -> None:
    cfg = ChangePointConfig(max_run_length=30)
    st = cp.init_state(10.0, 0, 1.0, cfg)
    for i in range(1, 80):
        cp.update(st, 10.0 + (i % 3), i, cfg, floor=1.0, min_effect=5.0)
    assert st.log_p.size == 31
    assert math.isclose(float(np.exp(st.log_p).sum()), 1.0, rel_tol=1e-9)
    back = cp.BocpdState.from_dict(st.as_dict())
    np.testing.assert_array_equal(back.log_p, st.log_p)
    a = cp.update(st, 30.0, 80, cfg, floor=1.0, min_effect=5.0)
    b = cp.update(back, 30.0, 80, cfg, floor=1.0, min_effect=5.0)
    assert a == b


# ----------------------------------------------------------------------------- cold start
def test_cold_start_states() -> None:
    def run(values: list[float], ctx: CorroborationContext | None) -> Any:
        return assess(
            values,
            min_baseline=14,
            min_effect=8.0,
            ctx=ctx,
            cfg=CFG.cold_start,
            max_stage=AlertStage.CANDIDATE,
        )

    assert run([], None).state is ColdStartState.NEW_TOPIC
    new = CorroborationContext(novelty_kind=NoveltyKind.NEW_ENTITY)
    assert run([3.0], new).state is ColdStartState.EARLY_INTEREST_OBSERVED
    assert run([20.0, 19.0, 21.0], None).state is ColdStartState.INSUFFICIENT_BASELINE
    growth = run([4.0, 7.0, 11.0, 16.0, 22.0], None)
    assert growth.state is ColdStartState.COLD_START_GROWTH_CANDIDATE
    assert growth.stage is AlertStage.WATCH  # measured growth only, no corroboration
    corroborated = CorroborationContext(
        novelty_kind=NoveltyKind.NEW_ENTITY,
        evidence_types={
            EvidenceType.OFFICIAL_ANNOUNCEMENT.value: 2,
            EvidenceType.VIDEO_TITLE.value: 5,
        },
        providers=("feed", "youtube"),
    )
    c = run([4.0, 9.0, 15.0], corroborated)
    assert c.state is ColdStartState.COLD_START_GROWTH_CANDIDATE
    assert c.stage is AlertStage.CANDIDATE
    assert run([float(i) for i in range(14)], None).state is (ColdStartState.BASELINE_ESTABLISHED)
    # newness and discussion alone are not a measured surge
    talk = run([5.0], corroborated)
    assert talk.stage in (None, AlertStage.WATCH)


# ----------------------------------------------------------------------------- engine
def test_incremental_processing_equals_full_recomputation() -> None:
    rng = np.random.default_rng(7)
    pts = [
        point(
            i,
            float(
                round(
                    40 + (12 if i % 7 in (5, 6) else 0) + rng.normal(0, 3) + (20 if i > 90 else 0)
                )
            ),
        )
        for i in range(110)
    ]
    full = run_engine(pts)
    eng = SeriesEngine(LOADED)
    first = eng.run(prepare(pts[:60], CFG, already_known=True), None)
    state = EngineState.from_payloads(first.state.payloads())  # survive a restart
    second = eng.run(prepare(pts, CFG, already_known=True), state)
    assert not second.recomputed

    def key(e: Any) -> tuple[Any, ...]:
        return (
            e.slot,
            e.anomaly.z,
            e.cusum.statistic,
            e.change_point.probability,
            e.baseline.model,
        )

    assert [key(e) for e in full.evaluations] == [
        key(e) for e in first.evaluations + second.evaluations
    ]
    assert second.state.payloads() == full.state.payloads()


def test_revision_and_late_arrival_trigger_recomputation() -> None:
    pts = noisy(50)
    eng = SeriesEngine(LOADED)
    out = eng.run(prepare(pts, CFG, already_known=True), None)
    revised = point(45, 90.0, revision=2, revision_kind="revision")
    pts2 = [*pts[:45], revised, *pts[46:]]
    out2 = eng.run(prepare(pts2, CFG, already_known=True), out.state, new_obs_ids={revised.id})
    assert out2.recomputed
    assert out2.recompute_reason == "revision"
    assert [e.observation_id for e in out2.evaluations] == [revised.id]
    in_order = eng.run(prepare(pts2, CFG, already_known=True), None).state
    assert out2.state.payloads()[next(iter(out2.state.payloads()))] is not None
    assert out2.state.cusum.as_dict() == in_order.cusum.as_dict()
    # late arrival: a missing day delivered after later days
    gap = [p for p in pts if p.period_start != pts[40].period_start]
    s1 = eng.run(prepare(gap, CFG, already_known=True), None)
    late = eng.run(prepare(pts, CFG, already_known=True), s1.state, new_obs_ids={pts[40].id})
    assert late.recompute_reason == "late_arrival"
    assert (
        late.state.cusum.as_dict()
        == eng.run(prepare(pts, CFG, already_known=True), None).state.cusum.as_dict()
    )


def test_configuration_change_forces_recomputation() -> None:
    pts = noisy(30)
    out = run_engine(pts)
    other = LoadedDetectionConfig(path=None, config=DetectionConfig(), digest="other")
    again = SeriesEngine(other).run(
        prepare(pts, CFG, already_known=True), out.state, new_obs_ids=set()
    )
    assert again.recompute_reason == "configuration_changed"
