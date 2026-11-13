"""Providers that need no network: Google Trends status adapter and curated seeds.

The Google Trends adapter exposes related/rising queries only if the configured Trends
transport (Phase 1, limited-access alpha) is available *and* implements a related-queries
call. Otherwise it reports the exact limitation and returns nothing - related queries are
never fabricated.
"""

from __future__ import annotations

from typing import Any

from trend_agent.discovery.config import slug
from trend_agent.discovery.models import (
    DiscoveryItem,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import discovery_descriptor, simple_status
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    ProviderNotConfiguredError,
)
from trend_agent.providers.google_trends import GoogleTrendsProvider
from trend_agent.schemas.enums import Capability, MeasurementType, ProviderStatus, ProviderType
from trend_agent.schemas.provider import ProviderDescriptor, ProviderStatusReport
from trend_agent.utils.time import Clock


class TrendsRelatedQueriesProvider:
    NAME = "google_trends_discovery"

    def __init__(self, trends: GoogleTrendsProvider, *, enabled: bool, clock: Clock) -> None:
        self._trends = trends
        self._enabled = enabled
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=self.NAME,
            display_name="Google Trends related queries (official API alpha)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.SEARCH_SUGGESTIONS}),
            requires_authentication=True,
            enabled=enabled,
            retention_days=None,
            measurement_types=frozenset({MeasurementType.SEARCH_SUGGESTION}),
            usage_restrictions=(
                "Related queries are discovery signals, not search volume.",
                "Requires Google Trends API alpha access and a transport implementing it.",
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _transport_related(self) -> Any:
        transport = getattr(self._trends, "_transport", None)
        return getattr(transport, "fetch_related_queries", None)

    def status(self) -> ProviderStatusReport:
        base = self._trends.status()
        if base.status is not ProviderStatus.AVAILABLE:
            return simple_status(
                self.NAME,
                self._enabled,
                base.detail,
                self._clock(),
                capability=Capability.SEARCH_SUGGESTIONS,
                status=base.status if self._enabled else None,
                missing=base.missing_configuration,
            )
        if self._transport_related() is None:
            return simple_status(
                self.NAME,
                self._enabled,
                "Trends transport is available but does not implement related queries.",
                self._clock(),
                capability=Capability.SEARCH_SUGGESTIONS,
                status=ProviderStatus.UNSUPPORTED if self._enabled else None,
            )
        return simple_status(
            self.NAME,
            self._enabled,
            "Related queries available.",
            self._clock(),
            capability=Capability.SEARCH_SUGGESTIONS,
        )

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.API_QUERY

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return True, ""

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        report = self.status()
        if report.status is ProviderStatus.NOT_CONFIGURED:
            raise ProviderNotConfiguredError(
                report.detail, missing=report.missing_configuration, provider=self.NAME
            )
        if report.status is ProviderStatus.UNSUPPORTED:
            raise CapabilityNotSupportedError(report.detail, provider=self.NAME)
        if report.status is not ProviderStatus.AVAILABLE:
            raise AccessRestrictedError(report.detail, provider=self.NAME)
        fetch = self._transport_related()
        queries: list[str] = list(await fetch(request.source.reference, request.source.region))
        now = self._clock()
        source = request.source
        return DiscoveryResult(
            provider_name=self.NAME,
            source_key=source.key,
            items=(
                DiscoveryItem(
                    provider_name=self.NAME,
                    source_key=source.key,
                    source_reference=f"trends:related:{slug(source.reference)}:{now.date()}",
                    evidence_type=EvidenceType.RELATED_QUERY,
                    title=f"Related queries for {source.reference}",
                    collected_at=now,
                    category_hint=source.category,
                    observed_queries=tuple(queries[: request.max_items]),
                ),
            )
            if queries
            else (),
        )


class CuratedSeedProvider:
    """Turns configured seed terms into CURATED_SEED items (never evidence of demand)."""

    NAME = "curated_seeds"

    def __init__(self, *, enabled: bool, clock: Clock) -> None:
        self._enabled = enabled
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=self.NAME,
            display_name="Curated seed topics (configuration)",
            version="1.0.0",
            provider_type=ProviderType.LICENSED_THIRD_PARTY,
            capabilities=frozenset({Capability.TREND_DISCOVERY}),
            requires_authentication=False,
            enabled=enabled,
            retention_days=None,
            usage_restrictions=("Seeds are starting points chosen by the operator, not evidence.",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        return simple_status(self.NAME, self._enabled, "Reads config/seeds/*.yaml.", self._clock())

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type is SourceType.SEED

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return True, ""

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        item = DiscoveryItem(
            provider_name=self.NAME,
            source_key=source.key,
            source_reference=source.key,
            evidence_type=EvidenceType.CURATED_SEED,
            title=source.reference,
            collected_at=self._clock(),
            category_hint=source.category,
            publisher="operator-config",
            metadata={
                "track_as_keyword": bool(source.params.get("track_as_keyword")),
                **source.params,
            },
        )
        return DiscoveryResult(provider_name=self.NAME, source_key=source.key, items=(item,))
