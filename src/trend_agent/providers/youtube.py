"""YouTube Data API v3 provider (official, documented endpoints only).

Endpoints used: ``search.list``, ``videos.list`` (by id and ``chart=mostPopular``),
``channels.list`` and ``videoCategories.list``. None of these exposes search volume; all
output is labelled ``MeasurementType.VIDEO_ACTIVITY``.

Security: the API key is sent in the ``X-Goog-Api-Key`` header, never in the URL, so it
cannot leak through logged URLs, exception messages or ``source_reference`` values.
"""

from __future__ import annotations

import asyncio
import html
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Self
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from trend_agent.config import Settings
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderDisabledError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.http import RetryPolicy, Sleep, call_with_retries
from trend_agent.providers.quota import (
    EndpointCost,
    QuotaBucket,
    QuotaManager,
    QuotaPolicy,
    QuotaStore,
)
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
from trend_agent.schemas.provider import (
    CapabilityStatus,
    PopularVideosRequest,
    ProviderDescriptor,
    ProviderStatusReport,
    RetentionRule,
    VideoSearchRequest,
)
from trend_agent.schemas.video import (
    ChannelDetails,
    ChannelDetailsResponse,
    ChannelStatistics,
    PopularVideosResponse,
    Thumbnail,
    VideoCategory,
    VideoDetails,
    VideoDetailsResponse,
    VideoSearchResponse,
    VideoSearchResult,
    VideoStatistics,
)
from trend_agent.utils.codes import normalize_region
from trend_agent.utils.time import Clock, format_rfc3339, parse_iso8601_duration, parse_rfc3339
from trend_agent.utils.time import utc_now as _utc_now

PROVIDER_NAME = "youtube_data_api"
PROVIDER_VERSION = "1.0.0"
DEFAULT_BASE_URL = "https://www.googleapis.com/youtube/v3"
API_KEY_ENV = "TREND_AGENT_YOUTUBE_API_KEY"

ENDPOINT_SEARCH = "search.list"
ENDPOINT_VIDEOS = "videos.list"
ENDPOINT_CHANNELS = "channels.list"
ENDPOINT_CATEGORIES = "videoCategories.list"
ENDPOINT_PLAYLIST_ITEMS = "playlistItems.list"  # Phase 2: monitored channel uploads

BUCKET_SEARCH = "search"
BUCKET_DEFAULT = "default"

MAX_RESULTS_PER_PAGE = 50  # documented maximum for search.list and videos.list
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,100}$")
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"})
_INVALID_KEY_REASONS = frozenset({"keyInvalid", "keyExpired", "API_KEY_INVALID", "API_KEY_EXPIRED"})
_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"})
_ACCESS_REASONS = frozenset(
    {
        "accessNotConfigured",
        "SERVICE_DISABLED",
        "forbidden",
        "ipRefererBlocked",
        "keyServiceBlocked",
        "API_KEY_SERVICE_BLOCKED",
        "API_KEY_HTTP_REFERRER_BLOCKED",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "API_KEY_ANDROID_APP_BLOCKED",
        "API_KEY_IOS_APP_BLOCKED",
        "CONSUMER_INVALID",
    }
)

_VIDEO_PARTS = "snippet,contentDetails,statistics"


def youtube_quota_policy(settings: Settings) -> QuotaPolicy:
    """Quota buckets per Google's documented default allocation (configurable).

    ``search.list`` has its own bucket counted in calls; the other read endpoints used here
    cost 1 unit each from the shared daily unit pool. ``videos.insert`` is never called.
    """
    margin = settings.youtube_quota_safety_margin_pct
    return QuotaPolicy(
        provider_name=PROVIDER_NAME,
        reset_timezone=settings.youtube_quota_timezone,
        buckets={
            BUCKET_SEARCH: QuotaBucket(
                BUCKET_SEARCH, settings.youtube_search_daily_call_limit, margin
            ),
            BUCKET_DEFAULT: QuotaBucket(BUCKET_DEFAULT, settings.youtube_daily_unit_limit, margin),
        },
        endpoint_costs={
            ENDPOINT_SEARCH: EndpointCost(BUCKET_SEARCH, 1),
            ENDPOINT_VIDEOS: EndpointCost(BUCKET_DEFAULT, 1),
            ENDPOINT_CHANNELS: EndpointCost(BUCKET_DEFAULT, 1),
            ENDPOINT_CATEGORIES: EndpointCost(BUCKET_DEFAULT, 1),
            ENDPOINT_PLAYLIST_ITEMS: EndpointCost(BUCKET_DEFAULT, 1),
        },
    )


