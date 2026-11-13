"""Forecast model metadata (registry contract)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TargetFamily(StrEnum):
    """What a model forecasts. Families are never pooled: their series are incomparable."""

    YOUTUBE_SEARCH_INTEREST = "youtube_search_interest"  # direct YouTube Search measurement
    EXTERNAL_SEARCH_INTEREST = "external_search_interest"  # e.g. Google web search
    PROXY_ATTENTION = "proxy_attention"  # video/community activity: never search demand


class TargetType(StrEnum):
    BREAKOUT = "breakout"
    CONTINUATION = "continuation"
    TRAJECTORY = "trajectory"
    PEAK = "peak"


class ModelType(StrEnum):
    CLIMATOLOGY = "climatology"  # training base rate
    LOGISTIC_REGRESSION = "logistic_regression"
    LIGHTGBM_CLASSIFIER = "lightgbm_classifier"
    PERSISTENCE = "persistence"
    SEASONAL_NAIVE = "seasonal_naive"
    DAMPED_TREND = "damped_trend"
    LIGHTGBM_REGRESSOR = "lightgbm_regressor"
    ANALOG_PEAK = "analog_peak"  # empirical comparable-episode model (experimental)


class ModelStatus(StrEnum):
    EXPERIMENTAL = "experimental"
    VALIDATED = "validated"
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    RETIRED = "retired"


class DataOrigin(StrEnum):
    REAL = "real"
    SYNTHETIC = "synthetic"  # isolated experimental data: never a production model


class Period(_Frozen):
    start: datetime | None
    end: datetime | None


class ModelMetadata(_Frozen):
    model_id: str
    model_name: str
    version: str
    model_type: ModelType
    target_family: TargetFamily
    target_type: TargetType
    target_definition: dict[str, Any]
    horizon_hours: int
    granularity: str
    category_scope: str = "all"
    provider_scope: tuple[str, ...] = ()
    data_origin: DataOrigin
    feature_schema_version: str
    feature_names: tuple[str, ...]
    dataset_version: str
    dataset_hash: str
    training_period: Period
    calibration_period: Period
    test_period: Period
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    calibration: dict[str, Any] = Field(default_factory=dict)
    artifact_checksum: str = ""
    trained_at: datetime
    code_version: str
    status: ModelStatus = ModelStatus.EXPERIMENTAL
    status_reasons: tuple[str, ...] = ()
