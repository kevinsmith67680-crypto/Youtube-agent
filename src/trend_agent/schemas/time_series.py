"""Canonical time-series observation (Phase 3).

Time semantics (never interchangeable, all timezone-aware UTC):

* ``period_start``/``period_end`` - the observation window the value describes (event time
  window, half-open). ``event_time`` - for point events (a release), when it happened.
* ``provider_published_at`` - when the provider published this value, if it says so.
* ``first_available_at`` - earliest time the value is known to have been obtainable (e.g.
  the publication time of the first revision); ``None`` when unknown.
* ``collected_at`` - when *we* fetched it. ``ingested_at`` - when we stored it.

``source_timezone`` keeps the provider's own time zone when it matters for interpreting
the window (e.g. a "day" in America/Los_Angeles); the window itself is stored in UTC.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trend_agent.schemas.enums import (
    VALUE_FORBIDDEN_QUALITIES,
    VALUE_REQUIRED_QUALITIES,
    DataNature,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.monitoring import MeasurementCategory, QualityStatus
from trend_agent.schemas.observation import NormalizationContext
from trend_agent.utils.time import ensure_utc


def _hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SeriesIdentity(BaseModel):
    """Everything that defines one time series; observations in it share a scale."""

    model_config = ConfigDict(frozen=True)

    query_id: int
    provider_name: str
    measurement_category: MeasurementCategory
    simulated_category: MeasurementCategory | None = None
    search_property: SearchProperty
    region: str
    language: str | None
    granularity: TimeGranularity
    scale_type: ScaleType
    unit: str
    normalization_key: str = Field(min_length=64, max_length=64)

    def key(self) -> str:
        return _hash(self.model_dump(mode="json"))


class ObservationDraft(BaseModel):
    """One value ready to be written (validated, not yet stored)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: int
    target_id: int | None
    provider_name: str
    provider_version: str
    measurement_category: MeasurementCategory
    simulated_category: MeasurementCategory | None = None
    is_proxy: bool = False
    unit: str = Field(min_length=1, max_length=40)
    provider_observation_id: str | None = None

    period_start: datetime
    period_end: datetime
    granularity: TimeGranularity
    event_time: datetime | None = None
    source_timezone: str | None = None
    provider_published_at: datetime | None = None
    first_available_at: datetime | None = None
    collected_at: datetime

    value: float | None
    raw_value: float | None = None
    scale_type: ScaleType
    normalization_context: NormalizationContext
    region: str
    language: str | None
    search_property: SearchProperty
    data_nature: DataNature
    sample_quality: SampleQuality
    source_reference: str = Field(min_length=1)
    data_policy_id: str
    expires_at: datetime | None = None

    @field_validator(
        "period_start",
        "period_end",
        "event_time",
        "provider_published_at",
        "first_available_at",
        "collected_at",
        "expires_at",
    )
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _consistent(self) -> ObservationDraft:
        if self.period_end < self.period_start:
            raise ValueError("period_end before period_start")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("value must be finite")
        if self.sample_quality in VALUE_REQUIRED_QUALITIES and self.value is None:
            raise ValueError(f"{self.sample_quality.value} requires a value")
        if self.sample_quality in VALUE_FORBIDDEN_QUALITIES and self.value is not None:
            raise ValueError(f"{self.sample_quality.value} must not carry a value (not zero)")
        synthetic = self.measurement_category is MeasurementCategory.SYNTHETIC_TEST_OBSERVATION
        if synthetic != (self.data_nature is DataNature.SYNTHETIC):
            raise ValueError("synthetic category and data nature must agree")
        if self.simulated_category is not None and not synthetic:
            raise ValueError("simulated_category is only allowed on synthetic observations")
        if self.provider_published_at and self.provider_published_at > self.collected_at:
            raise ValueError("published after it was collected")
        return self

    @property
    def is_synthetic(self) -> bool:
        return self.data_nature is DataNature.SYNTHETIC

    @property
    def effective_category(self) -> MeasurementCategory:
        return self.simulated_category or self.measurement_category

    def identity(self) -> SeriesIdentity:
        return SeriesIdentity(
            query_id=self.query_id,
            provider_name=self.provider_name,
            measurement_category=self.measurement_category,
            simulated_category=self.simulated_category,
            search_property=self.search_property,
            region=self.region,
            language=self.language,
            granularity=self.granularity,
            scale_type=self.scale_type,
            unit=self.unit,
            normalization_key=self.normalization_context.key(),
        )

    def quality_status(self) -> QualityStatus:
        caveats = (
            self.sample_quality in (SampleQuality.PARTIAL_PERIOD, SampleQuality.LOW_VOLUME)
            or self.provider_published_at is None
        )
        return QualityStatus.CAVEAT if caveats else QualityStatus.VALID

    def same_measurement(self, value: float | None, quality: SampleQuality) -> bool:
        return self.value == value and self.sample_quality is quality


class StoredPoint(BaseModel):
    """A stored observation revision as read back for analysis / replay."""

    model_config = ConfigDict(frozen=True)

    id: int
    series_id: int
    target_id: int | None
    provider_name: str
    measurement_category: MeasurementCategory
    simulated_category: MeasurementCategory | None
    period_start: datetime
    period_end: datetime
    granularity: TimeGranularity
    value: float | None
    sample_quality: SampleQuality
    scale_type: ScaleType
    normalization_key: str
    unit: str
    region: str
    language: str | None
    provider_version: str
    provider_published_at: datetime | None
    first_available_at: datetime | None
    collected_at: datetime
    ingested_at: datetime
    revision: int
    revision_kind: str
    is_current: bool
    supersedes_id: int | None
    quality_status: QualityStatus
    is_synthetic: bool
    is_proxy: bool
    source_reference: str
    data_policy_id: str
    job_id: str | None = None

    @property
    def effective_category(self) -> MeasurementCategory:
        return self.simulated_category or self.measurement_category
