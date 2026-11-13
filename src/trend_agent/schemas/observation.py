"""Search-interest observation contracts.

Two models are used deliberately:

* :class:`ObservationCandidate` is what a provider emits. Its fields are loosely typed so
  that malformed provider output can still be represented, validated, and *quarantined
  with a diagnostic reason* rather than crashing ingestion.
* :class:`ValidatedObservation` is the strict, canonical record produced by
  ``ingestion.validation``. Only validated observations may be persisted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trend_agent.schemas.enums import (
    DataNature,
    MeasurementType,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.utils.time import ensure_utc


class NormalizationContext(BaseModel):
    """Everything needed to decide whether two values share a scale.

    For ``RELATIVE_PER_REQUEST`` scales (e.g. Google Trends website-style 0-100 values),
    a value is only meaningful relative to the other values produced by the same request:
    the same co-normalised terms, geography, time range, property and the same sample. The
    ``batch_id`` identifies that request/sample and is mandatory for such scales.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scale_type: ScaleType
    method: str = Field(min_length=1, description="Provider-specific scaling method id")
    scale_version: str = Field(default="1", min_length=1)
    batch_id: str | None = Field(
        default=None, description="Identifier of the normalisation batch / sample"
    )
    co_normalized_terms: tuple[str, ...] = ()
    reference_geography: str | None = None
    reference_start: datetime | None = None
    reference_end: datetime | None = None
    granularity: TimeGranularity | None = None
    search_property: SearchProperty | None = None
    category: str | None = None
    notes: str | None = None

    @field_validator("co_normalized_terms")
    @classmethod
    def _sorted_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))

    @field_validator("reference_start", "reference_end")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def key(self) -> str:
        """Stable hash identifying this exact normalisation context."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ObservationCandidate(BaseModel):
    """Unvalidated observation as emitted by a provider adapter."""

    model_config = ConfigDict(extra="forbid")

    query: str
    query_language: str | None = None
    query_region: str | None = None

    provider_name: str | None
    provider_version: str | None
    measurement_type: str
    search_property: str

    observation_start: datetime | None
    observation_end: datetime | None
    granularity: str
    collected_at: datetime | None
    available_at: datetime | None = None

    value: float | None
    scale_type: str
    normalization_context: NormalizationContext | None
    geography: str | None
    language: str | None = None
    sample_quality: str
    data_nature: str
    source_reference: str | None

    def diagnostic_payload(self) -> dict[str, Any]:
        """JSON-safe representation used when quarantining a rejected candidate."""
        return self.model_dump(mode="json")


class ValidatedObservation(BaseModel):
    """Canonical, validated search-interest observation (all timestamps UTC)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_query: str
    original_query: str
    query_language: str | None
    query_region: str | None

    provider_name: str
    provider_version: str
    measurement_type: MeasurementType
    search_property: SearchProperty

    # Event time is the half-open window [observation_start, observation_end). For
    # instantaneous snapshots (granularity POINT) start == end.
    observation_start: datetime
    observation_end: datetime
    granularity: TimeGranularity
    collected_at: datetime
    available_at: datetime | None

    value: float | None
    scale_type: ScaleType
    normalization_context: NormalizationContext
    geography: str
    language: str | None
    sample_quality: SampleQuality
    data_nature: DataNature
    source_reference: str

    @property
    def is_synthetic(self) -> bool:
        return self.data_nature is DataNature.SYNTHETIC

    @property
    def normalization_key(self) -> str:
        return self.normalization_context.key()

    def identity(self) -> tuple[Any, ...]:
        """Natural key used for idempotency (mirrors the database unique constraint)."""
        return (
            self.canonical_query,
            self.query_language,
            self.query_region,
            self.provider_name,
            self.measurement_type,
            self.search_property,
            self.geography,
            self.language,
            self.granularity,
            self.observation_start,
            self.observation_end,
            self.normalization_key,
        )

    def same_measurement(self, other: ValidatedObservation) -> bool:
        """True when two records with the same identity carry the same measurement."""
        return (
            self.value == other.value
            and self.sample_quality == other.sample_quality
            and self.data_nature == other.data_nature
        )
