"""Video, channel, statistics and search-result persistence with expiry tracking."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from trend_agent.db.models import (
    ChannelRow,
    RejectedRecordRow,
    VideoRow,
    VideoSearchResultRow,
    VideoStatisticsRow,
)
from trend_agent.schemas.video import (
    ChannelDetails,
    Thumbnail,
    VideoDetails,
    VideoSearchResult,
    VideoStatistics,
)


def _thumbs(thumbnails: dict[str, Thumbnail]) -> dict[str, object]:
    return {name: t.model_dump(mode="json") for name, t in thumbnails.items()}


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class VideoRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert_video_details(
        self,
        video: VideoDetails,
        *,
        provider_id: int,
        expires_at: datetime,
        persist_description: bool,
    ) -> None:
        """Full metadata from ``videos.list``: overwrites the row and resets its expiry."""
        values = {
            "video_id": video.video_id,
            "channel_id": video.channel_id,
            "channel_title": video.channel_title,
            "title": video.title,
            "description": video.description if persist_description else None,
            "published_at": video.published_at,
            "duration_seconds": video.duration_seconds,
            "duration_iso": video.duration_iso,
            "category_id": video.category_id,
            "tags": list(video.tags),
            "default_language": video.default_language,
            "default_audio_language": video.default_audio_language,
            "live_broadcast_content": video.live_broadcast_content,
            "thumbnails": _thumbs(video.thumbnails),
            "source_provider_id": provider_id,
            "metadata_collected_at": video.collected_at,
            "metadata_expires_at": expires_at,
        }
        stmt = pg_insert(VideoRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[VideoRow.video_id],
            set_={
                **{k: stmt.excluded[k] for k in values if k != "video_id"},
                "updated_at": sa.func.now(),
            },
        )
        self._s.execute(stmt)

    def insert_video_from_search(
        self,
        result: VideoSearchResult,
        *,
        provider_id: int,
        collected_at: datetime,
        expires_at: datetime,
        persist_description: bool,
    ) -> bool:
        """Create a minimal row from a search snippet if none exists.

        Never updates an existing row: a partial snippet must not extend the expiry of
        fields (duration, tags, ...) that were collected earlier and not refreshed.
        """
        stmt = (
            pg_insert(VideoRow)
            .values(
                video_id=result.video_id,
                channel_id=result.channel_id,
                channel_title=result.channel_title,
                title=result.title,
                description=result.description if persist_description else None,
                published_at=result.published_at,
                live_broadcast_content=result.live_broadcast_content,
                thumbnails=_thumbs(result.thumbnails),
                source_provider_id=provider_id,
                metadata_collected_at=collected_at,
                metadata_expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=[VideoRow.video_id])
            .returning(VideoRow.video_id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def upsert_channel(
        self, channel: ChannelDetails, *, provider_id: int, expires_at: datetime
    ) -> None:
        values = {
            "channel_id": channel.channel_id,
            "title": channel.title,
            "description": channel.description,
            "custom_url": channel.custom_url,
            "country": channel.country,
            "published_at": channel.published_at,
            "thumbnails": _thumbs(channel.thumbnails),
            "source_provider_id": provider_id,
            "metadata_collected_at": channel.collected_at,
            "metadata_expires_at": expires_at,
        }
        stmt = pg_insert(ChannelRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChannelRow.channel_id],
            set_={
                **{k: stmt.excluded[k] for k in values if k != "channel_id"},
                "updated_at": sa.func.now(),
            },
        )
        self._s.execute(stmt)

    def insert_statistics(
        self,
        video_id: str,
        stats: VideoStatistics,
        *,
        collected_at: datetime,
        provider_id: int,
        run_id: uuid.UUID | None,
        expires_at: datetime,
    ) -> bool:
        stmt = (
            pg_insert(VideoStatisticsRow)
            .values(
                video_id=video_id,
                collected_at=collected_at,
                view_count=stats.view_count,
                like_count=stats.like_count,
                comment_count=stats.comment_count,
                source_provider_id=provider_id,
                ingestion_run_id=run_id,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_video_statistics_snapshot")
            .returning(VideoStatisticsRow.id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def insert_search_result(
        self,
        result: VideoSearchResult,
        *,
        query_id: int,
        provider_id: int,
        run_id: uuid.UUID | None,
        region: str | None,
        language: str | None,
        order: str,
        published_after: datetime | None,
        published_before: datetime | None,
        collected_at: datetime,
        expires_at: datetime,
    ) -> bool:
        stmt = (
            pg_insert(VideoSearchResultRow)
            .values(
                query_id=query_id,
                provider_id=provider_id,
                video_id=result.video_id,
                ingestion_run_id=run_id,
                region=region,
                language=language,
                search_order=order,
                published_after=published_after,
                published_before=published_before,
                rank=result.rank,
                page=result.page,
                collected_at=collected_at,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_video_search_results_snapshot")
            .returning(VideoSearchResultRow.id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def fresh_channel_ids(self, channel_ids: Iterable[str], now: datetime) -> set[str]:
        """Channels whose stored metadata is still within its retention window."""
        ids = list(set(channel_ids))
        if not ids:
            return set()
        rows = self._s.execute(
            sa.select(ChannelRow.channel_id).where(
                ChannelRow.channel_id.in_(ids), ChannelRow.metadata_expires_at > now
            )
        ).scalars()
        return set(rows)

    def videos_due_for_refresh(
        self, now: datetime, horizon: timedelta, limit: int
    ) -> Sequence[str]:
        return (
            self._s.execute(
                sa.select(VideoRow.video_id)
                .where(VideoRow.metadata_expires_at <= now + horizon)
                .order_by(VideoRow.metadata_expires_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )

    def delete_videos(self, video_ids: Sequence[str]) -> int:
        if not video_ids:
            return 0
        return _rowcount(
            self._s.execute(sa.delete(VideoRow).where(VideoRow.video_id.in_(video_ids)))
        )

    def count_expired(self, now: datetime) -> dict[str, int]:
        def count(model: type, column: InstrumentedAttribute[Any]) -> int:
            return int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(column <= now)
                ).scalar_one()
            )

        return {
            "video_search_results": count(VideoSearchResultRow, VideoSearchResultRow.expires_at),
            "video_statistics": count(VideoStatisticsRow, VideoStatisticsRow.expires_at),
            "videos": count(VideoRow, VideoRow.metadata_expires_at),
            "channels": count(ChannelRow, ChannelRow.metadata_expires_at),
            "rejected_records": count(RejectedRecordRow, RejectedRecordRow.expires_at),
        }

    def delete_expired(self, now: datetime) -> dict[str, int]:
        """Delete expired third-party data. Children first, then parents."""
        return {
            "video_search_results": _rowcount(
                self._s.execute(
                    sa.delete(VideoSearchResultRow).where(VideoSearchResultRow.expires_at <= now)
                )
            ),
            "video_statistics": _rowcount(
                self._s.execute(
                    sa.delete(VideoStatisticsRow).where(VideoStatisticsRow.expires_at <= now)
                )
            ),
            "videos": _rowcount(
                self._s.execute(sa.delete(VideoRow).where(VideoRow.metadata_expires_at <= now))
            ),
            "channels": _rowcount(
                self._s.execute(sa.delete(ChannelRow).where(ChannelRow.metadata_expires_at <= now))
            ),
            "rejected_records": _rowcount(
                self._s.execute(
                    sa.delete(RejectedRecordRow).where(RejectedRecordRow.expires_at <= now)
                )
            ),
        }
