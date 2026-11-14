"""Twitch tables: collection snapshots, game rankings, observed viewers, live streams.

Relationships::

    twitch_snapshots 1--* twitch_rank_observations      (top-games polls)
    twitch_snapshots 1--* twitch_viewer_observations    (per game, from stream polls)
    twitch_snapshots 1--* twitch_stream_observations    (individual live streams)
    twitch_games     (latest state + attention assessment, keyed by Twitch game ID)
    twitch_monitored_games (what to poll: operator, configuration or auto-selected)

Invariants: Twitch IDs are the keys; one rank / viewer row per (snapshot, game) and one
stream row per (snapshot, stream), so re-ingestion is idempotent; every collection records
its sample status (complete / partial / unknown). Viewer sums are never written for games
a snapshot did not cover: a missing game is unknown, not zero.

Retention: every row holding Twitch data carries ``expires_at`` (default 24 hours, the
Twitch Developer Services Agreement's cache limit) and is purged by the Twitch stage itself
as well as by the daily retention job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, utc_now_sql

SNAPSHOT_KINDS = ("top_games", "top_streams", "game_streams")
SAMPLE_STATUSES = ("complete", "partial", "unknown")
SAMPLE_BASES = ("game_streams", "top_streams")
CATEGORY_KINDS = ("game", "non_game", "unknown")
ATTENTION_STATES = (
    "rising",
    "sustained_rise",
    "steady",
    "falling",
    "insufficient_observations",
    "incomparable_samples",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class TwitchSnapshotRow(Base):
    __tablename__ = "twitch_snapshots"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", SNAPSHOT_KINDS), name="known_kind"),
        sa.CheckConstraint(_in("sample_status", SAMPLE_STATUSES), name="known_sample_status"),
        sa.CheckConstraint(
            "pages_fetched >= 0 AND pages_fetched <= pages_requested", name="page_counts"
        ),
        sa.CheckConstraint("items >= 0 AND duplicates >= 0", name="non_negative_counts"),
        sa.CheckConstraint("viewer_sum IS NULL OR viewer_sum >= 0", name="non_negative_viewer_sum"),
        sa.Index("ix_twitch_snapshots_kind_observed", "kind", "observed_at"),
        sa.Index("ix_twitch_snapshots_game_observed", "game_id", "observed_at"),
        sa.Index("ix_twitch_snapshots_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(sa.String(16))
    game_id: Mapped[str | None] = mapped_column(sa.String(32))
    languages: Mapped[list[str]] = mapped_column(ARRAY(sa.String(8)), server_default="{}")
    observed_at: Mapped[datetime]
    completed_at: Mapped[datetime]
    pages_requested: Mapped[int] = mapped_column(sa.Integer)
    pages_fetched: Mapped[int] = mapped_column(sa.Integer)
    page_size: Mapped[int] = mapped_column(sa.Integer)
    items: Mapped[int] = mapped_column(sa.Integer)
    duplicates: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    skipped: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    sample_status: Mapped[str] = mapped_column(sa.String(10))
    status_reason: Mapped[str] = mapped_column(sa.String(120))
    viewer_sum: Mapped[int | None] = mapped_column(sa.BigInteger)
    stream_count: Mapped[int | None] = mapped_column(sa.Integer)
    requests: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    rate_limit_remaining: Mapped[int | None] = mapped_column(sa.Integer)
    trigger: Mapped[str] = mapped_column(sa.String(16), server_default="scheduler")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())


class TwitchGameRow(Base):
    __tablename__ = "twitch_games"
    __table_args__ = (
        sa.CheckConstraint(_in("category_kind", CATEGORY_KINDS), name="known_category_kind"),
        sa.CheckConstraint(_in("attention_state", ATTENTION_STATES), name="known_attention_state"),
        sa.CheckConstraint(
            "attention_score >= 0 AND attention_score <= 100", name="attention_range"
        ),
        sa.CheckConstraint("latest_rank IS NULL OR latest_rank >= 1", name="rank_positive"),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
        sa.Index("ix_twitch_games_attention", "attention_state", "attention_score"),
        sa.Index("ix_twitch_games_expires_at", "expires_at"),
    )

    game_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text)
    igdb_id: Mapped[str | None] = mapped_column(sa.String(32))
    box_art_url: Mapped[str | None] = mapped_column(sa.Text)
    category_kind: Mapped[str] = mapped_column(sa.String(10))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    latest_rank: Mapped[int | None] = mapped_column(sa.Integer)
    latest_rank_at: Mapped[datetime | None]
    best_rank: Mapped[int | None] = mapped_column(sa.Integer)
    latest_viewers: Mapped[int | None] = mapped_column(sa.BigInteger)
    latest_streams: Mapped[int | None] = mapped_column(sa.Integer)
    latest_sample_status: Mapped[str | None] = mapped_column(sa.String(10))
    latest_viewers_at: Mapped[datetime | None]
    attention: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    attention_state: Mapped[str] = mapped_column(
        sa.String(32), server_default="insufficient_observations"
    )
    attention_score: Mapped[float] = mapped_column(sa.Double, server_default="0")
    attention_version: Mapped[str | None] = mapped_column(sa.String(32))
    entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovery_items.id", ondelete="SET NULL")
    )
    promoted_at: Mapped[datetime | None]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=utc_now_sql(), onupdate=sa.func.now()
    )


class TwitchMonitoredGameRow(Base):
    """What to poll. ``requested_name`` is the operator's own text, not Twitch data."""

    __tablename__ = "twitch_monitored_games"
    __table_args__ = (
        sa.CheckConstraint("tier IN ('high','low')", name="known_tier"),
        sa.CheckConstraint("origin IN ('manual','config','auto')", name="known_origin"),
    )

    game_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    requested_name: Mapped[str | None] = mapped_column(sa.Text)
    tier: Mapped[str] = mapped_column(sa.String(8))
    origin: Mapped[str] = mapped_column(sa.String(8))
    reason: Mapped[str | None] = mapped_column(sa.Text)
    next_poll_at: Mapped[datetime | None]
    last_polled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())
    expires_at: Mapped[datetime | None]  # auto-selected games stop being monitored


