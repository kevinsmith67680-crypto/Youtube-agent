"""Initial schema: providers, queries, observations, videos, channels, statistics,
search-result snapshots, ingestion runs, quarantine and local quota state.

Reviewed by hand after autogeneration (constraints, NULLS NOT DISTINCT, indexes).

Revision ID: 0001
Revises:
Create Date: 2026-11-13 12:30:57.934426+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "provider_rate_limits",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("bucket", sa.String(length=32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_limit", sa.Integer(), nullable=False),
        sa.Column("units_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "server_exhausted", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("server_exhausted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "units_used >= 0", name=op.f("ck_provider_rate_limits_non_negative_usage")
        ),
        sa.CheckConstraint(
            "window_end > window_start", name=op.f("ck_provider_rate_limits_window_order")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_rate_limits")),
        sa.UniqueConstraint(
            "provider_name", "bucket", "window_start", name="uq_provider_rate_limits_window"
        ),
    )
    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column(
            "provider_type",
            sa.Enum(
                "official_api",
                "licensed_third_party",
                "synthetic",
                name="provider_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        sa.Column("descriptor", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_providers_name")),
    )
    op.create_table(
        "search_queries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("canonical_query", sa.Text(), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("region", sa.String(length=16), nullable=True),
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
            "length(canonical_query) BETWEEN 1 AND 200",
            name=op.f("ck_search_queries_canonical_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_queries")),
        sa.UniqueConstraint(
            "canonical_query",
            "language",
            "region",
            name="uq_search_queries_canonical_language_region",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "channels",
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("custom_url", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "thumbnails",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("source_provider_id", sa.Integer(), nullable=False),
        sa.Column("metadata_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["source_provider_id"],
            ["providers.id"],
            name=op.f("fk_channels_source_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_channels")),
    )
    op.create_index(
        "ix_channels_metadata_expires_at", "channels", ["metadata_expires_at"], unique=False
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("request_params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                name="ingestion_status",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("records_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_accepted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_rejected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_duplicate", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_withheld", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_quota_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "quota_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_ingestion_runs_completed_after_start"),
        ),
        sa.CheckConstraint(
            "estimated_quota_used >= 0", name=op.f("ck_ingestion_runs_non_negative_quota")
        ),
        sa.CheckConstraint(
            "records_received >= 0 AND records_accepted >= 0 AND records_rejected >= 0 AND records_duplicate >= 0 AND records_withheld >= 0",
            name=op.f("ck_ingestion_runs_non_negative_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_ingestion_runs_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_runs")),
    )
    op.create_index("ix_ingestion_runs_started_at", "ingestion_runs", ["started_at"], unique=False)
    op.create_table(
        "videos",
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("channel_title", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("duration_iso", sa.String(length=32), nullable=True),
        sa.Column("category_id", sa.String(length=8), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("default_language", sa.String(length=16), nullable=True),
        sa.Column("default_audio_language", sa.String(length=16), nullable=True),
        sa.Column("live_broadcast_content", sa.String(length=16), nullable=True),
        sa.Column(
            "thumbnails",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("source_provider_id", sa.Integer(), nullable=False),
        sa.Column("metadata_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_expires_at", sa.DateTime(timezone=True), nullable=False),
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
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name=op.f("ck_videos_non_negative_duration"),
        ),
        sa.CheckConstraint(
            "metadata_expires_at > metadata_collected_at",
            name=op.f("ck_videos_expiry_after_collection"),
        ),
        sa.ForeignKeyConstraint(
            ["source_provider_id"],
            ["providers.id"],
            name=op.f("fk_videos_source_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("video_id", name=op.f("pk_videos")),
    )
    op.create_index("ix_videos_channel_id", "videos", ["channel_id"], unique=False)
    op.create_index(
        "ix_videos_metadata_expires_at", "videos", ["metadata_expires_at"], unique=False
    )
    op.create_table(
        "rejected_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("ingestion_run_id", sa.UUID(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column(
            "record_kind",
            sa.Enum(
                "search_interest_observation",
                "video",
                "video_statistics",
                "channel",
                "video_search_result",
                name="record_kind",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "reason_code",
            sa.Enum(
                "missing_provenance",
                "provider_mismatch",
                "unrecognized_provider_version",
                "invalid_timestamp",
                "invalid_window",
                "granularity_mismatch",
                "non_finite_value",
                "value_out_of_range",
                "inconsistent_sample_quality",
                "invalid_region",
                "invalid_language",
                "invalid_query",
                "unsupported_measurement_type",
                "mislabeled_search_property",
                "invalid_scale",
                "missing_normalization_context",
                "synthetic_label_mismatch",
                "invalid_enum_value",
                "duplicate_conflict",
                "conflicts_with_stored",
                "malformed_provider_item",
                name="rejection_reason",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("reason_detail", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_rejected_records_ingestion_run_id_ingestion_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_rejected_records_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rejected_records")),
    )
    op.create_index(
        "ix_rejected_records_expires_at",
        "rejected_records",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_rejected_records_run", "rejected_records", ["ingestion_run_id"], unique=False
    )
    op.create_table(
        "search_interest_observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("ingestion_run_id", sa.UUID(), nullable=True),
        sa.Column("provider_version", sa.String(length=32), nullable=False),
        sa.Column(
            "measurement_type",
            sa.Enum(
                "direct_search_interest",
                "search_suggestion",
                "video_activity",
                "external_search_interest",
                "unknown",
                name="measurement_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "search_property",
            sa.Enum(
                "youtube_search",
                "google_web_search",
                "google_news_search",
                "google_image_search",
                "google_shopping",
                "other_external",
                "not_applicable",
                "unknown",
                name="search_property",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "granularity",
            sa.Enum(
                "point",
                "hour",
                "day",
                "week",
                "month",
                "year",
                name="granularity",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("observation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value", sa.Double(), nullable=True),
        sa.Column(
            "scale_type",
            sa.Enum(
                "relative_per_request",
                "consistent_index",
                "absolute_count",
                "estimated_count",
                "rank",
                "presence",
                name="scale_type",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("normalization_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalization_key", sa.String(length=64), nullable=False),
        sa.Column("geography", sa.String(length=16), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column(
            "sample_quality",
            sa.Enum(
                "complete",
                "partial_period",
                "low_volume",
                "suppressed",
                "missing",
                name="sample_quality",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "data_nature",
            sa.Enum(
                "actual",
                "sampled",
                "estimated",
                "synthetic",
                "unavailable",
                name="data_nature",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(sample_quality IN ('complete', 'partial_period') AND value IS NOT NULL) OR (sample_quality IN ('suppressed', 'missing') AND value IS NULL) OR sample_quality = 'low_volume'",
            name=op.f("ck_search_interest_observations_value_matches_quality"),
        ),
        sa.CheckConstraint(
            "data_nature <> 'unavailable'",
            name=op.f("ck_search_interest_observations_no_unavailable_values"),
        ),
        sa.CheckConstraint(
            "is_synthetic = (data_nature = 'synthetic')",
            name=op.f("ck_search_interest_observations_synthetic_label_consistent"),
        ),
        sa.CheckConstraint(
            "measurement_type <> 'direct_search_interest' OR search_property = 'youtube_search'",
            name=op.f("ck_search_interest_observations_direct_requires_youtube_property"),
        ),
        sa.CheckConstraint(
            "measurement_type <> 'external_search_interest' OR search_property NOT IN ('youtube_search', 'not_applicable', 'unknown')",
            name=op.f("ck_search_interest_observations_external_requires_external_property"),
        ),
        sa.CheckConstraint(
            "value IS NULL OR value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)",
            name=op.f("ck_search_interest_observations_finite_value"),
        ),
        sa.CheckConstraint(
            "length(normalization_key) = 64",
            name=op.f("ck_search_interest_observations_normalization_key_length"),
        ),
        sa.CheckConstraint(
            "observation_end >= observation_start",
            name=op.f("ck_search_interest_observations_window_order"),
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_search_interest_observations_ingestion_run_id_ingestion_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_search_interest_observations_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["search_queries.id"],
            name=op.f("fk_search_interest_observations_query_id_search_queries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_interest_observations")),
        sa.UniqueConstraint(
            "query_id",
            "provider_id",
            "measurement_type",
            "search_property",
            "geography",
            "language",
            "granularity",
            "observation_start",
            "observation_end",
            "normalization_key",
            name="uq_search_interest_observations_identity",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_sio_expires_at",
        "search_interest_observations",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_sio_normalization_key",
        "search_interest_observations",
        ["normalization_key"],
        unique=False,
    )
    op.create_index(
        "ix_sio_provider_collected",
        "search_interest_observations",
        ["provider_id", "collected_at"],
        unique=False,
    )
    op.create_index(
        "ix_sio_query_window",
        "search_interest_observations",
        ["query_id", "observation_start"],
        unique=False,
    )
    op.create_table(
        "video_search_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("query_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("ingestion_run_id", sa.UUID(), nullable=True),
        sa.Column("region", sa.String(length=16), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("search_order", sa.String(length=16), nullable=False),
        sa.Column("published_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rank >= 1 AND page >= 1", name=op.f("ck_video_search_results_positive_rank")
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_video_search_results_ingestion_run_id_ingestion_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_video_search_results_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["search_queries.id"],
            name=op.f("fk_video_search_results_query_id_search_queries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.video_id"],
            name=op.f("fk_video_search_results_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_search_results")),
        sa.UniqueConstraint(
            "query_id",
            "provider_id",
            "video_id",
            "collected_at",
            "search_order",
            "region",
            "language",
            name="uq_video_search_results_snapshot",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_video_search_results_expires_at", "video_search_results", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_video_search_results_query",
        "video_search_results",
        ["query_id", "collected_at"],
        unique=False,
    )
    op.create_table(
        "video_statistics",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("like_count", sa.BigInteger(), nullable=True),
        sa.Column("comment_count", sa.BigInteger(), nullable=True),
        sa.Column("source_provider_id", sa.Integer(), nullable=False),
        sa.Column("ingestion_run_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(view_count IS NULL OR view_count >= 0) AND (like_count IS NULL OR like_count >= 0) AND (comment_count IS NULL OR comment_count >= 0)",
            name=op.f("ck_video_statistics_non_negative_counts"),
        ),
        sa.CheckConstraint(
            "expires_at > collected_at", name=op.f("ck_video_statistics_expiry_after_collection")
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ingestion_runs.id"],
            name=op.f("fk_video_statistics_ingestion_run_id_ingestion_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_provider_id"],
            ["providers.id"],
            name=op.f("fk_video_statistics_source_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.video_id"],
            name=op.f("fk_video_statistics_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_statistics")),
        sa.UniqueConstraint(
            "video_id", "collected_at", "source_provider_id", name="uq_video_statistics_snapshot"
        ),
    )
    op.create_index(
        "ix_video_statistics_expires_at", "video_statistics", ["expires_at"], unique=False
    )


def downgrade() -> None:

    op.drop_index("ix_video_statistics_expires_at", table_name="video_statistics")
    op.drop_table("video_statistics")
    op.drop_index("ix_video_search_results_query", table_name="video_search_results")
    op.drop_index("ix_video_search_results_expires_at", table_name="video_search_results")
    op.drop_table("video_search_results")
    op.drop_index("ix_sio_query_window", table_name="search_interest_observations")
    op.drop_index("ix_sio_provider_collected", table_name="search_interest_observations")
    op.drop_index("ix_sio_normalization_key", table_name="search_interest_observations")
    op.drop_index(
        "ix_sio_expires_at",
        table_name="search_interest_observations",
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.drop_table("search_interest_observations")
    op.drop_index("ix_rejected_records_run", table_name="rejected_records")
    op.drop_index(
        "ix_rejected_records_expires_at",
        table_name="rejected_records",
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.drop_table("rejected_records")
    op.drop_index("ix_videos_metadata_expires_at", table_name="videos")
    op.drop_index("ix_videos_channel_id", table_name="videos")
    op.drop_table("videos")
    op.drop_index("ix_ingestion_runs_started_at", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index("ix_channels_metadata_expires_at", table_name="channels")
    op.drop_table("channels")
    op.drop_table("search_queries")
    op.drop_table("providers")
    op.drop_table("provider_rate_limits")
