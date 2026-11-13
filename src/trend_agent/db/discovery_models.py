"""Phase 2 schema: keyword discovery.

Relationships::

    discovery_sources 1--* discovery_items *--* discovered_entities   (via entity_mentions)
    discovered_entities 1--* entity_aliases ; discovered_entities 0..1--* children (versions)
    search_queries 1--1 discovered_keywords *--1 topic_clusters 1--* intent_clusters
    discovered_keywords *--* discovery_items   (via discovery_evidence, with relation type)
    discovered_keywords 1--* keyword_status_history
    discovery_runs 1--* discovery_run_sources

Invariants enforced in the database: one keyword row per canonical query, one evidence
row per (keyword, item, relation), one item per (provider, source reference), synthetic
labelling consistency, scores within 0-100.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql
from trend_agent.discovery.models import (
    CandidateStatus,
    Category,
    DiscoveryMethod,
    EntityOrigin,
    EntityStatus,
    EntityType,
    EvidenceRelation,
    EvidenceType,
    KeywordOrigin,
    NoveltyKind,
    SearchIntent,
    SourceHealth,
    SourcePool,
    SourceType,
)
from trend_agent.schemas.enums import IngestionStatus


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), onupdate=sa.func.now(), nullable=False)


class DiscoverySourceRow(Base):
    __tablename__ = "discovery_sources"
    __table_args__ = (
        sa.CheckConstraint("reliability >= 0 AND reliability <= 1", name="reliability_range"),
        sa.CheckConstraint("consecutive_failures >= 0", name="non_negative_failures"),
        sa.CheckConstraint("min_poll_interval_minutes >= 1", name="positive_interval"),
        sa.Index("ix_discovery_sources_category", "category", "enabled"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(200), unique=True)
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(sa.Text)
    source_type: Mapped[SourceType] = mapped_column(str_enum(SourceType, "discovery_source_type"))
    source_reference: Mapped[str] = mapped_column(sa.Text)
    category: Mapped[Category] = mapped_column(str_enum(Category, "discovery_source_category"))
    pool: Mapped[SourcePool] = mapped_column(str_enum(SourcePool, "discovery_source_pool"))
    enabled: Mapped[bool] = mapped_column(sa.Boolean)
    reliability: Mapped[float] = mapped_column(sa.Double)
    min_poll_interval_minutes: Mapped[int] = mapped_column(sa.Integer)
    last_checked_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_new_items_at: Mapped[datetime | None]
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    health_status: Mapped[SourceHealth] = mapped_column(
        str_enum(SourceHealth, "discovery_source_health")
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    etag: Mapped[str | None] = mapped_column(sa.Text)
    last_modified: Mapped[str | None] = mapped_column(sa.Text)
    expected_yield: Mapped[float] = mapped_column(sa.Double, server_default="0")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class DiscoveryRunRow(Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (
        sa.CheckConstraint(
            "total_candidates >= 0 AND new_candidates >= 0 AND merged_candidates >= 0 "
            "AND rejected_candidates >= 0 AND quota_estimate >= 0",
            name="non_negative_counts",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completed_after_start"
        ),
        sa.Index("ix_discovery_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    category: Mapped[Category | None] = mapped_column(str_enum(Category, "discovery_run_category"))
    status: Mapped[IngestionStatus] = mapped_column(
        str_enum(IngestionStatus, "discovery_run_status")
    )
    total_candidates: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    new_candidates: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    merged_candidates: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    rejected_candidates: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    items_received: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    items_new: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    entities_new: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    sources_checked: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    sources_failed: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    quota_estimate: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    quota_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    errors: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    scoring_version: Mapped[str] = mapped_column(sa.String(32))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())


class DiscoveryRunSourceRow(Base):
    __tablename__ = "discovery_run_sources"
    __table_args__ = (sa.Index("ix_discovery_run_sources_run", "run_id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("discovery_runs.id", ondelete="CASCADE")
    )
    source_id: Mapped[int] = mapped_column(
        sa.ForeignKey("discovery_sources.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(sa.String(32))
    items_received: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    items_new: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(sa.String(64))
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    quota_used: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime]


class DiscoveryItemRow(Base):
    __tablename__ = "discovery_items"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider_name", "source_reference", name="uq_discovery_items_reference"
        ),
        sa.CheckConstraint(
            "is_synthetic = (evidence_type = 'synthetic_test_data')",
            name="synthetic_label_consistent",
        ),
        sa.CheckConstraint(
            "simulated_evidence_type IS NULL OR is_synthetic", name="simulated_only_for_synthetic"
        ),
        sa.CheckConstraint("quality >= 0 AND quality <= 100", name="quality_range"),
        sa.Index("ix_discovery_items_published", "published_at"),
        sa.Index("ix_discovery_items_fingerprint", "content_fingerprint"),
        sa.Index(
            "ix_discovery_items_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(
        sa.ForeignKey("discovery_sources.id", ondelete="RESTRICT")
    )
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("discovery_runs.id", ondelete="SET NULL")
    )
    source_reference: Mapped[str] = mapped_column(sa.Text)
    evidence_type: Mapped[EvidenceType] = mapped_column(
        str_enum(EvidenceType, "discovery_item_evidence_type")
    )
    simulated_evidence_type: Mapped[EvidenceType | None] = mapped_column(
        str_enum(EvidenceType, "discovery_item_simulated_type")
    )
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    title: Mapped[str] = mapped_column(sa.Text)
    excerpt: Mapped[str | None] = mapped_column(sa.Text)
    url: Mapped[str | None] = mapped_column(sa.Text)
    canonical_url: Mapped[str | None] = mapped_column(sa.Text)
    publisher: Mapped[str | None] = mapped_column(sa.Text)
    author: Mapped[str | None] = mapped_column(sa.Text)
    published_at: Mapped[datetime | None]
    collected_at: Mapped[datetime]
    category_hint: Mapped[Category | None] = mapped_column(
        str_enum(Category, "discovery_item_category")
    )
    content_fingerprint: Mapped[str] = mapped_column(sa.String(64))
    quality: Mapped[float] = mapped_column(sa.Double)
    quality_flags: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    observed_queries: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class DiscoveredEntityRow(Base):
    __tablename__ = "discovered_entities"
    __table_args__ = (
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
        sa.Index("ix_discovered_entities_category_type", "category", "entity_type"),
        sa.Index("ix_discovered_entities_first_seen", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    entity_key: Mapped[str] = mapped_column(sa.String(300), unique=True)
    canonical_name: Mapped[str] = mapped_column(sa.Text)
    entity_type: Mapped[EntityType] = mapped_column(str_enum(EntityType, "discovered_entity_type"))
    category: Mapped[Category] = mapped_column(str_enum(Category, "discovered_entity_category"))
    parent_entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    version: Mapped[str | None] = mapped_column(sa.Text)
    release_level: Mapped[str | None] = mapped_column(sa.String(16))
    status: Mapped[EntityStatus] = mapped_column(str_enum(EntityStatus, "discovered_entity_status"))
    origin: Mapped[EntityOrigin] = mapped_column(str_enum(EntityOrigin, "discovered_entity_origin"))
    is_family: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    evergreen: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    confidence: Mapped[float] = mapped_column(sa.Double)
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    first_published_at: Mapped[datetime | None]
    release_date: Mapped[datetime | None]
    attributes: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class EntityAliasRow(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (
        sa.UniqueConstraint("entity_id", "alias_normalized", name="uq_entity_aliases_alias"),
        sa.Index("ix_entity_aliases_normalized", "alias_normalized"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="CASCADE")
    )
    alias: Mapped[str] = mapped_column(sa.Text)
    alias_normalized: Mapped[str] = mapped_column(sa.Text)
    language: Mapped[str | None] = mapped_column(sa.String(16))
    source: Mapped[str] = mapped_column(sa.String(32))
    created_at: Mapped[datetime] = _created()


class EntityMentionRow(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (
        sa.UniqueConstraint("entity_id", "item_id", name="uq_entity_mentions_pair"),
        sa.Index("ix_entity_mentions_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="CASCADE")
    )
    item_id: Mapped[int] = mapped_column(sa.ForeignKey("discovery_items.id", ondelete="CASCADE"))
    matched_text: Mapped[str] = mapped_column(sa.Text)
    confidence: Mapped[float] = mapped_column(sa.Double)
    extractor: Mapped[str] = mapped_column(sa.String(40))
    status: Mapped[EntityStatus] = mapped_column(str_enum(EntityStatus, "entity_mention_status"))
    attributes: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    related: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    created_at: Mapped[datetime] = _created()


class TopicClusterRow(Base):
    __tablename__ = "topic_clusters"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    topic_key: Mapped[str] = mapped_column(sa.String(300), unique=True)
    canonical_topic: Mapped[str] = mapped_column(sa.Text)
    category: Mapped[Category] = mapped_column(str_enum(Category, "topic_cluster_category"))
    representative_entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class IntentClusterRow(Base):
    __tablename__ = "intent_clusters"
    __table_args__ = (
        sa.UniqueConstraint("topic_cluster_id", "cluster_key", name="uq_intent_clusters_key"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    topic_cluster_id: Mapped[int] = mapped_column(
        sa.ForeignKey("topic_clusters.id", ondelete="CASCADE")
    )
    cluster_key: Mapped[str] = mapped_column(sa.String(300))
    canonical_intent: Mapped[SearchIntent] = mapped_column(
        str_enum(SearchIntent, "intent_cluster_intent")
    )
    representative_query: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class DiscoveredKeywordRow(Base):
    __tablename__ = "discovered_keywords"
    __table_args__ = (
        sa.CheckConstraint(
            "novelty_score >= 0 AND novelty_score <= 100 AND priority_score >= 0 "
            "AND priority_score <= 100",
            name="score_range",
        ),
        sa.CheckConstraint("last_discovered_at >= first_discovered_at", name="discovered_order"),
        sa.Index(
            "ix_discovered_keywords_rank", "primary_category", "candidate_status", "priority_score"
        ),
        sa.Index("ix_discovered_keywords_entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    query_id: Mapped[int] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="RESTRICT"), unique=True
    )
    entity_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_entities.id", ondelete="SET NULL")
    )
    topic_cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("topic_clusters.id", ondelete="SET NULL")
    )
    intent_cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("intent_clusters.id", ondelete="SET NULL")
    )
    origin: Mapped[KeywordOrigin] = mapped_column(str_enum(KeywordOrigin, "keyword_origin"))
    discovery_type: Mapped[DiscoveryMethod] = mapped_column(
        str_enum(DiscoveryMethod, "keyword_discovery_type")
    )
    template_id: Mapped[str | None] = mapped_column(sa.String(60))
    primary_category: Mapped[Category] = mapped_column(str_enum(Category, "keyword_category"))
    category_scores: Mapped[dict[str, Any]] = mapped_column(JSONB)
    primary_intent: Mapped[SearchIntent] = mapped_column(str_enum(SearchIntent, "keyword_intent"))
    intent_scores: Mapped[dict[str, Any]] = mapped_column(JSONB)
    candidate_status: Mapped[CandidateStatus] = mapped_column(
        str_enum(CandidateStatus, "keyword_status")
    )
    status_reason: Mapped[str] = mapped_column(sa.Text)
    first_discovered_at: Mapped[datetime]
    last_discovered_at: Mapped[datetime]
    novelty_score: Mapped[float] = mapped_column(sa.Double)
    novelty_kind: Mapped[NoveltyKind] = mapped_column(str_enum(NoveltyKind, "keyword_novelty_kind"))
    novelty_components: Mapped[dict[str, Any]] = mapped_column(JSONB)
    priority_score: Mapped[float] = mapped_column(sa.Double)
    scoring_version: Mapped[str] = mapped_column(sa.String(32))
    score_components: Mapped[dict[str, Any]] = mapped_column(JSONB)
    topic_assignment: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    intent_assignment: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    grounding: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class DiscoveryEvidenceRow(Base):
    __tablename__ = "discovery_evidence"
    __table_args__ = (
        sa.UniqueConstraint("keyword_id", "item_id", "relation", name="uq_discovery_evidence_link"),
        sa.CheckConstraint(
            "evidence_quality >= 0 AND evidence_quality <= 100", name="quality_range"
        ),
        sa.Index("ix_discovery_evidence_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    keyword_id: Mapped[int] = mapped_column(
        sa.ForeignKey("discovered_keywords.id", ondelete="CASCADE")
    )
    item_id: Mapped[int] = mapped_column(sa.ForeignKey("discovery_items.id", ondelete="CASCADE"))
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    source_reference: Mapped[str] = mapped_column(sa.Text)
    evidence_type: Mapped[EvidenceType] = mapped_column(
        str_enum(EvidenceType, "discovery_evidence_type")
    )
    relation: Mapped[EvidenceRelation] = mapped_column(
        str_enum(EvidenceRelation, "discovery_evidence_relation")
    )
    observed_at: Mapped[datetime]
    collected_at: Mapped[datetime]
    source_published_at: Mapped[datetime | None]
    evidence_quality: Mapped[float] = mapped_column(sa.Double)
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    created_at: Mapped[datetime] = _created()


class KeywordStatusHistoryRow(Base):
    __tablename__ = "keyword_status_history"
    __table_args__ = (sa.Index("ix_keyword_status_history_keyword", "keyword_id", "changed_at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    keyword_id: Mapped[int] = mapped_column(
        sa.ForeignKey("discovered_keywords.id", ondelete="CASCADE")
    )
    from_status: Mapped[CandidateStatus | None] = mapped_column(
        str_enum(CandidateStatus, "status_history_from")
    )
    to_status: Mapped[CandidateStatus] = mapped_column(
        str_enum(CandidateStatus, "status_history_to")
    )
    reason: Mapped[str] = mapped_column(sa.Text)
    actor: Mapped[str] = mapped_column(sa.String(32))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("discovery_runs.id", ondelete="SET NULL")
    )
    changed_at: Mapped[datetime]