@dataclass(frozen=True)
class YouTubeRetentionConfig:
    metadata_days: int = 30
    statistics_days: int = 30
    persist_statistics: bool = False
    persist_descriptions: bool = True

    def rules(self) -> tuple[RetentionRule, ...]:
        meta = timedelta(days=self.metadata_days)
        note = (
            "YouTube API Services Developer Policies: stored API data must be refreshed or "
            "deleted within 30 days."
        )
        return (
            RetentionRule(
                record_kind=RecordKind.VIDEO,
                persist_allowed=True,
                max_retention=meta,
                refresh_required=True,
                notes=note,
            ),
            RetentionRule(
                record_kind=RecordKind.CHANNEL,
                persist_allowed=True,
                max_retention=meta,
                refresh_required=True,
                notes=note,
            ),
            RetentionRule(
                record_kind=RecordKind.VIDEO_SEARCH_RESULT,
                persist_allowed=True,
                max_retention=meta,
                refresh_required=False,
                notes=note + " Search result snapshots are deleted at expiry.",
            ),
            RetentionRule(
                record_kind=RecordKind.VIDEO_STATISTICS,
                persist_allowed=self.persist_statistics,
                max_retention=timedelta(days=self.statistics_days),
                refresh_required=True,
                notes=(
                    "Disabled by default. Storing statistics and building derived metrics "
                    "may require additional YouTube API Services authorization; enable only "
                    "after confirming your project's permitted use case."
                ),
            ),
        )