class TwitchRankObservationRow(Base):
    __tablename__ = "twitch_rank_observations"
    __table_args__ = (
        sa.UniqueConstraint("snapshot_id", "game_id", name="twitch_rank_once"),
        sa.CheckConstraint("rank >= 1", name="rank_positive"),
        sa.Index("ix_twitch_rank_observations_game", "game_id", "observed_at"),
        sa.Index("ix_twitch_rank_observations_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        sa.ForeignKey("twitch_snapshots.id", ondelete="CASCADE")
    )
    game_id: Mapped[str] = mapped_column(sa.String(32))
    rank: Mapped[int] = mapped_column(sa.Integer)
    observed_at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]


class TwitchViewerObservationRow(Base):
    __tablename__ = "twitch_viewer_observations"
    __table_args__ = (
        sa.UniqueConstraint("snapshot_id", "game_id", name="twitch_viewers_once"),
        sa.CheckConstraint(_in("sample_status", SAMPLE_STATUSES), name="known_sample_status"),
        sa.CheckConstraint(_in("sample_basis", SAMPLE_BASES), name="known_sample_basis"),
        sa.CheckConstraint(
            "streams_observed >= 0 AND viewers_observed >= 0", name="non_negative_counts"
        ),
        sa.Index("ix_twitch_viewer_observations_game", "game_id", "observed_at"),
        sa.Index("ix_twitch_viewer_observations_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        sa.ForeignKey("twitch_snapshots.id", ondelete="CASCADE")
    )
    game_id: Mapped[str] = mapped_column(sa.String(32))
    observed_at: Mapped[datetime]
    streams_observed: Mapped[int] = mapped_column(sa.Integer)
    viewers_observed: Mapped[int] = mapped_column(sa.BigInteger)
    top_stream_viewers: Mapped[int | None] = mapped_column(sa.Integer)
    sample_status: Mapped[str] = mapped_column(sa.String(10))
    sample_basis: Mapped[str] = mapped_column(sa.String(16))
    languages: Mapped[list[str]] = mapped_column(ARRAY(sa.String(8)), server_default="{}")
    sample_cap: Mapped[int] = mapped_column(sa.Integer)  # page_size x pages_requested
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]


class TwitchStreamObservationRow(Base):
    __tablename__ = "twitch_stream_observations"
    __table_args__ = (
        sa.UniqueConstraint("snapshot_id", "stream_id", name="twitch_stream_once"),
        sa.CheckConstraint("viewer_count >= 0", name="non_negative_viewers"),
        sa.Index("ix_twitch_stream_observations_game", "game_id", "observed_at"),
        sa.Index("ix_twitch_stream_observations_stream", "stream_id"),
        sa.Index("ix_twitch_stream_observations_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        sa.ForeignKey("twitch_snapshots.id", ondelete="CASCADE")
    )
    stream_id: Mapped[str] = mapped_column(sa.String(40))
    user_id: Mapped[str] = mapped_column(sa.String(40))
    user_login: Mapped[str] = mapped_column(sa.String(64))
    user_name: Mapped[str | None] = mapped_column(sa.Text)
    game_id: Mapped[str | None] = mapped_column(sa.String(32))
    game_name: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str | None] = mapped_column(sa.Text)  # sanitised, truncated
    language: Mapped[str | None] = mapped_column(sa.String(8))
    started_at: Mapped[datetime]
    viewer_count: Mapped[int] = mapped_column(sa.Integer)
    position: Mapped[int] = mapped_column(sa.Integer)
    observed_at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
