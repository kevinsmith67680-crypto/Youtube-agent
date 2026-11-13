"""Typed, versioned forecast output (the contract Phase 7 consumes without re-inference)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FORECAST_SCHEMA_VERSION: Literal["forecast/1.0"] = "forecast/1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceClassification(StrEnum):
    DIRECT_YOUTUBE_SEARCH_INTEREST = "DIRECT_YOUTUBE_SEARCH_INTEREST"
    EXTERNAL_SEARCH_INTEREST = "EXTERNAL_SEARCH_INTEREST"
    VIDEO_ACTIVITY = "VIDEO_ACTIVITY"  # proxy attention, never search demand
    COMMUNITY_ACTIVITY = "COMMUNITY_ACTIVITY"  # proxy attention, never search demand
    OFFICIAL_EVENT_SIGNAL = "OFFICIAL_EVENT_SIGNAL"
    SYNTHETIC_TEST_DATA = "SYNTHETIC_TEST_DATA"


class ForecastStatus(StrEnum):
    READY = "READY"
    LIMITED_CONFIDENCE = "LIMITED_CONFIDENCE"
    BASELINE_ONLY = "BASELINE_ONLY"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    UNSUPPORTED_GRANULARITY = "UNSUPPORTED_GRANULARITY"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    FORECAST_UNAVAILABLE = "FORECAST_UNAVAILABLE"


class CalibrationStatus(StrEnum):
    CALIBRATED = "CALIBRATED"
    UNCALIBRATED = "UNCALIBRATED"
    INSUFFICIENT_CALIBRATION_DATA = "INSUFFICIENT_CALIBRATION_DATA"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PeakStatus(StrEnum):
    ESTIMATED = "ESTIMATED"
    PEAK_LIKELY_BEYOND_HORIZON = "PEAK_LIKELY_BEYOND_HORIZON"  # right-censored
    PEAK_MAY_HAVE_PASSED = "PEAK_MAY_HAVE_PASSED"
    PEAK_NOT_ESTIMABLE = "PEAK_NOT_ESTIMABLE"


class OpportunityTiming(StrEnum):
    URGENT = "URGENT"
    TIMELY = "TIMELY"
    LONGER_WINDOW = "LONGER_WINDOW"
    LIKELY_TOO_LATE = "LIKELY_TOO_LATE"
    UNKNOWN = "UNKNOWN"


class DataQualityStatus(StrEnum):
    GOOD = "GOOD"
    ACCEPTABLE = "ACCEPTABLE"
    DEGRADED = "DEGRADED"
    UNUSABLE = "UNUSABLE"


class ModelRef(_Frozen):
    model_id: str | None
    name: str
    version: str | None
    model_type: str
    status: str
    data_origin: str | None
    validated: bool


class BreakoutForecast(_Frozen):
    probability: float | None = Field(
        default=None, description="calibrated probability; None when not reliably estimable"
    )
    score: float | None = Field(
        default=None, description="uncalibrated model score (not a probability)"
    )
    calibration_status: CalibrationStatus
    applicable: bool = Field(description="False when the series is already elevated")
    probability_interval: tuple[float, float] | None = None
    threshold_index: float | None = Field(default=None, description="breakout level (series units)")
    baseline_index: float | None = None
    model: ModelRef | None = None
    baseline_comparison: dict[str, float | None] = Field(default_factory=dict)


class ContinuationForecast(_Frozen):
    probability: float | None = None
    calibration_status: CalibrationStatus
    applicable: bool
    model: ModelRef | None = None


class TrajectoryPoint(_Frozen):
    lead_hours: int
    forecast_time: datetime
    value: float | None
    lower: float | None
    upper: float | None
    interval_level: float | None
    interval_method: str
    model: str


class PeakEstimate(_Frozen):
    status: PeakStatus
    horizon_hours: int
    earliest_hours: float | None = None
    latest_hours: float | None = None
    median_hours: float | None = None
    probability_beyond_horizon: float | None = None
    analogs: int = 0
    experimental: bool = True
    note: str = ""


class OpportunityWindow(_Frozen):
    production_profile: str
    production_hours: float
    earliest_publication: datetime
    probability_elevated_at_publication: float | None
    probability_interval: tuple[float, float] | None = None
    horizon_covered: bool
    status: OpportunityTiming
    uncertainty: str
    evidence: tuple[str, ...] = ()
    note: str = (
        "A hypothetical planning estimate of demand persistence, not a prediction of video "
        "performance."
    )


class DataQuality(_Frozen):
    status: DataQualityStatus
    measurement_unit: str
    scale_type: str
    granularity: str
    observations: int
    missing_rate: float | None
    staleness_hours: float | None
    normalization_stable: bool
    reasons: tuple[str, ...] = ()


class Uncertainty(_Frozen):
    ood_status: str  # in_distribution | limited | out_of_distribution | unknown
    ood_share: float | None
    ood_features: tuple[str, ...] = ()
    calibration_support: dict[str, int | float | None] = Field(default_factory=dict)
    historical_reliability: dict[str, float | None] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()


class FeatureContribution(_Frozen):
    feature: str
    value: float | None
    contribution: float


class ForecastExplanation(_Frozen):
    predicted_target: str
    horizon: str
    data_type: str
    top_features: tuple[FeatureContribution, ...] = ()
    baseline_comparison: str
    calibration: str
    uncertainty: str
    limitations: tuple[str, ...]
    validation: str
    opportunity_reasoning: str
    attribution_note: str = (
        "Feature contributions describe the model's arithmetic, not causes of demand."
    )


class ForecastOutput(_Frozen):
    schema_version: Literal["forecast/1.0"] = FORECAST_SCHEMA_VERSION
    prediction_id: str
    query: str
    query_id: int | None
    target_id: int | None
    series_id: int | None
    category: str | None
    region: str | None
    provider: str
    source_classification: SourceClassification
    target_family: str
    is_proxy: bool
    synthetic: bool
    prediction_timestamp: datetime
    data_cutoff: datetime
    forecast_horizon_hours: int
    status: ForecastStatus
    status_reasons: tuple[str, ...] = ()
    breakout: BreakoutForecast | None = None
    continuation: ContinuationForecast | None = None
    trajectory: tuple[TrajectoryPoint, ...] = ()
    peak_estimate: PeakEstimate | None = None
    opportunity_windows: tuple[OpportunityWindow, ...] = ()
    data_quality: DataQuality
    uncertainty: Uncertainty
    explanation: ForecastExplanation | None = None
    features: dict[str, float | None] = Field(default_factory=dict)
    feature_schema_version: str
    configuration_version: str
    forecast_version: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    notice: str = (
        "Forecasts describe relative interest in the measured source only. They are not "
        "search counts, not view predictions and not guarantees; synthetic outputs say "
        "nothing about real-world accuracy."
    )
