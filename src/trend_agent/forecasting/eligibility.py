"""Which series can be forecast, for which horizon — and why not.

A forecast is refused (with a typed status and reasons) rather than produced with fake
precision: unsupported measurement, horizon not a whole number of observation periods,
too little history, unstable normalization, too many missing periods, or a stale source.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from trend_agent.forecasting.config import ForecastingConfig, granularity_hours, horizon_supported
from trend_agent.forecasting.feature_store import SeriesMeta, Visible, hours
from trend_agent.forecasting.target_definition import baseline_window
from trend_agent.schemas.forecast import ForecastStatus, SourceClassification
from trend_agent.schemas.model_metadata import TargetFamily
from trend_agent.schemas.monitoring import MeasurementCategory

M = MeasurementCategory
_FAMILY = {
    M.DIRECT_YOUTUBE_SEARCH_INTEREST: TargetFamily.YOUTUBE_SEARCH_INTEREST,
    M.EXTERNAL_SEARCH_INTEREST: TargetFamily.EXTERNAL_SEARCH_INTEREST,
    M.VIDEO_ACTIVITY: TargetFamily.PROXY_ATTENTION,
    M.COMMUNITY_ACTIVITY: TargetFamily.PROXY_ATTENTION,
}
_SOURCE = {
    M.DIRECT_YOUTUBE_SEARCH_INTEREST: SourceClassification.DIRECT_YOUTUBE_SEARCH_INTEREST,
    M.EXTERNAL_SEARCH_INTEREST: SourceClassification.EXTERNAL_SEARCH_INTEREST,
    M.VIDEO_ACTIVITY: SourceClassification.VIDEO_ACTIVITY,
    M.COMMUNITY_ACTIVITY: SourceClassification.COMMUNITY_ACTIVITY,
    M.OFFICIAL_EVENT_SIGNAL: SourceClassification.OFFICIAL_EVENT_SIGNAL,
}


def target_family(measurement: MeasurementCategory) -> TargetFamily | None:
    """The model family for a measurement; None = not a forecast target."""
    return _FAMILY.get(measurement)


def source_classification(meta: SeriesMeta) -> SourceClassification:
    return _SOURCE.get(meta.measurement, SourceClassification.SYNTHETIC_TEST_DATA)


@dataclass(frozen=True)
class EligibilityResult:
    status: ForecastStatus
    reasons: tuple[str, ...]
    history_hours: float
    missing_rate: float | None
    staleness_hours: float | None
    supported_horizons: tuple[int, ...]

    @property
    def eligible(self) -> bool:
        return self.status is ForecastStatus.READY


def check(
    vis: Visible, meta: SeriesMeta, cfg: ForecastingConfig, horizon: int | None = None
) -> EligibilityResult:
    e = cfg.eligibility
    step = granularity_hours(meta.granularity)
    reasons: list[str] = []
    supported = tuple(
        h for h in cfg.horizons_hours if step is not None and horizon_supported(h, meta.granularity)
    )
    ok = np.isfinite(vis.values)
    history = float(hours(vis.as_of) - vis.t_end[0] + (step or 0)) if vis.n else 0.0
    staleness = float(hours(vis.as_of) - vis.t_end[ok][-1]) if ok.any() else None
    bw = baseline_window(cfg.targets, meta.granularity)
    window = vis.t_end > hours(vis.as_of) - bw
    expected = bw / step if step else 0
    missing = float(max(0.0, 1 - np.sum(ok & window) / expected)) if expected else None

    def result(status: ForecastStatus) -> EligibilityResult:
        return EligibilityResult(status, tuple(reasons), history, missing, staleness, supported)

    if (
        meta.measurement.value not in e.forecastable_measurements
        or target_family(meta.measurement) is None
    ):
        reasons.append(f"{meta.measurement.value} is not a forecast target")
        return result(ForecastStatus.SOURCE_UNAVAILABLE)
    if step is None:
        reasons.append(f"granularity {meta.granularity.value} is not supported")
        return result(ForecastStatus.UNSUPPORTED_GRANULARITY)
    if horizon is not None and horizon not in supported:
        reasons.append(
            f"a {horizon} h horizon is not a whole number of {meta.granularity.value} periods; "
            "values are never interpolated"
        )
        return result(ForecastStatus.UNSUPPORTED_GRANULARITY)
    if not ok.any():
        reasons.append("no observation is available at the prediction time")
        return result(ForecastStatus.SOURCE_UNAVAILABLE)
    need = e.min_history_hours.get(meta.granularity, 168)
    if history < need:
        reasons.append(f"{history:.0f} h of history available, {need} h required")
        return result(ForecastStatus.INSUFFICIENT_HISTORY)
    if len({k for k, w in zip(vis.period_keys, window, strict=True) if w}) > 1:
        reasons.append("the normalization context changed inside the baseline window")
        return result(ForecastStatus.FORECAST_UNAVAILABLE)
    if missing is not None and missing > e.max_missing_rate:
        reasons.append(
            f"{missing:.0%} of recent periods are missing (max {e.max_missing_rate:.0%})"
        )
        return result(ForecastStatus.INSUFFICIENT_HISTORY)
    stale_max = e.max_staleness_hours.get(meta.granularity, 96)
    if staleness is not None and staleness > stale_max:
        reasons.append(f"the latest observation is {staleness:.0f} h old (max {stale_max} h)")
        return result(ForecastStatus.SOURCE_UNAVAILABLE)
    return result(ForecastStatus.READY)
