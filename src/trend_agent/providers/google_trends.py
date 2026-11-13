"""Google Trends search-interest adapter.

Status of official access (see docs/data_sources.md):
  The official Google Trends API is a *limited-access alpha*. Its request/response
  contract is provided to admitted testers and could not be verified for this project.
  This module therefore does NOT invent endpoints or payload formats.

What is implemented:
  * The provider-neutral mapping from Trends-style points to canonical observations
    (granularity, scale, normalisation context, property -> measurement type, low-volume
    handling). This is the part downstream correctness depends on.
  * A ``TrendsTransport`` protocol. An operator who has alpha access implements it against
    the contract Google gave them and passes it in; until then the provider reports
    ``NOT_CONFIGURED`` or ``ACCESS_RESTRICTED`` and never returns fabricated data.

Unofficial endpoints (e.g. scraping the Trends website) are intentionally not implemented:
they are undocumented, unstable, rate-limited/CAPTCHA-protected and their use may breach
Google's Terms of Service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trend_agent.config import Settings, TrendsAccessMode
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    ProviderNotConfiguredError,
)
from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    MeasurementType,
    ProviderStatus,
    ProviderType,
    RecordKind,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.observation import NormalizationContext, ObservationCandidate
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderDescriptor,
    ProviderStatusReport,
    RetentionRule,
    SearchInterestBatch,
    SearchInterestRequest,
)
from trend_agent.utils.codes import WORLD
from trend_agent.utils.time import Clock, utc_now

PROVIDER_NAME = "google_trends"
PROVIDER_VERSION = "0.1.0"
ACCESS_MODE_ENV = "TREND_AGENT_GOOGLE_TRENDS_ACCESS_MODE"
CREDENTIALS_ENV = "TREND_AGENT_GOOGLE_TRENDS_CREDENTIALS_FILE"
ENDPOINT_ENV = "TREND_AGENT_GOOGLE_TRENDS_ENDPOINT"

# Features Google has publicly *announced* for the alpha API. Listed for operator
# information only; none of these is relied upon until a verified transport declares it.
PUBLICLY_ANNOUNCED_FEATURES: tuple[str, ...] = (
    "Limited-access alpha; access by application only (announced July 2025).",
    "Consistently scaled search interest (comparable across requests), per announcement.",
    "Daily, weekly, monthly and yearly aggregation, per announcement.",
    "Rolling ~5-year window, with data up to roughly two days before the request.",
    "Region and sub-region breakdowns, per announcement.",
    "No announced real-time or hourly data; YouTube-property support unconfirmed.",
)


@dataclass(frozen=True)
class TrendsPoint:
    """One value from a Trends-style series, in transport-neutral form."""

    term: str
    window_start: datetime
    window_end: datetime
    value: float | None
    is_partial: bool = False
    low_volume: bool = False
    suppressed: bool = False


@dataclass(frozen=True)
class TrendsResponse:
    """What a transport must report alongside its points: the true measurement context."""

    points: tuple[TrendsPoint, ...]
    granularity: TimeGranularity  # the granularity actually returned, not the one requested
    scale_type: ScaleType
    scale_method: str
    scale_version: str
    batch_id: str
    co_normalized_terms: tuple[str, ...]
    geography: str
    search_property: SearchProperty
    category: str | None
    data_nature: DataNature
    source_reference: str
    available_at: datetime | None = None
    zero_means_insufficient_data: bool = True


class TrendsTransport(Protocol):
    """Implemented by an operator against the contract Google provided to them."""

    @property
    def verified(self) -> bool:
        """True only if the implementation was validated against the real API contract."""
        ...

    @property
    def supported_properties(self) -> frozenset[SearchProperty]: ...

    @property
    def supported_granularities(self) -> frozenset[TimeGranularity]: ...

    async def fetch(self, request: SearchInterestRequest) -> TrendsResponse: ...

    async def aclose(self) -> None: ...


def measurement_type_for(prop: SearchProperty) -> MeasurementType:
    """Only YouTube-property data may be labelled as direct YouTube search interest."""
    if prop is SearchProperty.YOUTUBE_SEARCH:
        return MeasurementType.DIRECT_SEARCH_INTEREST
    if prop in {SearchProperty.NOT_APPLICABLE, SearchProperty.UNKNOWN}:
        return MeasurementType.UNKNOWN
    return MeasurementType.EXTERNAL_SEARCH_INTEREST


def map_trends_response(
    response: TrendsResponse,
    request: SearchInterestRequest,
    *,
    provider_name: str,
    provider_version: str,
    collected_at: datetime,
) -> list[ObservationCandidate]:
    context = NormalizationContext(
        scale_type=response.scale_type,
        method=response.scale_method,
        scale_version=response.scale_version,
        batch_id=response.batch_id,
        co_normalized_terms=response.co_normalized_terms,
        reference_geography=response.geography,
        reference_start=request.start,
        reference_end=request.end,
        granularity=response.granularity,
        search_property=response.search_property,
        category=response.category,
    )
    measurement = measurement_type_for(response.search_property)
    candidates: list[ObservationCandidate] = []
    for point in response.points:
        candidates.append(
            ObservationCandidate(
                query=point.term,
                query_language=request.language,
                query_region=request.region,
                provider_name=provider_name,
                provider_version=provider_version,
                measurement_type=measurement.value,
                search_property=response.search_property.value,
                observation_start=point.window_start,
                observation_end=point.window_end,
                granularity=response.granularity.value,
                collected_at=collected_at,
                available_at=response.available_at,
                value=point.value,
                scale_type=response.scale_type.value,
                normalization_context=context,
                geography=response.geography,
                language=None,  # Trends does not filter by language
                sample_quality=_quality(point, response).value,
                data_nature=response.data_nature.value,
                source_reference=response.source_reference,
            )
        )
    return candidates


def _quality(point: TrendsPoint, response: TrendsResponse) -> SampleQuality:
    if point.suppressed:
        return SampleQuality.SUPPRESSED
    if point.value is None:
        return SampleQuality.MISSING
    if point.is_partial:
        return SampleQuality.PARTIAL_PERIOD
    if point.low_volume or (response.zero_means_insufficient_data and point.value == 0):
        # Google Trends documents 0 as "not enough data", which is not zero demand.
        return SampleQuality.LOW_VOLUME
    return SampleQuality.COMPLETE


class GoogleTrendsProvider:
    def __init__(
        self,
        *,
        access_mode: TrendsAccessMode,
        credentials_file: str | None,
        endpoint: str | None,
        retention_days: int | None,
        transport: TrendsTransport | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._access_mode = access_mode
        self._credentials_file = credentials_file
        self._endpoint = endpoint
        self._transport = transport
        self._clock = clock
        verified = transport is not None and transport.verified
        properties = (
            transport.supported_properties
            if transport is not None and verified
            else frozenset({SearchProperty.GOOGLE_WEB_SEARCH})
        )
        granularities = (
            transport.supported_granularities if transport is not None and verified else frozenset()
        )
        self._descriptor = ProviderDescriptor(
            name=PROVIDER_NAME,
            display_name="Google Trends API (official, limited-access alpha)",
            version=PROVIDER_VERSION,
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.SEARCH_INTEREST}),
            measurement_types=frozenset(measurement_type_for(p) for p in properties),
            search_properties=properties,
            supported_regions=None,
            supported_granularities=granularities,
            requires_authentication=True,
            historical_data_available=True,
            data_nature=DataNature.SAMPLED if verified else DataNature.UNAVAILABLE,
            enabled=access_mode is not TrendsAccessMode.DISABLED,
            retention=(
                RetentionRule(
                    record_kind=RecordKind.SEARCH_INTEREST_OBSERVATION,
                    persist_allowed=retention_days is not None,
                    max_retention=(
                        timedelta(days=retention_days) if retention_days is not None else None
                    ),
                    notes=(
                        "Persistence requires an operator-set retention period consistent "
                        "with the Google Trends API access terms."
                    ),
                ),
            ),
            usage_restrictions=(
                "Values are relative/indexed search interest, never absolute search counts.",
                "Web Search data must never be labelled as YouTube search demand.",
                "Access is subject to the Google Trends API alpha terms.",
            ),
            documentation_urls=("https://developers.google.com/search/apis/trends",),
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: TrendsTransport | None = None,
        clock: Clock = utc_now,
    ) -> GoogleTrendsProvider:
        return cls(
            access_mode=settings.google_trends_access_mode,
            credentials_file=settings.google_trends_credentials_file,
            endpoint=settings.google_trends_endpoint,
            retention_days=settings.google_trends_retention_days,
            transport=transport,
            clock=clock,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _missing_configuration(self) -> tuple[str, ...]:
        missing = []
        if self._access_mode is TrendsAccessMode.DISABLED:
            missing.append(ACCESS_MODE_ENV)
        if not self._credentials_file:
            missing.append(CREDENTIALS_ENV)
        if not self._endpoint:
            missing.append(ENDPOINT_ENV)
        return tuple(missing)

    def status(self) -> ProviderStatusReport:
        missing = self._missing_configuration()
        if missing:
            status = ProviderStatus.NOT_CONFIGURED
            detail = (
                "Google Trends API access is not configured. The official API is a "
                "limited-access alpha; apply for access, then set: " + ", ".join(missing) + "."
            )
        elif self._transport is None or not self._transport.verified:
            status = ProviderStatus.ACCESS_RESTRICTED
            detail = (
                "Configuration present, but no verified transport for the alpha API contract "
                "is installed. No data will be fetched or fabricated."
            )
        else:
            status = ProviderStatus.AVAILABLE
            detail = "Verified transport installed."
        return ProviderStatusReport(
            provider_name=PROVIDER_NAME,
            status=status,
            detail=detail,
            capabilities=(
                CapabilityStatus(
                    capability=Capability.SEARCH_INTEREST, status=status, detail=detail
                ),
            ),
            missing_configuration=missing,
            checked_at=self._clock(),
        )

    async def fetch_search_interest(
        self, request: SearchInterestRequest, ctx: RequestContext
    ) -> SearchInterestBatch:
        report = self.status()
        if report.status is ProviderStatus.NOT_CONFIGURED:
            raise ProviderNotConfiguredError(
                report.detail, missing=report.missing_configuration, provider=PROVIDER_NAME
            )
        if report.status is not ProviderStatus.AVAILABLE or self._transport is None:
            raise AccessRestrictedError(report.detail, provider=PROVIDER_NAME)
        if request.search_property not in self._descriptor.search_properties:
            raise CapabilityNotSupportedError(
                f"search property {request.search_property.value!r} is not supported by the "
                "configured Google Trends transport",
                provider=PROVIDER_NAME,
            )
        if request.granularity not in self._descriptor.supported_granularities:
            raise CapabilityNotSupportedError(
                f"granularity {request.granularity.value!r} is not supported",
                provider=PROVIDER_NAME,
            )
        response = await self._transport.fetch(request)
        collected_at = self._clock()
        candidates = map_trends_response(
            response,
            request,
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            collected_at=collected_at,
        )
        warnings: list[str] = []
        if response.granularity is not request.granularity:
            warnings.append(
                f"requested {request.granularity.value} granularity, provider returned "
                f"{response.granularity.value}; observations carry the actual granularity"
            )
        if response.geography != (request.region or WORLD):
            warnings.append(
                f"requested geography {request.region or WORLD}, got {response.geography}"
            )
        return SearchInterestBatch(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            request=request,
            observations=tuple(candidates),
            warnings=tuple(warnings),
            source_reference=response.source_reference,
        )

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()