class YouTubeDataApiProvider:
    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        http_client: httpx.AsyncClient,
        quota: QuotaManager,
        retry: RetryPolicy,
        base_url: str = DEFAULT_BASE_URL,
        enabled: bool = True,
        max_pages: int = 3,
        max_ids_per_call: int = MAX_RESULTS_PER_PAGE,
        max_concurrency: int = 4,
        retention: YouTubeRetentionConfig | None = None,
        clock: Clock = _utc_now,
        sleep: Sleep = asyncio.sleep,
        owns_client: bool = False,
    ) -> None:
        if not 1 <= max_ids_per_call <= MAX_RESULTS_PER_PAGE:
            raise ValueError("max_ids_per_call must be within 1..50")
        self._api_key = api_key
        self._client = http_client
        self._quota = quota
        self._retry = retry
        self._base_url = base_url.rstrip("/")
        self._enabled = enabled
        self._max_pages = max_pages
        self._max_ids = max_ids_per_call
        self._max_concurrency = max_concurrency
        self._retention = retention or YouTubeRetentionConfig()
        self._clock = clock
        self._sleep = sleep
        self._owns_client = owns_client
        self._descriptor = ProviderDescriptor(
            name=PROVIDER_NAME,
            display_name="YouTube Data API v3",
            version=PROVIDER_VERSION,
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=frozenset(
                {
                    Capability.VIDEO_SEARCH,
                    Capability.VIDEO_STATISTICS,
                    Capability.CHANNEL_METADATA,
                    Capability.TREND_DISCOVERY,
                }
            ),
            measurement_types=frozenset({MeasurementType.VIDEO_ACTIVITY}),
            search_properties=frozenset({SearchProperty.NOT_APPLICABLE}),
            supported_regions=None,
            supported_granularities=frozenset({TimeGranularity.POINT}),
            requires_authentication=True,
            historical_data_available=False,
            data_nature=DataNature.ACTUAL,
            enabled=enabled,
            retention=self._retention.rules(),
            usage_restrictions=(
                "Does not provide search volume or search interest for any keyword.",
                "search.list results are a ranked sample of matching videos, not demand.",
                "Stored API data must be refreshed or deleted within 30 days.",
                "Statistics persistence is disabled unless explicitly enabled.",
                "Requires attribution to YouTube wherever API data is displayed.",
            ),
            documentation_urls=(
                "https://developers.google.com/youtube/v3/docs",
                "https://developers.google.com/youtube/v3/determine_quota_cost",
                "https://developers.google.com/youtube/terms/developer-policies",
            ),
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        quota_store: QuotaStore,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Clock = _utc_now,
    ) -> Self:
        owns = http_client is None
        client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "youtube-trend-agent/0.1"},
        )
        return cls(
            api_key=settings.youtube_api_key,
            http_client=client,
            quota=QuotaManager(youtube_quota_policy(settings), quota_store, clock),
            retry=RetryPolicy(
                max_attempts=settings.http_max_attempts,
                base_delay=settings.http_backoff_base_seconds,
                max_delay=settings.http_backoff_max_seconds,
            ),
            base_url=settings.youtube_api_base_url,
            enabled=settings.youtube_enabled,
            max_pages=settings.youtube_max_pages_per_request,
            max_ids_per_call=settings.youtube_max_ids_per_videos_call,
            max_concurrency=settings.ingestion_max_concurrency,
            retention=YouTubeRetentionConfig(
                metadata_days=settings.youtube_metadata_retention_days,
                statistics_days=settings.youtube_statistics_retention_days,
                persist_statistics=settings.youtube_persist_statistics,
                persist_descriptions=settings.youtube_persist_descriptions,
            ),
            clock=clock,
            owns_client=owns,
        )

    # ------------------------------------------------------------------ protocol
    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def quota(self) -> QuotaManager:
        return self._quota

    @property
    def retention_config(self) -> YouTubeRetentionConfig:
        return self._retention

    def status(self) -> ProviderStatusReport:
        now = self._clock()
        caps = sorted(self._descriptor.capabilities)
        if not self._enabled:
            return self._report(
                ProviderStatus.DISABLED, "Provider disabled by configuration.", caps
            )
        if self._api_key is None:
            return self._report(
                ProviderStatus.NOT_CONFIGURED,
                f"No API key configured. Set {API_KEY_ENV}.",
                caps,
                missing=(API_KEY_ENV,),
            )
        try:
            exhausted = {u.bucket for u in self._quota.status() if u.exhausted}
        except Exception as exc:  # quota store unavailable (e.g. database down)
            return ProviderStatusReport(
                provider_name=PROVIDER_NAME,
                status=ProviderStatus.AVAILABLE,
                detail=f"API key set; quota state unavailable ({type(exc).__name__}).",
                capabilities=tuple(
                    CapabilityStatus(capability=c, status=ProviderStatus.AVAILABLE) for c in caps
                ),
                checked_at=now,
            )
        statuses = []
        for cap in caps:
            bucket = BUCKET_SEARCH if cap is Capability.VIDEO_SEARCH else BUCKET_DEFAULT
            if bucket in exhausted:
                statuses.append(
                    CapabilityStatus(
                        capability=cap,
                        status=ProviderStatus.QUOTA_EXHAUSTED,
                        detail=f"estimated '{bucket}' quota exhausted for today",
                    )
                )
            else:
                statuses.append(CapabilityStatus(capability=cap, status=ProviderStatus.AVAILABLE))
        overall = (
            ProviderStatus.QUOTA_EXHAUSTED
            if all(s.status is ProviderStatus.QUOTA_EXHAUSTED for s in statuses)
            else ProviderStatus.AVAILABLE
        )
        detail = "API key set (validity is only confirmed by a live request)."
        return ProviderStatusReport(
            provider_name=PROVIDER_NAME,
            status=overall,
            detail=detail,
            capabilities=tuple(statuses),
            checked_at=now,
        )

    def _report(
        self,
        status: ProviderStatus,
        detail: str,
        caps: list[Capability],
        missing: tuple[str, ...] = (),
    ) -> ProviderStatusReport:
        return ProviderStatusReport(
            provider_name=PROVIDER_NAME,
            status=status,
            detail=detail,
            capabilities=tuple(CapabilityStatus(capability=c, status=status) for c in caps),
            missing_configuration=missing,
            checked_at=self._clock(),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ operations
    async def search_videos(
        self, request: VideoSearchRequest, ctx: RequestContext
    ) -> VideoSearchResponse:
        base: dict[str, str] = {
            "part": "snippet",
            "type": "video",
            "q": request.query,
            "order": request.order.value,
        }
        if request.region:
            base["regionCode"] = normalize_region(
                request.region, allow_world=False, allow_subdivision=False
            )
        if request.language:
            base["relevanceLanguage"] = request.language
        if request.published_after:
            base["publishedAfter"] = format_rfc3339(request.published_after)
        if request.published_before:
            base["publishedBefore"] = format_rfc3339(request.published_before)

        results: list[VideoSearchResult] = []
        warnings: list[str] = []
        page_token: str | None = None
        pages = 0
        approx_total: int | None = None
        # Pages can hold fewer items than requested, so the page count is bounded only by
        # the configured hard limit, the requested result count and the next-page token.
        while pages < self._max_pages and len(results) < request.max_results:
            params = dict(base)
            params["maxResults"] = str(
                min(MAX_RESULTS_PER_PAGE, request.max_results - len(results))
            )
            if page_token:
                params["pageToken"] = page_token
            data = await self._get_json(ENDPOINT_SEARCH, "search", params, ctx)
            pages += 1
            items = _require_list(data, "items", ENDPOINT_SEARCH)
            if approx_total is None:
                approx_total = _opt_int(_get(data, "pageInfo", "totalResults"))
            for index, item in enumerate(items):
                if len(results) >= request.max_results:
                    break
                try:
                    results.append(_parse_search_item(item, rank=len(results) + 1, page=pages))
                except _ItemError as exc:
                    warnings.append(f"page {pages} item {index}: {exc}")
            next_token = data.get("nextPageToken")
            page_token = next_token if isinstance(next_token, str) and next_token else None
            if page_token is None or not items:
                break

        return VideoSearchResponse(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            query=request.query,
            region=base.get("regionCode"),
            language=request.language,
            order=request.order.value,
            published_after=request.published_after,
            published_before=request.published_before,
            results=tuple(results),
            pages_fetched=pages,
            more_available=page_token is not None,
            approx_matching_videos=approx_total,
            collected_at=self._clock(),
            source_reference=self._source_reference("search", base),
            warnings=tuple(warnings),
        )

    async def get_videos(
        self, video_ids: Sequence[str], ctx: RequestContext
    ) -> VideoDetailsResponse:
        ids = _dedupe_ids(video_ids, _VIDEO_ID, "video")
        batches = [ids[i : i + self._max_ids] for i in range(0, len(ids), self._max_ids)]
        pages = await self._run_bounded(
            [
                self._bind_get(
                    ENDPOINT_VIDEOS,
                    "videos",
                    {"part": _VIDEO_PARTS, "id": ",".join(batch), "maxResults": str(len(batch))},
                    ctx,
                )
                for batch in batches
            ]
        )
        collected_at = self._clock()
        videos: list[VideoDetails] = []
        warnings: list[str] = []
        for data in pages:
            for index, item in enumerate(_require_list(data, "items", ENDPOINT_VIDEOS)):
                try:
                    videos.append(_parse_video_item(item, collected_at))
                except _ItemError as exc:
                    warnings.append(f"item {index}: {exc}")
        returned = {v.video_id for v in videos}
        return VideoDetailsResponse(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            videos=tuple(videos),
            requested_ids=tuple(ids),
            missing_ids=tuple(i for i in ids if i not in returned),
            collected_at=collected_at,
            source_reference=self._source_reference("videos", {"part": _VIDEO_PARTS}),
            warnings=tuple(warnings),
        )

    async def get_channels(
        self, channel_ids: Sequence[str], ctx: RequestContext
    ) -> ChannelDetailsResponse:
        ids = _dedupe_ids(channel_ids, _CHANNEL_ID, "channel")
        batches = [ids[i : i + self._max_ids] for i in range(0, len(ids), self._max_ids)]
        parts = "snippet,statistics"
        pages = await self._run_bounded(
            [
                self._bind_get(
                    ENDPOINT_CHANNELS,
                    "channels",
                    {"part": parts, "id": ",".join(batch), "maxResults": str(len(batch))},
                    ctx,
                )
                for batch in batches
            ]
        )
        collected_at = self._clock()
        channels: list[ChannelDetails] = []
        warnings: list[str] = []
        for data in pages:
            for index, item in enumerate(_require_list(data, "items", ENDPOINT_CHANNELS)):
                try:
                    channels.append(_parse_channel_item(item, collected_at))
                except _ItemError as exc:
                    warnings.append(f"item {index}: {exc}")
        returned = {c.channel_id for c in channels}
        return ChannelDetailsResponse(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            channels=tuple(channels),
            missing_ids=tuple(i for i in ids if i not in returned),
            collected_at=collected_at,
            source_reference=self._source_reference("channels", {"part": parts}),
            warnings=tuple(warnings),
        )

    async def popular_videos(
        self, request: PopularVideosRequest, ctx: RequestContext
    ) -> PopularVideosResponse:
        base: dict[str, str] = {
            "part": _VIDEO_PARTS,
            "chart": "mostPopular",
            "regionCode": request.region,
        }
        if request.category_id:
            base["videoCategoryId"] = request.category_id
        videos: list[VideoDetails] = []
        warnings: list[str] = []
        page_token: str | None = None
        pages = 0
        while pages < self._max_pages and len(videos) < request.max_results:
            params = dict(base)
            params["maxResults"] = str(min(MAX_RESULTS_PER_PAGE, request.max_results - len(videos)))
            if page_token:
                params["pageToken"] = page_token
            data = await self._get_json(ENDPOINT_VIDEOS, "videos", params, ctx)
            pages += 1
            collected_at = self._clock()
            items = _require_list(data, "items", ENDPOINT_VIDEOS)
            for index, item in enumerate(items):
                if len(videos) >= request.max_results:
                    break
                try:
                    video = _parse_video_item(item, collected_at)
                except _ItemError as exc:
                    warnings.append(f"page {pages} item {index}: {exc}")
                    continue
                videos.append(video.model_copy(update={"rank": len(videos) + 1}))
            next_token = data.get("nextPageToken")
            page_token = next_token if isinstance(next_token, str) and next_token else None
            if page_token is None or not items:
                break
        return PopularVideosResponse(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            region=request.region,
            category_id=request.category_id,
            videos=tuple(videos),
            pages_fetched=pages,
            more_available=page_token is not None,
            collected_at=self._clock(),
            source_reference=self._source_reference("videos", base),
            warnings=tuple(warnings),
        )

    async def video_categories(self, region: str, ctx: RequestContext) -> list[VideoCategory]:
        code = normalize_region(region, allow_world=False, allow_subdivision=False)
        data = await self._get_json(
            ENDPOINT_CATEGORIES, "videoCategories", {"part": "snippet", "regionCode": code}, ctx
        )
        categories: list[VideoCategory] = []
        for item in _require_list(data, "items", ENDPOINT_CATEGORIES):
            try:
                categories.append(
                    VideoCategory(
                        category_id=_req_str(item, "id"),
                        title=_req_str(item, "snippet", "title"),
                        assignable=bool(_get(item, "snippet", "assignable")),
                    )
                )
            except _ItemError:
                continue
        return categories

    async def channel_uploads(
        self, channel_ref: str, ctx: RequestContext, max_results: int = 25
    ) -> VideoSearchResponse:
        """Recent uploads of a channel given as '@handle' or channel id.

        Costs 2 units from the default bucket (``channels.list`` to resolve the uploads
        playlist, then one ``playlistItems.list`` page) instead of a ``search.list`` call.
        Results are wrapped as a search response (rank = upload order) for reuse.
        """
        ref = channel_ref.strip()
        if _HANDLE.match(ref):
            lookup = {"part": "contentDetails,snippet", "forHandle": ref}
        elif _CHANNEL_ID.match(ref):
            lookup = {"part": "contentDetails,snippet", "id": ref}
        else:
            raise InvalidRequestError(
                f"invalid channel reference {channel_ref!r}", provider=PROVIDER_NAME
            )
        data = await self._get_json(ENDPOINT_CHANNELS, "channels", lookup, ctx)
        items = _require_list(data, "items", ENDPOINT_CHANNELS)
        if not items:
            raise InvalidRequestError(f"channel {channel_ref!r} not found", provider=PROVIDER_NAME)
        uploads = _opt_str(items[0], "contentDetails", "relatedPlaylists", "uploads")
        if uploads is None:
            raise MalformedResponseError("channel has no uploads playlist", provider=PROVIDER_NAME)
        params = {
            "part": "snippet",
            "playlistId": uploads,
            "maxResults": str(max(1, min(MAX_RESULTS_PER_PAGE, max_results))),
        }
        page = await self._get_json(ENDPOINT_PLAYLIST_ITEMS, "playlistItems", params, ctx)
        results: list[VideoSearchResult] = []
        warnings: list[str] = []
        for index, item in enumerate(_require_list(page, "items", ENDPOINT_PLAYLIST_ITEMS)):
            try:
                description = _opt_str(item, "snippet", "description")
                results.append(
                    VideoSearchResult(
                        video_id=_req_str(item, "snippet", "resourceId", "videoId"),
                        title=_req_str(item, "snippet", "title"),
                        description=description,
                        channel_id=_opt_str(item, "snippet", "channelId") or ref,
                        channel_title=_opt_str(item, "snippet", "channelTitle"),
                        published_at=_opt_datetime(item, "snippet", "publishedAt"),
                        thumbnails=_thumbnails(item),
                        rank=len(results) + 1,
                        page=1,
                    )
                )
            except _ItemError as exc:
                warnings.append(f"item {index}: {exc}")
        return VideoSearchResponse(
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            query=f"uploads:{ref}",
            region=None,
            language=None,
            order="date",
            published_after=None,
            published_before=None,
            results=tuple(results),
            pages_fetched=1,
            more_available=isinstance(page.get("nextPageToken"), str),
            collected_at=self._clock(),
            source_reference=self._source_reference("playlistItems", {"playlistId": uploads}),
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------ transport
    def _require_key(self) -> str:
        if not self._enabled:
            raise ProviderDisabledError("YouTube provider is disabled", provider=PROVIDER_NAME)
        if self._api_key is None:
            raise ProviderNotConfiguredError(
                f"YouTube Data API key is not configured. Set {API_KEY_ENV} "
                "(see README: 'Environment configuration').",
                missing=(API_KEY_ENV,),
                provider=PROVIDER_NAME,
            )
        return self._api_key.get_secret_value()

    def _bind_get(
        self, endpoint: str, path: str, params: dict[str, str], ctx: RequestContext
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def run() -> dict[str, Any]:
            return await self._get_json(endpoint, path, params, ctx)

        return run

    async def _run_bounded(
        self, calls: list[Callable[[], Awaitable[dict[str, Any]]]]
    ) -> list[dict[str, Any]]:
        """Run calls with bounded concurrency; the first failure cancels the rest."""
        if not calls:
            return []
        semaphore = asyncio.Semaphore(self._max_concurrency)
        results: list[dict[str, Any] | None] = [None] * len(calls)

        async def worker(index: int, call: Callable[[], Awaitable[dict[str, Any]]]) -> None:
            async with semaphore:
                results[index] = await call()

        try:
            async with asyncio.TaskGroup() as group:
                for index, call in enumerate(calls):
                    group.create_task(worker(index, call))
        except ExceptionGroup as group_error:
            raise _first_leaf(group_error) from None
        return [r for r in results if r is not None]

    async def _get_json(
        self, endpoint: str, path: str, params: Mapping[str, str], ctx: RequestContext
    ) -> dict[str, Any]:
        key = self._require_key()
        url = f"{self._base_url}/{path}"

        async def attempt() -> dict[str, Any]:
            self._quota.reserve(endpoint, ctx.ledger)
            try:
                response = await self._client.get(
                    url, params=dict(params), headers={"X-Goog-Api-Key": key}
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    f"{endpoint} timed out ({type(exc).__name__})", provider=PROVIDER_NAME
                ) from None
            except httpx.TransportError as exc:
                raise TemporarilyUnavailableError(
                    f"{endpoint} network error ({type(exc).__name__})", provider=PROVIDER_NAME
                ) from None
            return self._handle_response(endpoint, response)

        return await call_with_retries(attempt, self._retry, op_name=endpoint, sleep=self._sleep)

    def _handle_response(self, endpoint: str, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError:
                raise MalformedResponseError(
                    f"{endpoint} returned a non-JSON body",
                    provider=PROVIDER_NAME,
                    http_status=status,
                ) from None
            if not isinstance(data, dict):
                raise MalformedResponseError(
                    f"{endpoint} returned JSON that is not an object",
                    provider=PROVIDER_NAME,
                    http_status=status,
                )
            return data

        message, reasons = _parse_google_error(response)
        reason = ",".join(sorted(reasons)) or None
        text = f"{endpoint} failed with HTTP {status}: {message}"
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        common: dict[str, Any] = {
            "provider": PROVIDER_NAME,
            "http_status": status,
            "reason": reason,
        }

        if reasons & _QUOTA_REASONS:
            window = self._quota.record_server_exhaustion(endpoint)
            bucket = self._quota.policy.cost_of(endpoint).bucket
            raise QuotaExhaustedError(
                text, bucket=bucket, resets_at=window.end, source="server", **common
            )
        if reasons & _INVALID_KEY_REASONS or status == 401:
            raise InvalidCredentialsError(text, **common)
        if reasons & _RATE_REASONS or status == 429:
            raise RateLimitedError(text, retry_after=retry_after, **common)
        if reasons & _ACCESS_REASONS or status == 403:
            raise AccessRestrictedError(text, **common)
        if status >= 500:
            raise TemporarilyUnavailableError(text, retry_after=retry_after, **common)
        raise InvalidRequestError(text, **common)

    def _source_reference(self, path: str, params: Mapping[str, str]) -> str:
        public = {k: v for k, v in sorted(params.items()) if k not in {"key", "pageToken"}}
        return f"{self._base_url}/{path}?{urlencode(public)}"


# ---------------------------------------------------------------------- parsing helpers


class _ItemError(ValueError):
    """A single response item is malformed; the item is skipped with a warning."""


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    first = group.exceptions[0]
    if isinstance(first, BaseExceptionGroup):
        return _first_leaf(first)
    return first


def _get(data: Any, *path: str) -> Any:
    for part in path:
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _req_str(data: Any, *path: str) -> str:
    value = _get(data, *path)
    if not isinstance(value, str) or not value:
        raise _ItemError(f"missing or invalid field {'.'.join(path)}")
    return value


def _opt_str(data: Any, *path: str) -> str | None:
    value = _get(data, *path)
    return value if isinstance(value, str) and value else None


def _opt_int(value: Any) -> int | None:
    """Parse a non-negative integer counter; absent or invalid -> None (never 0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _opt_datetime(data: Any, *path: str) -> datetime | None:
    value = _opt_str(data, *path)
    if value is None:
        return None
    try:
        return parse_rfc3339(value)
    except ValueError as exc:
        raise _ItemError(f"invalid timestamp in {'.'.join(path)}") from exc


def _thumbnails(data: Any) -> dict[str, Thumbnail]:
    raw = _get(data, "snippet", "thumbnails")
    if not isinstance(raw, dict):
        return {}
    thumbs: dict[str, Thumbnail] = {}
    for name, item in raw.items():
        url = _opt_str(item, "url")
        if url:
            thumbs[str(name)] = Thumbnail(
                url=url, width=_opt_int(_get(item, "width")), height=_opt_int(_get(item, "height"))
            )
    return thumbs


def _require_list(data: dict[str, Any], key: str, endpoint: str) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise MalformedResponseError(
            f"{endpoint} response field {key!r} is not a list", provider=PROVIDER_NAME
        )
    return value


def _parse_search_item(item: Any, *, rank: int, page: int) -> VideoSearchResult:
    if _get(item, "id", "kind") not in (None, "youtube#video"):
        raise _ItemError(f"unexpected result kind {_get(item, 'id', 'kind')!r}")
    description = _opt_str(item, "snippet", "description")
    channel_title = _opt_str(item, "snippet", "channelTitle")
    return VideoSearchResult(
        video_id=_req_str(item, "id", "videoId"),
        # search.list returns HTML-escaped snippet text (videos.list does not).
        title=html.unescape(_req_str(item, "snippet", "title")),
        description=html.unescape(description) if description else None,
        channel_id=_req_str(item, "snippet", "channelId"),
        channel_title=html.unescape(channel_title) if channel_title else None,
        published_at=_opt_datetime(item, "snippet", "publishedAt"),
        thumbnails=_thumbnails(item),
        live_broadcast_content=_opt_str(item, "snippet", "liveBroadcastContent"),
        rank=rank,
        page=page,
    )


def _parse_video_item(item: Any, collected_at: datetime) -> VideoDetails:
    raw_stats = _get(item, "statistics")
    stats = (
        VideoStatistics(
            view_count=_opt_int(raw_stats.get("viewCount")),
            like_count=_opt_int(raw_stats.get("likeCount")),
            comment_count=_opt_int(raw_stats.get("commentCount")),
        )
        if isinstance(raw_stats, dict)
        else None
    )
    tags = _get(item, "snippet", "tags")
    duration_iso = _opt_str(item, "contentDetails", "duration")
    return VideoDetails(
        video_id=_req_str(item, "id"),
        title=_req_str(item, "snippet", "title"),
        description=_opt_str(item, "snippet", "description"),
        channel_id=_req_str(item, "snippet", "channelId"),
        channel_title=_opt_str(item, "snippet", "channelTitle"),
        published_at=_opt_datetime(item, "snippet", "publishedAt"),
        duration_seconds=parse_iso8601_duration(duration_iso),
        duration_iso=duration_iso,
        category_id=_opt_str(item, "snippet", "categoryId"),
        tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
        default_language=_opt_str(item, "snippet", "defaultLanguage"),
        default_audio_language=_opt_str(item, "snippet", "defaultAudioLanguage"),
        live_broadcast_content=_opt_str(item, "snippet", "liveBroadcastContent"),
        thumbnails=_thumbnails(item),
        statistics=stats,
        collected_at=collected_at,
    )


def _parse_channel_item(item: Any, collected_at: datetime) -> ChannelDetails:
    raw_stats = _get(item, "statistics")
    stats = None
    if isinstance(raw_stats, dict):
        hidden = raw_stats.get("hiddenSubscriberCount") is True
        stats = ChannelStatistics(
            subscriber_count=None if hidden else _opt_int(raw_stats.get("subscriberCount")),
            view_count=_opt_int(raw_stats.get("viewCount")),
            video_count=_opt_int(raw_stats.get("videoCount")),
        )
    return ChannelDetails(
        channel_id=_req_str(item, "id"),
        title=_req_str(item, "snippet", "title"),
        description=_opt_str(item, "snippet", "description"),
        custom_url=_opt_str(item, "snippet", "customUrl"),
        country=_opt_str(item, "snippet", "country"),
        published_at=_opt_datetime(item, "snippet", "publishedAt"),
        thumbnails=_thumbnails(item),
        statistics=stats,
        collected_at=collected_at,
    )


def _dedupe_ids(ids: Sequence[str], pattern: re.Pattern[str], kind: str) -> list[str]:
    seen: dict[str, None] = {}
    for raw in ids:
        value = raw.strip() if isinstance(raw, str) else ""
        if not pattern.match(value):
            raise InvalidRequestError(f"invalid {kind} id {raw!r}", provider=PROVIDER_NAME)
        seen.setdefault(value, None)
    return list(seen)


def _parse_google_error(response: httpx.Response) -> tuple[str, set[str]]:
    """Extract (message, reasons) from a Google API error body, tolerating any shape."""
    try:
        body = response.json()
    except ValueError:
        return (response.reason_phrase or "error", set())
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return (response.reason_phrase or "error", set())
    reasons: set[str] = set()
    for key in ("errors", "details"):
        entries = error.get(key)
        if isinstance(entries, list):
            for entry in entries:
                reason = entry.get("reason") if isinstance(entry, dict) else None
                if isinstance(reason, str):
                    reasons.add(reason)
    message = error.get("message")
    return (message if isinstance(message, str) else "error", reasons)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form is not used by Google APIs; ignore rather than guess
    return seconds if seconds >= 0 and math.isfinite(seconds) else None


__all__ = [
    "PROVIDER_NAME",
    "PROVIDER_VERSION",
    "ProviderError",
    "YouTubeDataApiProvider",
    "YouTubeRetentionConfig",
    "youtube_quota_policy",
]
