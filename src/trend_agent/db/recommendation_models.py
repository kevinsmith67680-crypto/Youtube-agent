"""Phase 7 recommendation tables (migration 0008).

* ``creator_profiles``                 creator preferences and production capacity (only what
                                       the creator entered; nothing is inferred).
* ``recommendation_generation_runs``   one generation invocation: counts, skips, LLM usage and
                                       estimated cost, generator, versions.
* ``video_recommendations``            one concept per row; the ID is deterministic (UUIDv5 of
                                       opportunity, format, profile and the input fingerprint),
                                       so regenerating unchanged evidence is a no-op. The full
                                       ``recommendation/1.0`` document is stored in ``concept``;
                                       the columns are query/filter projections of it.
* ``recommendation_hooks``, ``recommendation_outlines``, ``recommendation_scores`` - children
                                       (normalised for filtering and audits; cascade).
* ``saved_recommendations``            the creator's saved / dismissed decisions.
* ``recommendation_feedback``          explicit feedback events (append-only).
* ``production_tracking``              the creator's production status with history.
* ``notifications`` / ``notification_deliveries``  alerts (deduplicated) and channel delivery
                                       attempts.

Retention: a recommendation expires with the opportunity report it was derived from. Expired
recommendations are deleted unless the creator saved or is producing them (their own data);
those are kept with ``is_current = false``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, utc_now_sql

_STATUSES = "'APPROVED','APPROVED_WITH_UNCERTAINTY','RESEARCH_REQUIRED','REJECTED'"
_PRODUCTION = (
    "'IDEA','RESEARCHING','SCRIPTING','RECORDING','EDITING','READY_TO_PUBLISH',"
    "'PUBLISHED','ABANDONED'"
)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


class CreatorProfileRow(Base):
    __tablename__ = "creator_profiles"
    __table_args__ = (
        sa.Index(
            "uq_creator_profiles_default",
            "is_default",
            unique=True,
            postgresql_where=sa.text("is_default"),
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_default: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class RecommendationRunRow(Base):
    __tablename__ = "recommendation_generation_runs"
    __table_args__ = (
        sa.Index("ix_recommendation_generation_runs_started", "started_at"),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','dry_run')", name="known_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(sa.String(16))
    trigger: Mapped[str] = mapped_column(sa.String(16))  # cli | api | scheduled
    category: Mapped[str | None] = mapped_column(sa.String(16))
    profile_id: Mapped[str] = mapped_column(sa.String(64))
    generator: Mapped[str] = mapped_column(sa.String(32))
    recommendation_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    considered: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    generated: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    reused: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    approved: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    research: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    suppressed: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    skipped: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    decisions: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    llm_calls: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    input_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    estimated_cost_usd: Mapped[float | None] = mapped_column(sa.Double)
    seconds: Mapped[float | None] = mapped_column(sa.Double)
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created()


class VideoRecommendationRow(Base):
    __tablename__ = "video_recommendations"
    __table_args__ = (
        sa.Index("ix_video_recommendations_opportunity", "opportunity_key", "is_current"),
        sa.Index(
            "ix_video_recommendations_current_score",
            "composite_score",
            postgresql_where=sa.text("is_current"),
        ),
        sa.Index("ix_video_recommendations_status", "status"),
        sa.Index("ix_video_recommendations_fingerprint", "fingerprint"),
        sa.Index(
            "ix_video_recommendations_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="known_status"),
        sa.CheckConstraint(
            "composite_score IS NULL OR (composite_score >= 0 AND composite_score <= 100)",
            name="score_range",
        ),
        sa.CheckConstraint("hours_low >= 0 AND hours_high >= hours_low", name="hours_order"),
        sa.CheckConstraint(
            "NOT eligible OR status IN ('APPROVED','APPROVED_WITH_UNCERTAINTY')",
            name="eligible_is_approved",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("recommendation_generation_runs.id", ondelete="SET NULL")
    )
    opportunity_key: Mapped[str] = mapped_column(sa.String(64))
    opportunity_row_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("content_opportunities.id", ondelete="SET NULL")
    )
    query: Mapped[str] = mapped_column(sa.Text)
    category: Mapped[str | None] = mapped_column(sa.String(16))
    region: Mapped[str | None] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    video_format: Mapped[str] = mapped_column(sa.String(48))
    intent: Mapped[str] = mapped_column(sa.String(48))
    sub_intent: Mapped[str | None] = mapped_column(sa.String(48))
    title: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(32))
    research: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    eligible: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    composite_score: Mapped[float | None] = mapped_column(sa.Double)
    rank: Mapped[int | None] = mapped_column(sa.Integer)
    ranking_version: Mapped[str] = mapped_column(sa.String(40))
    urgency: Mapped[str] = mapped_column(sa.String(24))
    production_profile: Mapped[str] = mapped_column(sa.String(32))
    hours_low: Mapped[float] = mapped_column(sa.Double)
    hours_high: Mapped[float] = mapped_column(sa.Double)
    demand_basis: Mapped[str] = mapped_column(sa.String(48))
    # the Phase 5 opportunity score when generated (used to detect score upgrades)
    opportunity_score: Mapped[float | None] = mapped_column(sa.Double)
    forecast_available: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    suppressed: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    suppression_reason: Mapped[str | None] = mapped_column(sa.Text)
    duplicate_of: Mapped[str | None] = mapped_column(sa.String(64))
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    generator: Mapped[str] = mapped_column(sa.String(32))
    profile_id: Mapped[str] = mapped_column(sa.String(64))
    recommendation_version: Mapped[str] = mapped_column(sa.String(40))
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.String(80)), server_default="{}")
    concept: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    generated_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None]


class RecommendationHookRow(Base):
    __tablename__ = "recommendation_hooks"
    __table_args__ = (
        sa.UniqueConstraint("recommendation_id", "position", name="uq_recommendation_hooks_pos"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(sa.SmallInteger)
    hook_type: Mapped[str] = mapped_column(sa.String(32))
    text: Mapped[str] = mapped_column(sa.Text)
    estimated_seconds: Mapped[float | None] = mapped_column(sa.Double)
    labels: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")


class RecommendationOutlineRow(Base):
    __tablename__ = "recommendation_outlines"

    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE"), primary_key=True
    )
    outline: Mapped[dict[str, Any]] = mapped_column(JSONB)
    section_count: Mapped[int] = mapped_column(sa.SmallInteger)
    label_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class RecommendationScoreRow(Base):
    __tablename__ = "recommendation_scores"

    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE"), primary_key=True
    )
    component: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    value: Mapped[float | None] = mapped_column(sa.Double)
    weight: Mapped[float] = mapped_column(sa.Double)
    contribution: Mapped[float | None] = mapped_column(sa.Double)
    available: Mapped[bool] = mapped_column(sa.Boolean)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    note: Mapped[str] = mapped_column(sa.Text)
    ranking_version: Mapped[str] = mapped_column(sa.String(40))


class SavedRecommendationRow(Base):
    __tablename__ = "saved_recommendations"
    __table_args__ = (
        sa.UniqueConstraint("user_key", "recommendation_id", name="uq_saved_recommendations_user"),
        sa.CheckConstraint("state IN ('SAVED','DISMISSED')", name="known_state"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    user_key: Mapped[str] = mapped_column(sa.String(64))
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(sa.String(16))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    saved_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = _created()


class RecommendationFeedbackRow(Base):
    __tablename__ = "recommendation_feedback"
    __table_args__ = (
        sa.Index("ix_recommendation_feedback_rec", "recommendation_id", "created_at"),
        sa.Index("ix_recommendation_feedback_type", "feedback_type"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    user_key: Mapped[str] = mapped_column(sa.String(64))
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE")
    )
    feedback_type: Mapped[str] = mapped_column(sa.String(24))
    value: Mapped[str | None] = mapped_column(sa.String(200))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    # copied at feedback time so evaluation can group feedback by the score version shown
    ranking_version: Mapped[str | None] = mapped_column(sa.String(40))
    composite_score: Mapped[float | None] = mapped_column(sa.Double)
    created_at: Mapped[datetime] = _created()


class ProductionTrackingRow(Base):
    __tablename__ = "production_tracking"
    __table_args__ = (
        sa.UniqueConstraint("user_key", "recommendation_id", name="uq_production_tracking_user"),
        sa.CheckConstraint(f"status IN ({_PRODUCTION})", name="known_status"),
        sa.CheckConstraint(
            "status <> 'PUBLISHED' OR published_video_id IS NOT NULL", name="published_has_video"
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    user_key: Mapped[str] = mapped_column(sa.String(64))
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(sa.String(24))
    planned_publication_time: Mapped[datetime | None]
    actual_publication_time: Mapped[datetime | None]
    published_video_id: Mapped[str | None] = mapped_column(sa.String(16))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    history: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class NotificationRow(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        sa.Index("ix_notifications_subject", "subject_key", "created_at"),
        sa.Index("ix_notifications_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dedup_key: Mapped[str] = mapped_column(sa.String(200), unique=True)
    subject_key: Mapped[str] = mapped_column(sa.String(200))
    event_type: Mapped[str] = mapped_column(sa.String(32))
    severity: Mapped[str] = mapped_column(sa.String(16))
    title: Mapped[str] = mapped_column(sa.Text)
    body: Mapped[str] = mapped_column(sa.Text)
    recommendation_id: Mapped[str | None] = mapped_column(sa.String(64))
    opportunity_key: Mapped[str | None] = mapped_column(sa.String(64))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    read: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime]


class NotificationDeliveryRow(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        sa.UniqueConstraint("notification_id", "channel", name="uq_notification_deliveries_ch"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("notifications.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(sa.String(24))
    status: Mapped[str] = mapped_column(sa.String(24))
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    detail: Mapped[str | None] = mapped_column(sa.Text)
    last_attempt_at: Mapped[datetime | None]
