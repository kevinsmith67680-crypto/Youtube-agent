"""Versioned, point-in-time feature engineering.

Every feature is computed from data *visible at the prediction time* (``Visible`` views
from ``feature_store``), using actual observation timestamps (irregular sampling is not
treated as equally spaced). Missing inputs give NaN — never zero — and the schema states
each feature's unit, source and availability rule. Features that compare levels of two
series are computed only if Phase 3's comparability check accepts the pair; rejected
comparisons are recorded in the provenance.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from trend_agent.detection.engine import PointEvaluation
from trend_agent.forecasting.config import Targets, granularity_hours
from trend_agent.forecasting.feature_store import (
    ContentEvent,
    PointInTimeSeries,
    SeriesMeta,
    Visible,
    hours,
)
from trend_agent.forecasting.target_definition import Reference, baseline_window, bounded_index
from trend_agent.monitoring.comparability import assess_pair
from trend_agent.schemas.detection import BaselineReadiness
from trend_agent.schemas.enums import ScaleType, TimeGranularity
from trend_agent.schemas.forecast_features import FeatureGroup, FeatureSpec, MissingPolicy
from trend_agent.schemas.monitoring import MeasurementCategory

FEATURE_SCHEMA_VERSION = "features-1.0.0"
G = FeatureGroup
_NAN = float("nan")
C = 1.0  # offset in log ratios: log((a + 1) / (b + 1)), safe at zero


def _spec(
    name: str,
    group: FeatureGroup,
    unit: str,
    source: str,
    definition: str,
    availability: str,
    *,
    dtype: str = "float",
    scale_dependent: bool = False,
    missing: MissingPolicy = MissingPolicy.NAN,
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        group=group,
        dtype=dtype,
        unit=unit,
        source=source,
        definition=definition,
        availability=availability,
        missing_policy=missing,
        scale_dependent=scale_dependent,
    )


_VIS = "target-series observations available at T (publication time)"
_DET = "Phase 4 detector state replayed on observations available at T"
FEATURES: tuple[FeatureSpec, ...] = (
    # growth
    _spec(
        "level_vs_baseline_log",
        G.GROWTH,
        "log_ratio",
        "target_series",
        "log((level+1)/(B+1)); level = median of the last p values, B = trailing median",
        _VIS,
    ),
    _spec(
        "change_6h_log",
        G.GROWTH,
        "log_ratio",
        "target_series",
        "log ratio of the last value to the value 6 h earlier (hourly data only)",
        _VIS,
    ),
    _spec(
        "change_24h_log",
        G.GROWTH,
        "log_ratio",
        "target_series",
        "log ratio of the last value to the value 24 h earlier",
        _VIS,
    ),
    _spec(
        "change_72h_log",
        G.GROWTH,
        "log_ratio",
        "target_series",
        "log ratio of the last value to the value 72 h earlier",
        _VIS,
    ),
    _spec(
        "change_168h_log",
        G.GROWTH,
        "log_ratio",
        "target_series",
        "log ratio of the last value to the value 168 h earlier",
        _VIS,
    ),
    _spec(
        "abs_change_24h",
        G.GROWTH,
        "index_points",
        "target_series",
        "last value minus the value 24 h earlier",
        _VIS,
        scale_dependent=True,
    ),
    _spec(
        "slope_recent_log_per_h",
        G.GROWTH,
        "log_per_hour",
        "target_series",
        "Theil-Sen slope of log(y+1) over the recent half of the growth window",
        _VIS,
    ),
    _spec(
        "growth_persistence",
        G.GROWTH,
        "share",
        "target_series",
        "share of increases among the last 6 consecutive changes",
        _VIS,
    ),
    _spec(
        "threshold_gap_log",
        G.GROWTH,
        "log_ratio",
        "target_series",
        "log((threshold+1)/(level+1)): distance to the breakout level",
        _VIS,
    ),
    _spec(
        "level_index",
        G.GROWTH,
        "index_points",
        "target_series",
        "current level (consistently scaled indices only)",
        _VIS,
        scale_dependent=True,
    ),
    # acceleration
    _spec(
        "slope_prior_log_per_h",
        G.ACCELERATION,
        "log_per_hour",
        "target_series",
        "Theil-Sen slope over the earlier half of the growth window",
        _VIS,
    ),
    _spec(
        "accel_log_per_h2",
        G.ACCELERATION,
        "log_per_hour2",
        "target_series",
        "(recent slope - prior slope) / half-window hours (actual timestamps)",
        _VIS,
    ),
    # volatility
    _spec(
        "baseline_mad_rel",
        G.VOLATILITY,
        "share",
        "target_series",
        "1.4826*MAD / (B+1) over the baseline window",
        _VIS,
    ),
    _spec(
        "fluctuation_log",
        G.VOLATILITY,
        "log_ratio",
        "target_series",
        "mean |change of log(y+1)| between consecutive observations in the growth window",
        _VIS,
    ),
    _spec(
        "noise_to_signal",
        G.VOLATILITY,
        "ratio",
        "target_series",
        "spread / (|level - B| + spread + 1)",
        _VIS,
    ),
    # detector state (Phase 4, causal)
    _spec(
        "det_anomaly_z",
        G.SURGE,
        "z_score",
        "detector_state",
        "robust anomaly z of the last visible period",
        _DET,
    ),
    _spec(
        "det_cusum_ratio",
        G.SURGE,
        "ratio",
        "detector_state",
        "CUSUM statistic / alarm threshold",
        _DET,
    ),
    _spec(
        "det_changepoint_prob",
        G.SURGE,
        "probability",
        "detector_state",
        "Bayesian online change-point posterior of a recent change",
        _DET,
    ),
    _spec(
        "det_run_length_h",
        G.SURGE,
        "log1p_hours",
        "detector_state",
        "log1p of the current segment length in hours, capped at 30 days",
        _DET,
    ),
    _spec(
        "det_elevated_streak_h",
        G.SURGE,
        "hours",
        "detector_state",
        "consecutive elevated periods (detector persistence) x period",
        _DET,
    ),
    _spec(
        "det_triggers_window",
        G.SURGE,
        "count",
        "detector_state",
        "detector triggers among the periods of the growth window",
        _DET,
    ),
    _spec(
        "det_baseline_ready",
        G.SURGE,
        "bool",
        "detector_state",
        "1 if the detector baseline was ready",
        _DET,
        dtype="int",
    ),
    # seasonality
    _spec(
        "hour_of_day",
        G.SEASONALITY,
        "hour",
        "target_series",
        "UTC hour of the last observation (hourly data only)",
        _VIS,
        dtype="int",
    ),
    _spec(
        "day_of_week",
        G.SEASONALITY,
        "day",
        "target_series",
        "UTC weekday of the last observation (0 = Monday)",
        _VIS,
        dtype="int",
    ),
    _spec(
        "dow_sin",
        G.SEASONALITY,
        "cyclic",
        "target_series",
        "sin(2*pi*weekday/7) of the last observation (weekly position, cyclic)",
        _VIS,
    ),
    _spec(
        "dow_cos",
        G.SEASONALITY,
        "cyclic",
        "target_series",
        "cos(2*pi*weekday/7) of the last observation",
        _VIS,
    ),
    _spec(
        "hod_sin",
        G.SEASONALITY,
        "cyclic",
        "target_series",
        "sin(2*pi*hour/24) of the last observation (hourly data only)",
        _VIS,
    ),
    _spec(
        "hod_cos",
        G.SEASONALITY,
        "cyclic",
        "target_series",
        "cos(2*pi*hour/24) of the last observation (hourly data only)",
        _VIS,
    ),
    _spec(
        "seasonal_ratio_log",
        G.SEASONALITY,
        "log_ratio",
        "target_series",
        "log ratio of the last value to the same phase one season earlier (24 h / 7 d)",
        _VIS,
    ),
    # novelty
    _spec(
        "series_age_h",
        G.NOVELTY,
        "log1p_hours",
        "target_series",
        "log1p hours since the first visible observation, capped at 30 days",
        _VIS,
    ),
    _spec(
        "keyword_age_h",
        G.NOVELTY,
        "log1p_hours",
        "catalog",
        "log1p hours since the keyword was first seen (Phase 2, only if before T), capped",
        "keyword first_seen_at <= T",
    ),
    _spec(
        "entity_age_h",
        G.NOVELTY,
        "log1p_hours",
        "catalog",
        "log1p hours since the entity was first seen (Phase 2, only if before T), capped",
        "entity first_seen_at <= T",
    ),
    # attention / supporting series (never mixed into the target scale)
    _spec(
        "ext_change_24h_log",
        G.ATTENTION,
        "log_ratio",
        "supporting_series",
        "24 h log change of external search interest for the same query",
        "supporting observations available at T",
    ),
    _spec(
        "video_change_24h_log",
        G.ATTENTION,
        "log_ratio",
        "supporting_series",
        "24 h log change of video activity for the same query (proxy, not demand)",
        "supporting observations available at T",
    ),
    _spec(
        "community_change_24h_log",
        G.ATTENTION,
        "log_ratio",
        "supporting_series",
        "24 h log change of community activity (proxy, not demand)",
        "supporting observations available at T",
    ),
    _spec(
        "official_event_age_h",
        G.ATTENTION,
        "hours",
        "supporting_series",
        "hours since the latest official event signal available at T",
        "official event observations available at T",
    ),
    _spec(
        "cross_level_ratio_log",
        G.ATTENTION,
        "log_ratio",
        "supporting_series",
        "log ratio of target and external levels - only if Phase 3 rates them comparable",
        "comparable supporting observation available at T",
    ),
    # content gap (Phase 5)
    _spec(
        "gap_strength_ord",
        G.CONTENT_GAP,
        "ordinal",
        "content",
        "0 none/insufficient, 1 weak, 2 moderate, 3 strong (latest analysis before T)",
        "analysis completed <= T",
        dtype="int",
    ),
    _spec(
        "gap_is_opportunity",
        G.CONTENT_GAP,
        "bool",
        "content",
        "1 if the latest analysis before T found an opportunity gap",
        "analysis completed <= T",
        dtype="int",
    ),
    _spec(
        "content_age_h",
        G.CONTENT_GAP,
        "hours",
        "content",
        "hours since the latest analysis completed before T",
        "analysis completed <= T",
    ),
    # data quality
    _spec(
        "missing_rate",
        G.QUALITY,
        "share",
        "target_series",
        "share of expected periods without a value in the baseline window",
        _VIS,
    ),
    _spec(
        "staleness_h",
        G.QUALITY,
        "hours",
        "target_series",
        "hours between T and the end of the last visible period",
        _VIS,
    ),
    _spec(
        "granularity_h", G.QUALITY, "hours", "meta", "observation period length", "series metadata"
    ),
    _spec(
        "normalization_stable",
        G.QUALITY,
        "bool",
        "target_series",
        "1 if every visible value shares one normalization context",
        _VIS,
        dtype="int",
    ),
    _spec(
        "revision_share",
        G.QUALITY,
        "share",
        "target_series",
        "share of visible values that are revisions",
        _VIS,
    ),
    _spec(
        "complete_share",
        G.QUALITY,
        "share",
        "target_series",
        "share of visible values marked complete",
        _VIS,
    ),
    _spec(
        "baseline_obs",
        G.QUALITY,
        "count",
        "target_series",
        "observations in the baseline window",
        _VIS,
    ),
    # context
    _spec("cat_ai", G.CONTEXT, "bool", "meta", "category is AI", "series metadata", dtype="int"),
    _spec(
        "cat_technology",
        G.CONTEXT,
        "bool",
        "meta",
        "category is technology",
        "series metadata",
        dtype="int",
    ),
    _spec(
        "cat_gaming",
        G.CONTEXT,
        "bool",
        "meta",
        "category is gaming",
        "series metadata",
        dtype="int",
    ),
)
FEATURE_NAMES = tuple(f.name for f in FEATURES)
SPECS = {f.name: f for f in FEATURES}
_GAP_STRENGTH = {"weak": 1, "moderate": 2, "strong": 3}


def code_version() -> str:
    """Hash of the code that defines features and targets (stored with datasets/models)."""
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in ("feature_engineering.py", "target_definition.py", "feature_store.py"):
        h.update((here / name).read_bytes())
    return h.hexdigest()[:16]


@dataclass
class FeatureRow:
    values: dict[str, float]
    provenance: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)

    def vector(self, names: Sequence[str] = FEATURE_NAMES) -> np.ndarray:
        return np.array([self.values.get(n, _NAN) for n in names], dtype=float)


AGE_CAP_HOURS = 720.0


def _age(h: float) -> float:
    """Age features: log1p(hours) capped at 30 days, so they do not trend with calendar time."""
    return math.log1p(min(max(h, 0.0), AGE_CAP_HOURS))


def _log_ratio(a: float, b: float) -> float:
    if not (math.isfinite(a) and math.isfinite(b)) or a < 0 or b < 0:
        return _NAN
    return math.log((a + C) / (b + C))


def _value_before(vis: Visible, t_ref: float, lag: float, step: float) -> float:
    """Value of the observation ending ``lag`` hours before ``t_ref`` (within one period)."""
    target = t_ref - lag
    ok = np.isfinite(vis.values) & (np.abs(vis.t_end - target) <= 0.5 * step + 1e-9)
    if not ok.any():
        return _NAN
    idx = np.flatnonzero(ok)
    best = idx[np.argmin(np.abs(vis.t_end[idx] - target))]
    return float(vis.values[best])


def _theil_sen(t: np.ndarray, y: np.ndarray) -> float:
    if t.size < 3:
        return _NAN
    i, j = np.triu_indices(t.size, 1)
    dt = t[j] - t[i]
    ok = dt > 0
    if not ok.any():
        return _NAN
    return float(np.median((y[j] - y[i])[ok] / dt[ok]))


def _change(vis: Visible | None, lag: float, step: float) -> float:
    if vis is None or vis.n == 0:
        return _NAN
    ok = np.isfinite(vis.values)
    if not ok.any():
        return _NAN
    last_i = int(np.flatnonzero(ok)[-1])
    return _log_ratio(
        float(vis.values[last_i]), _value_before(vis, float(vis.t_end[last_i]), lag, step)
    )


def growth_window_hours(g: TimeGranularity) -> float:
    return 24.0 if g is TimeGranularity.HOUR else 168.0


def compute(
    vis: Visible,
    ref: Reference,
    meta: SeriesMeta,
    targets: Targets,
    *,
    detector: Sequence[PointEvaluation] = (),
    supporting: Mapping[MeasurementCategory, tuple[Visible, PointInTimeSeries]] | None = None,
    target_pit: PointInTimeSeries | None = None,
    content: Sequence[ContentEvent] = (),
) -> FeatureRow:
    """Feature vector for one series at ``vis.as_of`` (point-in-time)."""
    v: dict[str, float] = dict.fromkeys(FEATURE_NAMES, _NAN)
    prov: dict[str, str] = {}
    rejected: list[str] = []
    step = granularity_hours(meta.granularity) or 24.0
    t_now = hours(vis.as_of)
    ok = np.isfinite(vis.values)
    scale_ok = meta.scale_type is ScaleType.CONSISTENT_INDEX
    v["granularity_h"] = step
    v["cat_ai"] = float(meta.category == "ai")
    v["cat_technology"] = float(meta.category == "technology")
    v["cat_gaming"] = float(meta.category == "gaming")
    v["normalization_stable"] = float(len(vis.normalization_keys) <= 1)
    v["baseline_obs"] = float(ref.baseline_observations)
    if vis.n:
        v["series_age_h"] = _age(t_now - float(vis.t_end[0]) + step)
        v["revision_share"] = float(np.mean(vis.revisions > 1))
        v["complete_share"] = float(np.mean(vis.complete))
        prov["target"] = (
            f"{vis.n} visible periods ending {_iso(vis.t_end[0])} .. {_iso(vis.t_end[-1])}"
        )
    bw = baseline_window(targets, meta.granularity)
    window = vis.t_end > t_now - bw
    expected = bw / step
    if expected > 0:
        v["missing_rate"] = float(max(0.0, 1.0 - np.sum(ok & window) / expected))
    if ok.any():
        last_i = int(np.flatnonzero(ok)[-1])
        t_last = float(vis.t_end[last_i])
        y_last = float(vis.values[last_i])
        v["staleness_h"] = t_now - t_last
        from trend_agent.forecasting.feature_store import from_hours

        last_dt = from_hours(t_last - step)  # period start of the last observation
        v["day_of_week"] = float(last_dt.weekday())
        v["dow_sin"] = math.sin(2 * math.pi * last_dt.weekday() / 7)
        v["dow_cos"] = math.cos(2 * math.pi * last_dt.weekday() / 7)
        if meta.granularity is TimeGranularity.HOUR:
            v["hour_of_day"] = float(last_dt.hour)
            v["hod_sin"] = math.sin(2 * math.pi * last_dt.hour / 24)
            v["hod_cos"] = math.cos(2 * math.pi * last_dt.hour / 24)
        for lag in (6, 24, 72, 168):
            if lag >= step - 1e-9:
                v[f"change_{lag}h_log"] = _log_ratio(
                    y_last, _value_before(vis, t_last, float(lag), step)
                )
        season = 24.0 if meta.granularity is TimeGranularity.HOUR else 168.0
        v["seasonal_ratio_log"] = _log_ratio(y_last, _value_before(vis, t_last, season, step))
        prior24 = _value_before(vis, t_last, 24.0, step)
        if scale_ok and math.isfinite(prior24):
            v["abs_change_24h"] = y_last - prior24
        gw = growth_window_hours(meta.granularity)
        half = gw / 2
        m_recent = ok & (vis.t_end > t_last - half)
        m_prior = ok & (vis.t_end <= t_last - half) & (vis.t_end > t_last - gw)
        ly = np.log(np.where(ok, vis.values, 0.0) + C)
        s_recent = _theil_sen(vis.t_end[m_recent], ly[m_recent])
        s_prior = _theil_sen(vis.t_end[m_prior], ly[m_prior])
        v["slope_recent_log_per_h"] = s_recent
        v["slope_prior_log_per_h"] = s_prior
        if math.isfinite(s_recent) and math.isfinite(s_prior):
            v["accel_log_per_h2"] = (s_recent - s_prior) / half
        diffs = np.diff(ly[ok][-7:])
        if diffs.size:
            v["growth_persistence"] = float(np.mean(diffs > 0))
        m_gw = ok & (vis.t_end > t_last - gw)
        if np.sum(m_gw) >= 2:
            v["fluctuation_log"] = float(np.mean(np.abs(np.diff(ly[m_gw]))))
    if ref.baseline is not None:
        v["baseline_mad_rel"] = (ref.spread or 0.0) / (ref.baseline + C)
        if ref.level is not None:
            v["level_vs_baseline_log"] = _log_ratio(ref.level, ref.baseline)
            spread = ref.spread or 0.0
            v["noise_to_signal"] = spread / (abs(ref.level - ref.baseline) + spread + C)
            if scale_ok and bounded_index(meta):
                v["level_index"] = ref.level
        if ref.threshold is not None and ref.level is not None:
            v["threshold_gap_log"] = _log_ratio(ref.threshold, ref.level)
    if not scale_ok:
        rejected.append(
            f"scale-dependent features disabled: {meta.scale_type.value} is not a "
            "consistently scaled index"
        )
    # detector state at T
    if detector:
        ev = detector[-1]
        if ev.anomaly.z is not None:
            v["det_anomaly_z"] = float(ev.anomaly.z)
        if ev.cusum.statistic is not None and ev.cusum.threshold:
            v["det_cusum_ratio"] = float(ev.cusum.statistic) / float(ev.cusum.threshold)
        if ev.change_point.probability is not None:
            v["det_changepoint_prob"] = float(ev.change_point.probability)
        if ev.change_point.run_length is not None:
            v["det_run_length_h"] = _age(float(ev.change_point.run_length) * step)
        v["det_elevated_streak_h"] = float(ev.persistence) * step
        gw = growth_window_hours(meta.granularity)
        recent = [e for e in detector if hours(e.period_end) > hours(ev.period_end) - gw]
        v["det_triggers_window"] = float(sum(e.any_trigger for e in recent))
        v["det_baseline_ready"] = float(ev.baseline.readiness is BaselineReadiness.READY)
        prov["detector"] = f"Phase 4 state at period ending {ev.period_end.isoformat()}"
    # novelty (catalog facts are used only if they were known at T)
    for name, seen in (
        ("keyword_age_h", meta.keyword_first_seen),
        ("entity_age_h", meta.entity_first_seen),
    ):
        if seen is not None and seen <= vis.as_of:
            v[name] = _age(t_now - hours(seen))
    # supporting series
    for cat, (svis, spit) in (supporting or {}).items():
        sstep = step
        if svis.n:
            sstep = float(np.median(np.diff(svis.t_end))) if svis.n > 1 else step
        if cat is MeasurementCategory.EXTERNAL_SEARCH_INTEREST:
            v["ext_change_24h_log"] = _change(svis, 24.0, sstep)
            prov["external"] = f"{svis.n} external observations visible at T"
            a = target_pit.last_point(vis.as_of) if target_pit else None
            b = spit.last_point(vis.as_of)
            if a is not None and b is not None:
                verdict = assess_pair(a, b)
                if verdict.status.value in ("comparable", "comparable_with_caveats"):
                    v["cross_level_ratio_log"] = _log_ratio(
                        float(a.value or 0.0), float(b.value or 0.0)
                    )
                else:
                    rejected.append(
                        "cross_level_ratio_log rejected: "
                        + ", ".join(r.value for r in verdict.reasons)
                    )
        elif cat is MeasurementCategory.VIDEO_ACTIVITY:
            v["video_change_24h_log"] = _change(svis, 24.0, sstep)
            prov["video_activity"] = "proxy attention, not search demand"
        elif cat is MeasurementCategory.COMMUNITY_ACTIVITY:
            v["community_change_24h_log"] = _change(svis, 24.0, sstep)
        elif cat is MeasurementCategory.OFFICIAL_EVENT_SIGNAL:
            okv = np.isfinite(svis.values) & (svis.values > 0)
            if okv.any():
                v["official_event_age_h"] = t_now - float(svis.t_end[okv][-1])
    # Phase 5 content analyses completed before T
    known = [c for c in content if c.completed_at <= vis.as_of]
    if known:
        last = max(known, key=lambda c: c.completed_at)
        v["gap_strength_ord"] = float(_GAP_STRENGTH.get(last.strength or "", 0))
        v["gap_is_opportunity"] = float(last.is_opportunity)
        v["content_age_h"] = t_now - hours(last.completed_at)
        prov["content"] = f"analysis completed {last.completed_at.isoformat()}"
    return FeatureRow(v, prov, rejected)


def _iso(h: float) -> str:
    from trend_agent.forecasting.feature_store import from_hours

    return from_hours(float(h)).isoformat()


def schema_document() -> list[dict[str, object]]:
    return [f.model_dump(mode="json") for f in FEATURES]


__all__ = [
    "FEATURES",
    "FEATURE_NAMES",
    "FEATURE_SCHEMA_VERSION",
    "FeatureRow",
    "compute",
    "datetime",
]
