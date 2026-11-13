"""Provider-neutral video and channel domain models.

These models are populated by provider adapters; nothing downstream depends on the raw
YouTube response shape. Statistics that are hidden or unavailable are ``None`` - never 0.
All counts here are supply / attention indicators (``MeasurementType.VIDEO_ACTIVITY``),
never measurements of search demand.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from trend_agent.schemas.enums import MeasurementType


class Thumbnail(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    width: int | None = None
    height: int | None = None


class VideoStatistics(BaseModel):
    """Public counters at ``collected_at``. ``None`` means hidden/unavailable, not zero."""

    model_config = ConfigDict(frozen=True)

    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None


class VideoSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    video_id: str
    title: str
    description: str | None
    channel_id: str
    channel_title: str | None
    published_at: datetime | None
    thumbnails: dict[str, Thumbnail] = Field(default_factory=dict)
    live_broadcast_content: str | None = None
    rank: int = Field(ge=1, description="1-based position across all fetched pages")
    page: int = Field(ge=1)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class VideoSearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str
    provider_version: str
    measurement_type: MeasurementType = MeasurementType.VIDEO_ACTIVITY
    query: str
    region: str | None
    language: str | None
    order: str
    published_after: datetime | None
    published_before: datetime | None
    results: tuple[VideoSearchResult, ...]
    pages_fetched: int
    more_available: bool
    approx_matching_videos: int | None = Field(
        default=None,
        description=(
            "Provider's approximate count of matching *videos* (pageInfo.totalResults). "
            "A supply indicator only; it is NOT a number of searches."
        ),
    )
    collected_at: datetime
    source_reference: str
    warnings: tuple[str, ...] = Field(
        default=(), description="Per-item parse problems; affected items were skipped"
    )


class VideoDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    video_id: str
    title: str
    description: str | None
    channel_id: str
    channel_title: str | None
    published_at: datetime | None
    duration_seconds: int | None
    duration_iso: str | None
    category_id: str | None
    tags: tuple[str, ...] = ()
    default_language: str | None = None
    default_audio_language: str | None = None
    live_broadcast_content: str | None = None
    thumbnails: dict[str, Thumbnail] = Field(default_factory=dict)
    statistics: VideoStatistics | None = None
    collected_at: datetime
    rank: int | None = Field(default=None, description="Position in a chart, if applicable")

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class VideoDetailsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str
    provider_version: str
    videos: tuple[VideoDetails, ...]
    requested_ids: tuple[str, ...]
    missing_ids: tuple[str, ...] = Field(
        description="Requested IDs not returned (deleted, private or otherwise unavailable)"
    )
    collected_at: datetime
    source_reference: str
    warnings: tuple[str, ...] = Field(
        default=(), description="Per-item parse problems; affected items were skipped"
    )


class PopularVideosResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str
    provider_version: str
    measurement_type: MeasurementType = MeasurementType.VIDEO_ACTIVITY
    region: str
    category_id: str | None
    videos: tuple[VideoDetails, ...]
    pages_fetched: int
    more_available: bool
    collected_at: datetime
    source_reference: str
    warnings: tuple[str, ...] = Field(
        default=(), description="Per-item parse problems; affected items were skipped"
    )


class ChannelStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)

    subscriber_count: int | None = None  # None when hidden by the channel
    view_count: int | None = None
    video_count: int | None = None


class ChannelDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    title: str
    description: str | None
    custom_url: str | None
    country: str | None
    published_at: datetime | None
    thumbnails: dict[str, Thumbnail] = Field(default_factory=dict)
    statistics: ChannelStatistics | None = None
    collected_at: datetime


class ChannelDetailsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str
    provider_version: str
    channels: tuple[ChannelDetails, ...]
    missing_ids: tuple[str, ...]
    collected_at: datetime
    source_reference: str
    warnings: tuple[str, ...] = Field(
        default=(), description="Per-item parse problems; affected items were skipped"
    )


class VideoCategory(BaseModel):
    model_config = ConfigDict(frozen=True)

    category_id: str
    title: str
    assignable: bool
