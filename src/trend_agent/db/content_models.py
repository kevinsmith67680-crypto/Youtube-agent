"""Phase 5 tables: analysis runs, intents, requirements, video analyses, coverage, gaps,
opportunities.

No third-party text is copied here: videos are referenced by ID and evidence ID (the
evidence registry holds the bounded, expiring excerpt). Rows derived from YouTube
metadata carry ``expires_at`` = the earliest expiry of the videos they were derived from
and are purged with them; the run row itself (counts, fingerprints, versions) remains as
an audit record.

Idempotency: a completed run is unique per ``fingerprint`` (inputs + retrieved results +
versions); child rows are unique per run; an opportunity is upserted per
``opportunity_key`` (query, region, language).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql


class AnalysisRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


class ContentAnalysisRunRow(Base):
    __tablename__ = "content_analysis_runs"
    __table_args__ = (
        sa.Index(
            "uq_content_analysis_runs_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=sa.text("status = 'completed' AND fingerprint IS NOT NULL"),
        ),
        sa.Index("ix_content_analysis_runs_query", "query_id", "created_at"),
        sa.Index("ix_content_analysis_runs_status", "status", "created_at"),
        sa.Index("ix_content_analysis_runs_inputs", "inputs_fingerprint"),
        sa.CheckConstraint("results_inspected >= 0", name="non_negative_results"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    surge_event_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("surge_events.id", ondelete="SET NULL")
    )
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="SET NULL")
    )
    trigger: Mapped[str] = mapped_column(sa.String(16))
    canonical_query: Mapped[str] = mapped_column(sa.Text)
    category: Mapped[str | None] = mapped_column(sa.String(16))
    region: Mapped[str | None] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    status: Mapped[AnalysisRunStatus] = mapped_column(
        str_enum(AnalysisRunStatus, "content_analysis_run_status")
    )
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    model_version: Mapped[str] = mapped_column(sa.String(80))
    analysis_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    results_inspected: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    inputs_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    fingerprint: Mapped[str | None] = mapped_column(sa.String(64))
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("evidence_bundles.id", ondelete="SET NULL")
    )
    demand_basis: Mapped[str] = mapped_column(sa.String(48))
    opportunity_status: Mapped[str | None] = mapped_column(sa.String(24))
    verification_state: Mapped[str | None] = mapped_column(sa.String(32))
    priority: Mapped[float] = mapped_column(sa.Double, server_default="0")
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    cost: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error_details: Mapped[str | None] = mapped_column(sa.Text)
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class SearchIntentAnalysisRow(Base):
    __tablename__ = "search_intent_analyses"
    __table_args__ = (sa.Index("ix_search_intent_analyses_query", "query_id", "created_at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("content_analysis_runs.id", ondelete="CASCADE"), unique=True
    )
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    primary_intent: Mapped[str] = mapped_column(sa.String(32))
    sub_intent: Mapped[str | None] = mapped_column(sa.String(40))
    secondary_intents: Mapped[list[str]] = mapped_column(ARRAY(sa.String(32)), server_default="{}")
    entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    entity_key: Mapped[str | None] = mapped_column(sa.String(300))
    confidence_status: Mapped[str] = mapped_column(sa.String(16))
    specificity: Mapped[str] = mapped_column(sa.String(16))
    analysis_method: Mapped[str] = mapped_column(sa.String(32))
    evidence_references: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(40)), server_default="{}"
    )
    ambiguity: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = _created()


class InformationRequirementRow(Base):
    __tablename__ = "information_requirements"
    __table_args__ = (
        sa.UniqueConstraint("analysis_run_id", "key", name="uq_information_requirements_key"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("content_analysis_runs.id", ondelete="CASCADE")
    )
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    intent_id: Mapped[int] = mapped_column(
        sa.ForeignKey("search_intent_analyses.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(sa.String(60))
    requirement: Mapped[str] = mapped_column(sa.Text)
    priority: Mapped[str] = mapped_column(sa.String(16))
    cues: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    source: Mapped[str] = mapped_column(sa.String(24))
    requires_verified_entity: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    blocking: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    evidence_reference: Mapped[str | None] = mapped_column(sa.String(40))


class VideoResultAnalysisRow(Base):
    __tablename__ = "video_result_analyses"
    __table_args__ = (
        sa.UniqueConstraint("analysis_run_id", "video_id", name="uq_video_result_analyses_video"),
        sa.CheckConstraint(
            "relevance_score >= 0 AND relevance_score <= 100", name="relevance_range"
        ),
        sa.Index(
            "ix_video_result_analyses_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("content_analysis_runs.id", ondelete="CASCADE")
    )
    video_id: Mapped[str] = mapped_column(sa.String(16))
    variant: Mapped[str] = mapped_column(sa.Text)
    api_position: Mapped[int] = mapped_column(sa.Integer)
    relevance_score: Mapped[float] = mapped_column(sa.Double)
    relevant: Mapped[bool] = mapped_column(sa.Boolean)
    intent_match: Mapped[bool] = mapped_column(sa.Boolean)
    entity_match: Mapped[str] = mapped_column(sa.String(24))
    video_format: Mapped[str] = mapped_column(sa.String(24))
    freshness_status: Mapped[str] = mapped_column(sa.String(32))
    evidence_level: Mapped[str] = mapped_column(sa.String(24))
    relevance_explanation: Mapped[str] = mapped_column(sa.Text)
    verification_status: Mapped[str] = mapped_column(sa.String(24))
    evidence_id: Mapped[str | None] = mapped_column(sa.String(40))
    expires_at: Mapped[datetime | None]


class RequirementCoverageRow(Base):
    __tablename__ = "requirement_coverage"
    __table_args__ = (
        sa.UniqueConstraint("requirement_id", "video_id", name="uq_requirement_coverage_cell"),
        sa.Index(
            "ix_requirement_coverage_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("content_analysis_runs.id", ondelete="CASCADE")
    )
    requirement_id: Mapped[int] = mapped_column(
        sa.ForeignKey("information_requirements.id", ondelete="CASCADE")
    )
    video_id: Mapped[str] = mapped_column(sa.String(16))
    coverage_status: Mapped[str] = mapped_column(sa.String(24))
    evidence_level: Mapped[str] = mapped_column(sa.String(24))
    supporting_evidence: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(40)), server_default="{}"
    )
    matched_cues: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    note: Mapped[str] = mapped_column(sa.Text)
    expires_at: Mapped[datetime | None]


class ContentGapRow(Base):
    __tablename__ = "content_gaps"
    __table_args__ = (
        sa.UniqueConstraint("analysis_run_id", "gap_type", name="uq_content_gaps_type"),
        sa.Index("ix_content_gaps_query", "query_id", "created_at"),
        sa.Index(
            "ix_content_gaps_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("content_analysis_runs.id", ondelete="CASCADE")
    )
    category: Mapped[str | None] = mapped_column(sa.String(16))
    gap_type: Mapped[str] = mapped_column(sa.String(40))
    evidence_strength: Mapped[str] = mapped_column(sa.String(16))
    explanation: Mapped[str] = mapped_column(sa.Text)
    verification_status: Mapped[str] = mapped_column(sa.String(16))
    requirement_keys: Mapped[list[str]] = mapped_column(ARRAY(sa.String(60)), server_default="{}")
    supporting_evidence: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(40)), server_default="{}"
    )
    limitations: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None]


class ContentOpportunityRow(Base):
    __tablename__ = "content_opportunities"
    __table_args__ = (
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="score_range"),
        sa.CheckConstraint(
            "status <> 'candidate' OR primary_gap_id IS NOT NULL OR analysis_run_id IS NULL",
            name="candidate_has_gap",
        ),
        sa.Index("ix_content_opportunities_rank", "status", "score"),
        sa.Index(
            "ix_content_opportunities_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    opportunity_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    surge_event_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("surge_events.id", ondelete="SET NULL")
    )
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("content_analysis_runs.id", ondelete="SET NULL")
    )
    primary_gap_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("content_gaps.id", ondelete="SET NULL")
    )
    canonical_query: Mapped[str] = mapped_column(sa.Text)
    category: Mapped[str | None] = mapped_column(sa.String(16))
    region: Mapped[str | None] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    score: Mapped[float | None] = mapped_column(sa.Double)
    score_version: Mapped[str] = mapped_column(sa.String(40))
    evidence_quality: Mapped[str | None] = mapped_column(sa.String(16))
    status: Mapped[str] = mapped_column(sa.String(24))
    verification_state: Mapped[str] = mapped_column(sa.String(32))
    demand_basis: Mapped[str] = mapped_column(sa.String(48))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=utc_now_sql(), nullable=False)
    expires_at: Mapped[datetime | None]
