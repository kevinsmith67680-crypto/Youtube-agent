"""Keyword discovery (Phase 2): sources, items, entities, aliases, mentions, clusters,
keywords, evidence, lifecycle history and discovery runs.

Additive only: no Phase 1 table or column is altered or dropped. The only change to an
existing object widens the rejected_records.record_kind CHECK constraint to accept the new
'discovery_item' record kind (drop + re-create; existing rows remain valid).

Reviewed by hand after autogeneration.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13 14:18:21.090487+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovered_entities",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_key", sa.String(length=300), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum(
                "ai_model",
                "ai_product",
                "company",
                "software",
                "framework",
                "game",
                "game_event",
                "game_item",
                "game_update",
                "hardware",
                "technology",
                "release",
                "feature",
                name="discovered_entity_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "category",
            sa.Enum(
                "ai",
                "technology",
                "gaming",
                "other",
                name="discovered_entity_category",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("parent_entity_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("release_level", sa.String(length=16), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "verified",
                "needs_verification",
                "rejected",
                name="discovered_entity_status",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "origin",
            sa.Enum(
                "gazetteer",
                "extracted",
                "llm_proposed",
                name="discovered_entity_origin",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("is_family", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("evergreen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attributes",
            postgresql.ARRAY(sa.String(length=40)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_discovered_entities_confidence_range"),
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at", name=op.f("ck_discovered_entities_seen_order")
        ),
        sa.ForeignKeyConstraint(
            ["parent_entity_id"],
            ["discovered_entities.id"],
            name=op.f("fk_discovered_entities_parent_entity_id_discovered_entities"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovered_entities")),
        sa.UniqueConstraint("entity_key", name=op.f("uq_discovered_entities_entity_key")),
    )
    op.create_index(
        "ix_discovered_entities_category_type",
        "discovered_entities",
        ["category", "entity_type"],
        unique=False,
    )
    op.create_index(
        "ix_discovered_entities_first_seen", "discovered_entities", ["first_seen_at"], unique=False
    )
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column(
            "category",
            sa.Enum(
                "ai",
                "technology",
                "gaming",
                "other",
                name="discovery_run_category",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                name="discovery_run_status",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("total_candidates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_candidates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("merged_candidates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rejected_candidates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("entities_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sources_checked", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sources_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quota_estimate", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "quota_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "errors", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "plan", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "summary", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("scoring_version", sa.String(length=32), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_discovery_runs_completed_after_start"),
        ),
        sa.CheckConstraint(
            "total_candidates >= 0 AND new_candidates >= 0 AND merged_candidates >= 0 AND rejected_candidates >= 0 AND quota_estimate >= 0",
            name=op.f("ck_discovery_runs_non_negative_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_discovery_runs_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_runs")),
    )
    op.create_index("ix_discovery_runs_started_at", "discovery_runs", ["started_at"], unique=False)
    op.create_table(
        "discovery_sources",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum(
                "feed",
                "api_query",
                "chart",
                "channel",
                "repository",
                "community",
                "seed",
                "exploration_query",
                "synthetic",
                name="discovery_source_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "ai",
                "technology",
                "gaming",
                "other",
                name="discovery_source_category",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "pool",
            sa.Enum(
                "exploitation",
                "exploration",
                name="discovery_source_pool",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reliability", sa.Double(), nullable=False),
        sa.Column("min_poll_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_new_items_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "health_status",
            sa.Enum(
                "unknown",
                "healthy",
                "degraded",
                "failing",
                "not_configured",
                "unsupported",
                name="discovery_source_health",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("expected_yield", sa.Double(), server_default="0", nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0", name=op.f("ck_discovery_sources_non_negative_failures")
        ),
        sa.CheckConstraint(
            "min_poll_interval_minutes >= 1", name=op.f("ck_discovery_sources_positive_interval")
        ),
        sa.CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name=op.f("ck_discovery_sources_reliability_range"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_discovery_sources_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_sources")),
        sa.UniqueConstraint("key", name=op.f("uq_discovery_sources_key")),
    )
    op.create_index(
        "ix_discovery_sources_category", "discovery_sources", ["category", "enabled"], unique=False
    )
    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("alias_normalized", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["discovered_entities.id"],
            name=op.f("fk_entity_aliases_entity_id_discovered_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_aliases")),
        sa.UniqueConstraint("entity_id", "alias_normalized", name="uq_entity_aliases_alias"),
    )
    op.create_index(
        "ix_entity_aliases_normalized", "entity_aliases", ["alias_normalized"], unique=False
    )
    op.create_table(
        "topic_clusters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("topic_key", sa.String(length=300), nullable=False),
        sa.Column("canonical_topic", sa.Text(), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "ai",
                "technology",
                "gaming",
                "other",
                name="topic_cluster_category",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("representative_entity_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["representative_entity_id"],
            ["discovered_entities.id"],
            name=op.f("fk_topic_clusters_representative_entity_id_discovered_entities"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topic_clusters")),
        sa.UniqueConstraint("topic_key", name=op.f("uq_topic_clusters_topic_key")),
    )
    op.create_table(
        "discovery_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "evidence_type",
            sa.Enum(
                "official_announcement",
                "video_title",
                "video_description",
                "related_query",
                "search_suggestion",
                "community_discussion",
                "code_release",
                "curated_seed",
                "synthetic_test_data",
                name="discovery_item_evidence_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "simulated_evidence_type",
            sa.Enum(
                "official_announcement",
                "video_title",
                "video_description",
                "related_query",
                "search_suggestion",
                "community_discussion",
                "code_release",
                "curated_seed",
                "synthetic_test_data",
                name="discovery_item_simulated_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "category_hint",
            sa.Enum(
                "ai",
                "technology",
                "gaming",
                "other",
                name="discovery_item_category",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("quality", sa.Double(), nullable=False),
        sa.Column(
            "quality_flags",
            postgresql.ARRAY(sa.String(length=40)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "observed_queries", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "is_synthetic = (evidence_type = 'synthetic_test_data')",
            name=op.f("ck_discovery_items_synthetic_label_consistent"),
        ),
        sa.CheckConstraint(
            "quality >= 0 AND quality <= 100", name=op.f("ck_discovery_items_quality_range")
        ),
        sa.CheckConstraint(
            "simulated_evidence_type IS NULL OR is_synthetic",
            name=op.f("ck_discovery_items_simulated_only_for_synthetic"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_runs.id"],
            name=op.f("fk_discovery_items_run_id_discovery_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_sources.id"],
            name=op.f("fk_discovery_items_source_id_discovery_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_items")),
        sa.UniqueConstraint(
            "provider_name", "source_reference", name="uq_discovery_items_reference"
        ),
    )
    op.create_index(
        "ix_discovery_items_expires_at",
        "discovery_items",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_discovery_items_fingerprint", "discovery_items", ["content_fingerprint"], unique=False
    )
    op.create_index(
        "ix_discovery_items_published", "discovery_items", ["published_at"], unique=False
    )
    op.create_table(
        "discovery_run_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("items_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "quota_used",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_runs.id"],
            name=op.f("fk_discovery_run_sources_run_id_discovery_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_sources.id"],
            name=op.f("fk_discovery_run_sources_source_id_discovery_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_run_sources")),
    )
    op.create_index(
        "ix_discovery_run_sources_run", "discovery_run_sources", ["run_id"], unique=False
    )
    op.create_table(
        "intent_clusters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("topic_cluster_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_key", sa.String(length=300), nullable=False),
        sa.Column(
            "canonical_intent",
            sa.Enum(
                "tutorial",
                "how_to",
                "explanation",
                "news",
                "comparison",
                "review",
                "demonstration",
                "troubleshooting",
                "game_guide",
                "item_location",
                "challenge_guide",
                "product_discovery",
                "other",
                name="intent_cluster_intent",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("representative_query", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["topic_cluster_id"],
            ["topic_clusters.id"],
            name=op.f("fk_intent_clusters_topic_cluster_id_topic_clusters"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intent_clusters")),
        sa.UniqueConstraint("topic_cluster_id", "cluster_key", name="uq_intent_clusters_key"),
    )
    op.create_table(
        "discovered_keywords",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column("topic_cluster_id", sa.BigInteger(), nullable=True),
        sa.Column("intent_cluster_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "origin",
            sa.Enum(
                "observed_query",
                "generated_hypothesis",
                "verified_interest",
                name="keyword_origin",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "discovery_type",
            sa.Enum(
                "observed_query",
                "entity_template",
                "entity_comparison",
                "title_phrase",
                "llm_suggestion",
                "seed_term",
                name="keyword_discovery_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("template_id", sa.String(length=60), nullable=True),
        sa.Column(
            "primary_category",
            sa.Enum(
                "ai",
                "technology",
                "gaming",
                "other",
                name="keyword_category",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("category_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "primary_intent",
            sa.Enum(
                "tutorial",
                "how_to",
                "explanation",
                "news",
                "comparison",
                "review",
                "demonstration",
                "troubleshooting",
                "game_guide",
                "item_location",
                "challenge_guide",
                "product_discovery",
                "other",
                name="keyword_intent",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("intent_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "candidate_status",
            sa.Enum(
                "discovered",
                "validated",
                "prioritized",
                "monitoring",
                "dormant",
                "rejected",
                name="keyword_status",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("first_discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("novelty_score", sa.Double(), nullable=False),
        sa.Column(
            "novelty_kind",
            sa.Enum(
                "new_entity",
                "new_version",
                "new_event",
                "new_intent",
                "recurring",
                "evergreen",
                name="keyword_novelty_kind",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("novelty_components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority_score", sa.Double(), nullable=False),
        sa.Column("scoring_version", sa.String(length=32), nullable=False),
        sa.Column("score_components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "topic_assignment",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "intent_assignment",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("grounding", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_discovered_at >= first_discovered_at",
            name=op.f("ck_discovered_keywords_discovered_order"),
        ),
        sa.CheckConstraint(
            "novelty_score >= 0 AND novelty_score <= 100 AND priority_score >= 0 AND priority_score <= 100",
            name=op.f("ck_discovered_keywords_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["discovered_entities.id"],
            name=op.f("fk_discovered_keywords_entity_id_discovered_entities"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["intent_cluster_id"],
            ["intent_clusters.id"],
            name=op.f("fk_discovered_keywords_intent_cluster_id_intent_clusters"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["search_queries.id"],
            name=op.f("fk_discovered_keywords_query_id_search_queries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["topic_cluster_id"],
            ["topic_clusters.id"],
            name=op.f("fk_discovered_keywords_topic_cluster_id_topic_clusters"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovered_keywords")),
        sa.UniqueConstraint("query_id", name=op.f("uq_discovered_keywords_query_id")),
    )
    op.create_index(
        "ix_discovered_keywords_entity", "discovered_keywords", ["entity_id"], unique=False
    )
    op.create_index(
        "ix_discovered_keywords_rank",
        "discovered_keywords",
        ["primary_category", "candidate_status", "priority_score"],
        unique=False,
    )
    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("matched_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("extractor", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "verified",
                "needs_verification",
                "rejected",
                name="entity_mention_status",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "attributes",
            postgresql.ARRAY(sa.String(length=40)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("related", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["discovered_entities.id"],
            name=op.f("fk_entity_mentions_entity_id_discovered_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["discovery_items.id"],
            name=op.f("fk_entity_mentions_item_id_discovery_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_mentions")),
        sa.UniqueConstraint("entity_id", "item_id", name="uq_entity_mentions_pair"),
    )
    op.create_index("ix_entity_mentions_item", "entity_mentions", ["item_id"], unique=False)
    op.create_table(
        "discovery_evidence",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("keyword_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "evidence_type",
            sa.Enum(
                "official_announcement",
                "video_title",
                "video_description",
                "related_query",
                "search_suggestion",
                "community_discussion",
                "code_release",
                "curated_seed",
                "synthetic_test_data",
                name="discovery_evidence_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "relation",
            sa.Enum(
                "query_observed",
                "entity_mention",
                "template_grounding",
                name="discovery_evidence_relation",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_quality", sa.Double(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "evidence_quality >= 0 AND evidence_quality <= 100",
            name=op.f("ck_discovery_evidence_quality_range"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["discovery_items.id"],
            name=op.f("fk_discovery_evidence_item_id_discovery_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["keyword_id"],
            ["discovered_keywords.id"],
            name=op.f("fk_discovery_evidence_keyword_id_discovered_keywords"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_evidence")),
        sa.UniqueConstraint("keyword_id", "item_id", "relation", name="uq_discovery_evidence_link"),
    )
    op.create_index("ix_discovery_evidence_item", "discovery_evidence", ["item_id"], unique=False)
    op.create_table(
        "keyword_status_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("keyword_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "from_status",
            sa.Enum(
                "discovered",
                "validated",
                "prioritized",
                "monitoring",
                "dormant",
                "rejected",
                name="status_history_from",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "discovered",
                "validated",
                "prioritized",
                "monitoring",
                "dormant",
                "rejected",
                name="status_history_to",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["keyword_id"],
            ["discovered_keywords.id"],
            name=op.f("fk_keyword_status_history_keyword_id_discovered_keywords"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_runs.id"],
            name=op.f("fk_keyword_status_history_run_id_discovery_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_keyword_status_history")),
    )
    op.create_index(
        "ix_keyword_status_history_keyword",
        "keyword_status_history",
        ["keyword_id", "changed_at"],
        unique=False,
    )

    # Widen the record_kind CHECK on rejected_records for RecordKind.DISCOVERY_ITEM.
    op.drop_constraint(op.f("ck_rejected_records_record_kind"), "rejected_records", type_="check")
    op.create_check_constraint(
        op.f("ck_rejected_records_record_kind"),
        "rejected_records",
        "record_kind IN ('search_interest_observation', 'video', 'video_statistics', 'channel', 'video_search_result', 'discovery_item')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_rejected_records_record_kind"), "rejected_records", type_="check")
    op.create_check_constraint(
        op.f("ck_rejected_records_record_kind"),
        "rejected_records",
        "record_kind IN ('search_interest_observation', 'video', 'video_statistics', 'channel', 'video_search_result')",
    )
    op.drop_index("ix_keyword_status_history_keyword", table_name="keyword_status_history")
    op.drop_table("keyword_status_history")
    op.drop_index("ix_discovery_evidence_item", table_name="discovery_evidence")
    op.drop_table("discovery_evidence")
    op.drop_index("ix_entity_mentions_item", table_name="entity_mentions")
    op.drop_table("entity_mentions")
    op.drop_index("ix_discovered_keywords_rank", table_name="discovered_keywords")
    op.drop_index("ix_discovered_keywords_entity", table_name="discovered_keywords")
    op.drop_table("discovered_keywords")
    op.drop_table("intent_clusters")
    op.drop_index("ix_discovery_run_sources_run", table_name="discovery_run_sources")
    op.drop_table("discovery_run_sources")
    op.drop_index("ix_discovery_items_published", table_name="discovery_items")
    op.drop_index("ix_discovery_items_fingerprint", table_name="discovery_items")
    op.drop_index(
        "ix_discovery_items_expires_at",
        table_name="discovery_items",
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.drop_table("discovery_items")
    op.drop_table("topic_clusters")
    op.drop_index("ix_entity_aliases_normalized", table_name="entity_aliases")
    op.drop_table("entity_aliases")
    op.drop_index("ix_discovery_sources_category", table_name="discovery_sources")
    op.drop_table("discovery_sources")
    op.drop_index("ix_discovery_runs_started_at", table_name="discovery_runs")
    op.drop_table("discovery_runs")
    op.drop_index("ix_discovered_entities_first_seen", table_name="discovered_entities")
    op.drop_index("ix_discovered_entities_category_type", table_name="discovered_entities")
    op.drop_table("discovered_entities")
