"""Provider interfaces.

Providers are plain classes that structurally satisfy these protocols; there is no base
class to inherit from. Application code depends only on these protocols and on the
provider-neutral schemas, never on a provider's raw response format.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from trend_agent.providers.errors import CapabilityNotSupportedError
from trend_agent.providers.quota import QuotaLedger
from trend_agent.schemas.enums import Capability
from trend_agent.schemas.provider import (
    PopularVideosRequest,
    ProviderDescriptor,
    ProviderStatusReport,
    SearchInterestBatch,
    SearchInterestRequest,
    VideoSearchRequest,
)
from trend_agent.schemas.video import (
    ChannelDetailsResponse,
    PopularVideosResponse,
    VideoCategory,
    VideoDetailsResponse,
    VideoSearchResponse,
)


@dataclass
class RequestContext:
    """Per-operation context threaded through provider calls (quota ledger + budget)."""

    ledger: QuotaLedger = field(default_factory=QuotaLedger)


@runtime_checkable
class Provider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def status(self) -> ProviderStatusReport:
        """Readiness without network I/O or quota consumption."""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class SearchInterestProvider(Provider, Protocol):
    async def fetch_search_interest(
        self, request: SearchInterestRequest, ctx: RequestContext
    ) -> SearchInterestBatch: ...


@runtime_checkable
class VideoDataProvider(Provider, Protocol):
    async def search_videos(
        self, request: VideoSearchRequest, ctx: RequestContext
    ) -> VideoSearchResponse: ...

    async def get_videos(
        self, video_ids: Sequence[str], ctx: RequestContext
    ) -> VideoDetailsResponse: ...

    async def get_channels(
        self, channel_ids: Sequence[str], ctx: RequestContext
    ) -> ChannelDetailsResponse: ...

    async def popular_videos(
        self, request: PopularVideosRequest, ctx: RequestContext
    ) -> PopularVideosResponse: ...

    async def video_categories(self, region: str, ctx: RequestContext) -> list[VideoCategory]: ...


def require_capability(descriptor: ProviderDescriptor, capability: Capability) -> None:
    if not descriptor.supports(capability):
        raise CapabilityNotSupportedError(
            f"provider {descriptor.name!r} does not support capability {capability.value!r}",
            provider=descriptor.name,
        )
