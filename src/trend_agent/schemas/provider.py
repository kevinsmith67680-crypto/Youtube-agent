"""Provider declarations, status reports and request contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    MeasurementType,
    ProviderStatus,
    ProviderType,
    RecordKind,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.observation import ObservationCandidate
from trend_agent.utils.codes import normalize_language, normalize_region
from trend_agent.utils.time import ensure_utc


class RetentionRule(BaseModel):
    """Source-specific storage permission for one kind of record."""

    model_config = ConfigDict(frozen=True)

    record_kind: RecordKind
    persist_allowed: bool
    max_retention: timedelta | None = Field(
        default=None, description="Maximum storage period; None = no source-imposed limit"
    )
    refresh_required: bool = False
    notes: str = ""


class ProviderDescriptor(BaseModel):
    """Static declaration of what a provider is and what it can do."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    display_name: str
    version: str
    compatible_versions: frozenset[str] = Field(
        default_factory=frozenset,
        description="Earlier versions whose output is interpreted identically",
    )
    provider_type: ProviderType
    capabilities: frozenset[Capability]
    measurement_types: frozenset[MeasurementType]
    search_properties: frozenset[SearchProperty]
    supported_regions: frozenset[str] | None = Field(
        default=None, description="None = any ISO 3166-1 alpha-2 region (and WORLD)"
    )
    supported_granularities: frozenset[TimeGranularity]
    requires_authentication: bool
    historical_data_available: bool
    data_nature: DataNature
    enabled: bool
    retention: tuple[RetentionRule, ...] = ()
    usage_restrictions: tuple[str, ...] = ()
    documentation_urls: tuple[str, ...] = ()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def accepts_version(self, version: str) -> bool:
        return version == self.version or version in self.compatible_versions

    def retention_for(self, kind: RecordKind) -> RetentionRule:
        """Retention rule for a record kind. Undeclared kinds may not be persisted."""
        for rule in self.retention:
            if rule.record_kind is kind:
                return rule
        return RetentionRule(
            record_kind=kind,
            persist_allowed=False,
            notes="No retention rule declared by provider; persistence denied by default.",
        )

    def supports_region(self, region: str) -> bool:
        return self.supported_regions is None or region in self.supported_regions


class CapabilityStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: Capability
    status: ProviderStatus
    detail: str = ""


class ProviderStatusReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str
    status: ProviderStatus
    detail: str
    capabilities: tuple[CapabilityStatus, ...]
    missing_configuration: tuple[str, ...] = ()
    checked_at: datetime

    def capability_status(self, capability: Capability) -> ProviderStatus:
        for item in self.capabilities:
            if item.capability is capability:
                return item.status
        return ProviderStatus.UNSUPPORTED


# ---------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------


class _RegionLanguageMixin(BaseModel):
    region: str | None = None
    language: str | None = None

    @field_validator("region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        return normalize_region(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        return normalize_language(value) if value is not None else None


class SearchInterestRequest(_RegionLanguageMixin):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime
    granularity: TimeGranularity = TimeGranularity.DAY
    search_property: SearchProperty = SearchProperty.YOUTUBE_SEARCH
    provider_options: dict[str, str] = Field(default_factory=dict)

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _window(self) -> SearchInterestRequest:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class SearchInterestBatch(BaseModel):
    """Raw provider output for one request; candidates still need validation."""

    model_config = ConfigDict(frozen=True)

    provider_name: str
    provider_version: str
    request: SearchInterestRequest
    observations: tuple[ObservationCandidate, ...]
    warnings: tuple[str, ...] = ()
    source_reference: str


class VideoSearchOrder(StrEnum):
    """``search.list`` ``order`` values documented by YouTube Data API v3."""

    RELEVANCE = "relevance"
    DATE = "date"
    RATING = "rating"
    TITLE = "title"
    VIEW_COUNT = "viewCount"


class VideoSearchRequest(_RegionLanguageMixin):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=200)
    published_after: datetime | None = None
    published_before: datetime | None = None
    max_results: int = Field(default=25, ge=1, le=500)
    order: VideoSearchOrder = VideoSearchOrder.RELEVANCE

    @field_validator("published_after", "published_before")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _range(self) -> VideoSearchRequest:
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_before <= self.published_after
        ):
            raise ValueError("published_before must be after published_after")
        return self


class PopularVideosRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    region: str = "US"
    category_id: str | None = Field(default=None, pattern=r"^\d{1,4}$")
    max_results: int = Field(default=25, ge=1, le=200)

    @field_validator("region")
    @classmethod
    def _region(cls, value: str) -> str:
        return normalize_region(value, allow_world=False, allow_subdivision=False)
