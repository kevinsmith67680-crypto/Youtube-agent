"""Feature schema: every feature's meaning, unit, source and availability rule."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FeatureGroup(StrEnum):
    GROWTH = "growth"
    ACCELERATION = "acceleration"
    VOLATILITY = "volatility"
    SURGE = "surge"
    SEASONALITY = "seasonality"
    NOVELTY = "novelty"
    ATTENTION = "attention"
    CONTENT_GAP = "content_gap"
    QUALITY = "quality"
    CONTEXT = "context"


class MissingPolicy(StrEnum):
    NAN = "nan"  # left missing (NaN); models handle it, never replaced by zero
    INDICATOR = "indicator"  # NaN plus an explicit 0/1 availability indicator


class FeatureSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    group: FeatureGroup
    dtype: str  # float | int | bool
    unit: str  # e.g. log_ratio, index_points, hours, share, z_score, code
    source: str  # target_series | detector_state | supporting_series | catalog | content | meta
    definition: str
    availability: str  # what must be knowable at the prediction time
    missing_policy: MissingPolicy = MissingPolicy.NAN
    scale_dependent: bool = False  # only meaningful for consistently scaled indices
    providers: tuple[str, ...] = ("any",)
    version: str = "1"
