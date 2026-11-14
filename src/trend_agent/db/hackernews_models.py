"""Hacker News tables: tracked stories and their repeated activity observations.

Relationships::

    hn_stories 1--* hn_story_observations
    hn_stories *--0..1 discovery_items       (the story's discovery evidence item)
    hn_stories *--0..1 discovered_entities   (the entity the story is about, when known)

Invariants: the key is the Hacker News item ID (stable); one observation per
(story, collection time), so re-ingestion is idempotent; points and comment counts are
non-negative. Stored text is the sanitised title only; story bodies stay in the
discovery item (bounded excerpt) and expire with it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, utc_now_sql

KINDS = ("story", "show_hn", "ask_hn", "launch_hn", "tell_hn")
SUBJECTS = ("product_launch", "release", "announcement", "question", "discussion")
CATEGORIES = ("ai", "technology", "gaming", "other")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class HNStoryRow(Base):
    __tablename__ = "hn_stories"
    __table_args__ = (
        sa.CheckConstraint(_in("kind", KINDS), name="known_kind"),
        sa.CheckConstraint(_in("subject", SUBJECTS), name="known_subject"),
        sa.CheckConstraint(_in("primary_category", CATEGORIES), name="known_primary_category"),
        sa.CheckConstraint(
            f"secondary_category IS NULL OR {_in('secondary_category', CATEGORIES)}",
            name="known_secondary_category",
        ),
        sa.CheckConstraint(
            "monitoring_state IN ('active','inactive')", name="known_monitoring_state"
        ),
        sa.CheckConstraint(
            "momentum_state IN ('rising','steady','unknown')", name="known_momentum_state"
        ),
        sa.CheckConstraint("refresh_tier IN ('high','normal','low')", name="known_refresh_tier"),
        sa.CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
        sa.CheckConstraint("priority_score >= 0 AND priority_score <= 100", name="priority_range"),
        sa.CheckConstraint(
            "latest_score >= 0 AND latest_comments >= 0", name="non_negative_activity"
        ),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
        sa.Index("ix_hn_stories_category_published", "primary_category", "published_at"),
        sa.Index("ix_hn_stories_refresh", "monitoring_state", "next_refresh_at"),
        sa.Index("ix_hn_stories_expires_at", "expires_at"),
        sa.Index("ix_hn_stories_canonical_link", "canonical_link"),
    )

    story_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(sa.Text)  # sanitised
    linked_url: Mapped[str | None] = mapped_column(sa.Text)
    # canonical form of linked_url: stories sharing it are reposts of one announcement
    canonical_link: Mapped[str | None] = mapped_column(sa.Text)
    discussion_url: Mapped[str] = mapped_column(sa.Text)
    domain: Mapped[str | None] = mapped_column(sa.String(255))
    author: Mapped[str | None] = mapped_column(sa.String(100))
    kind: Mapped[str] = mapped_column(sa.String(16))
    subject: Mapped[str] = mapped_column(sa.String(20))
    primary_category: Mapped[str] = mapped_column(sa.String(16))
    secondary_category: Mapped[str | None] = mapped_column(sa.String(16))
    ambiguous: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    relevance: Mapped[float] = mapped_column(sa.Double)
    classification: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    lists: Mapped[list[str]] = mapped_column(ARRAY(sa.String(16)), server_default="{}")
    published_at: Mapped[datetime]
    latest_score: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    latest_comments: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    priority_score: Mapped[float] = mapped_column(sa.Double, server_default="0")
    priority_components: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    momentum_state: Mapped[str] = mapped_column(sa.String(10), server_default="unknown")
    momentum: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    monitoring_state: Mapped[str] = mapped_column(sa.String(10), server_default="active")
    refresh_tier: Mapped[str] = mapped_column(sa.String(10), server_default="normal")
    next_refresh_at: Mapped[datetime | None]
    last_observed_at: Mapped[datetime | None]
    entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovery_items.id", ondelete="SET NULL")
    )
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    first_source_key: Mapped[str] = mapped_column(sa.String(200))
    first_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=utc_now_sql(), onupdate=sa.func.now()
    )


class HNObservationRow(Base):
    __tablename__ = "hn_story_observations"
    __table_args__ = (
        sa.UniqueConstraint("story_id", "observed_at", name="hn_observation_once"),
        sa.CheckConstraint("score IS NULL OR score >= 0", name="non_negative_score"),
        sa.CheckConstraint("comments IS NULL OR comments >= 0", name="non_negative_comments"),
        sa.CheckConstraint("origin IN ('list','refresh')", name="known_origin"),
        sa.Index("ix_hn_story_observations_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    story_id: Mapped[int] = mapped_column(sa.ForeignKey("hn_stories.story_id", ondelete="CASCADE"))
    observed_at: Mapped[datetime]
    score: Mapped[int | None] = mapped_column(sa.Integer)
    comments: Mapped[int | None] = mapped_column(sa.Integer)
    list_name: Mapped[str | None] = mapped_column(sa.String(16))
    rank: Mapped[int | None] = mapped_column(sa.Integer)
    origin: Mapped[str] = mapped_column(sa.String(10))
    source_key: Mapped[str | None] = mapped_column(sa.String(200))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovery_items.id", ondelete="SET NULL")
    )
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql())
