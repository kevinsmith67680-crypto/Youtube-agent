"""YouTube discovery adapter on top of the Phase 1 YouTube Data API provider.

Shares that provider's quota manager, so discovery and ingestion draw on the same daily
buckets. Targeted requests only:

* API_QUERY / EXPLORATION_QUERY sources -> one ``search.list`` call (search bucket),
  newest uploads matching the query since the lookback window.
* CHART sources -> one ``videos.list chart=mostPopular`` call for a region/category.
* CHANNEL sources -> ``channels.list`` + ``playlistItems.list`` (2 units).

Each video becomes a ``VIDEO_TITLE`` item (its description is kept as extraction context).
Video activity is supply/attention evidence; it is never treated as search demand.
"""

from __future__ import annotations

from datetime import timedelta

from trend_agent.discovery.models import (
    DiscoveryItem,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import (
    REQUEST_BUCKET,
    REQUEST_ENDPOINT,
    afford,
    discovery_descriptor,
)
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import InvalidRequestError
from trend_agent.providers.quota import QuotaManager
from trend_agent.providers.youtube import BUCKET_DEFAULT, BUCKET_SEARCH, YouTubeDataApiProvider
from trend_agent.schemas.enums import Capability, MeasurementType, ProviderStatus, ProviderType
from trend_agent.schemas.provider import (
    CapabilityStatus,
    PopularVideosRequest,
    ProviderDescriptor,
    ProviderStatusReport,
    VideoSearchOrder,
    VideoSearchRequest,
)
from trend_agent.schemas.video import VideoSearchResult

PROVIDER_NAME = "youtube_discovery"
MAX_TEXT = 1500
QUERY_TYPES = frozenset({SourceType.API_QUERY, SourceType.EXPLORATION_QUERY})


class YouTubeDiscoveryProvider:
    def __init__(
        self,
        youtube: YouTubeDataApiProvider,
        *,
        enabled: bool,
        retention_days: int | None,
        results_per_query: int = 25,
        default_region: str = "US",
        default_language: str | None = "en",
        discovery_quota: QuotaManager | None = None,
    ) -> None:
        self._yt = youtube
        # Optional cap on discovery's own share of the YouTube quota (one unit per fetch),
        # so discovery cannot spend the whole search.list budget other phases need.
        self._discovery_quota = discovery_quota
        self._enabled = enabled
        self._per_query = results_per_query
        self._region = default_region
        self._language = default_language
        # YouTube API data must be refreshed or deleted within 30 days.
        days = min(retention_days or 30, youtube.retention_config.metadata_days, 30)
        self._descriptor = discovery_descriptor(
            name=PROVIDER_NAME,
            display_name="YouTube Data API (discovery)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset({Capability.TREND_DISCOVERY, Capability.VIDEO_SEARCH}),
            requires_authentication=True,
            enabled=enabled,
            retention_days=days,
            measurement_types=frozenset({MeasurementType.VIDEO_ACTIVITY}),
            usage_restrictions=(
                "Video titles/descriptions are supply evidence, not search demand.",
                "Stored items expire within 30 days (YouTube API Services policies).",
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        base = self._yt.status()
        if not self._enabled:
            status = ProviderStatus.DISABLED
            detail = "Disabled by discovery configuration."
        else:
            status, detail = base.status, base.detail
        return ProviderStatusReport(
            provider_name=PROVIDER_NAME,
            status=status,
            detail=detail,
            capabilities=(
                CapabilityStatus(
                    capability=Capability.TREND_DISCOVERY, status=status, detail=detail
                ),
            ),
            missing_configuration=base.missing_configuration,
            checked_at=base.checked_at,
        )

    async def aclose(self) -> None:
        return None  # the wrapped provider is closed by the registry

    def supports(self, source: DiscoverySource) -> bool:
        return source.source_type in QUERY_TYPES | {SourceType.CHART, SourceType.CHANNEL}

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        if self._discovery_quota is not None:
            ok, why = afford(self._discovery_quota, REQUEST_BUCKET, 1)
            if not ok:
                return False, f"discovery cap: {why}"
        if source.source_type in QUERY_TYPES:
            return afford(self._yt.quota, BUCKET_SEARCH, 1)
        cost = 2 if source.source_type is SourceType.CHANNEL else 1
        return afford(self._yt.quota, BUCKET_DEFAULT, cost)

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        source = request.source
        if self._discovery_quota is not None:
            self._discovery_quota.reserve(REQUEST_ENDPOINT, ctx.ledger)
        region = source.region or self._region
        if source.source_type in QUERY_TYPES:
            response = await self._yt.search_videos(
                VideoSearchRequest(
                    query=source.reference,
                    region=region,
                    language=source.language or self._language,
                    published_after=request.since,
                    max_results=min(request.max_items, self._per_query),
                    order=VideoSearchOrder.DATE,
                ),
                ctx,
            )
            results = list(response.results)
            collected, warnings = response.collected_at, response.warnings
        elif source.source_type is SourceType.CHART:
            popular = await self._yt.popular_videos(
                PopularVideosRequest(
                    region=region,
                    category_id=source.reference or None,
                    max_results=min(request.max_items, 50),
                ),
                ctx,
            )
            results = [
                VideoSearchResult(
                    video_id=v.video_id,
                    title=v.title,
                    description=v.description,
                    channel_id=v.channel_id,
                    channel_title=v.channel_title,
                    published_at=v.published_at,
                    rank=v.rank or 1,
                    page=1,
                )
                for v in popular.videos
            ]
            collected, warnings = popular.collected_at, popular.warnings
        elif source.source_type is SourceType.CHANNEL:
            uploads = await self._yt.channel_uploads(source.reference, ctx, request.max_items)
            results = list(uploads.results)
            collected, warnings = uploads.collected_at, uploads.warnings
        else:
            raise InvalidRequestError(
                f"unsupported source type {source.source_type.value}", provider=PROVIDER_NAME
            )

        items = []
        for video in results:
            if (
                request.since
                and video.published_at
                and video.published_at < request.since - timedelta(days=1)
            ):
                continue
            items.append(
                DiscoveryItem(
                    provider_name=PROVIDER_NAME,
                    source_key=source.key,
                    source_reference=video.url,
                    evidence_type=source.evidence_type or EvidenceType.VIDEO_TITLE,
                    title=video.title[:1000],
                    text=(video.description or "")[:MAX_TEXT] or None,
                    url=video.url,
                    publisher=f"youtube.com/channel/{video.channel_id}",
                    author=video.channel_id,
                    published_at=video.published_at,
                    collected_at=collected,
                    category_hint=source.category,
                    metadata={
                        "video_id": video.video_id,
                        "channel_title": video.channel_title,
                        "rank": video.rank,
                        "description_included": video.description is not None,
                    },
                )
            )
        return DiscoveryResult(
            provider_name=PROVIDER_NAME,
            source_key=source.key,
            items=tuple(items),
            warnings=tuple(warnings),
        )
