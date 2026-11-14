"""PostgreSQL schema.

Design notes
------------
* Every timestamp is ``timestamptz``; the application only ever writes aware UTC values and
  connections are pinned to ``timezone=UTC``.
* Event time (``observation_start``/``observation_end``), collection time
  (``collected_at``), provider availability time (``available_at``) and row insertion time
  (``created_at``) are separate columns and are never assumed equal.
* Semantic labels are VARCHAR + CHECK constraints generated from the domain enums.
* Cross-field invariants that protect interpretation (synthetic labelling, YouTube vs
  external property, value vs sample-quality) are enforced in the database as well as in
  application validation, so a bug in one layer cannot store a mislabelled record.
* Third-party data carries ``expires_at``/``metadata_expires_at`` for retention enforcement.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql
from trend_agent.schemas.enums import (
    DataNature,
    IngestionStatus,
    MeasurementType,
    ProviderType,
    RecordKind,
    RejectionReason,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), onupdate=sa.func.now(), nullable=False)


class ProviderRow(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(sa.Integer, sa.Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(64), unique=True)
    display_name: Mapped[str] = mapped_column(sa.Text)
    version: Mapped[str] = mapped_column(sa.String(32))
    provider_type: Mapped[ProviderType] = mapped_column(str_enum(ProviderType, "provider_type"))
    enabled: Mapped[bool] = mapped_column(sa.Boolean)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)))
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class SearchQueryRow(Base):
    __tablename__ = "search_queries"
    __table_args__ = (
        sa.UniqueConstraint(
            "canonical_query",
            "language",
            "region",
            name="uq_search_queries_canonical_language_region",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint("length(canonical_query) BETWEEN 1 AND 200", name="canonical_length"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    canonical_query: Mapped[str] = mapped_column(sa.Text)
    original_query: Mapped[str] = mapped_column(sa.Text)
    language: Mapped[str | None] = mapped_column(sa.String(16))
    region: Mapped[str | None] = mapped_column(sa.String(16))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class IngestionRunRow(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        sa.CheckConstraint(
            "records_received >= 0 AND records_accepted >= 0 AND records_rejected >= 0 "
            "AND records_duplicate >= 0 AND records_withheld >= 0",
            name="non_negative_counts",
        ),
        sa.CheckConstraint("estimated_quota_used >= 0", name="non_negative_quota"),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completed_after_start"
        ),
        sa.Index("ix_ingestion_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    operation: Mapped[str] = mapped_column(sa.String(40))
    request_params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    status: Mapped[IngestionStatus] = mapped_column(str_enum(IngestionStatus, "ingestion_status"))
    records_received: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    records_accepted: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    records_rejected: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    records_duplicate: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    records_withheld: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    estimated_quota_used: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    quota_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error_type: Mapped[str | None] = mapped_column(sa.String(64))
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SearchInterestObservationRow(Base):
    __tablename__ = "search_interest_observations"
    __table_args__ = (
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
        sa.CheckConstraint("observation_end >= observation_start", name="window_order"),
        sa.CheckConstraint(
            "value IS NULL OR value NOT IN ('NaN'::float8, 'Infinity'::float8, "
            "'-Infinity'::float8)",
            name="finite_value",
        ),
        sa.CheckConstraint(
            "(sample_quality IN ('complete', 'partial_period') AND value IS NOT NULL) "
            "OR (sample_quality IN ('suppressed', 'missing') AND value IS NULL) "
            "OR sample_quality = 'low_volume'",
            name="value_matches_quality",
        ),
        sa.CheckConstraint(
            "is_synthetic = (data_nature = 'synthetic')", name="synthetic_label_consistent"
        ),
        sa.CheckConstraint("data_nature <> 'unavailable'", name="no_unavailable_values"),
        sa.CheckConstraint(
            "measurement_type <> 'direct_search_interest' OR search_property = 'youtube_search'",
            name="direct_requires_youtube_property",
        ),
        sa.CheckConstraint(
            "measurement_type <> 'external_search_interest' OR search_property NOT IN "
            "('youtube_search', 'not_applicable', 'unknown')",
            name="external_requires_external_property",
        ),
        sa.CheckConstraint("length(normalization_key) = 64", name="normalization_key_length"),
        sa.Index("ix_sio_query_window", "query_id", "observation_start"),
        sa.Index("ix_sio_provider_collected", "provider_id", "collected_at"),
        sa.Index("ix_sio_normalization_key", "normalization_key"),
        sa.Index(
            "ix_sio_expires_at", "expires_at", postgresql_where=sa.text("expires_at IS NOT NULL")
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    query_id: Mapped[int] = mapped_column(sa.ForeignKey("search_queries.id", ondelete="RESTRICT"))
    provider_id: Mapped[int] = mapped_column(sa.ForeignKey("providers.id", ondelete="RESTRICT"))
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    provider_version: Mapped[str] = mapped_column(sa.String(32))
    measurement_type: Mapped[MeasurementType] = mapped_column(
        str_enum(MeasurementType, "measurement_type")
    )
    search_property: Mapped[SearchProperty] = mapped_column(
        str_enum(SearchProperty, "search_property")
    )
    granularity: Mapped[TimeGranularity] = mapped_column(str_enum(TimeGranularity, "granularity"))
    observation_start: Mapped[datetime]
    observation_end: Mapped[datetime]
    collected_at: Mapped[datetime]
    available_at: Mapped[datetime | None]
    value: Mapped[float | None] = mapped_column(sa.Double)
    scale_type: Mapped[ScaleType] = mapped_column(str_enum(ScaleType, "scale_type"))
    normalization_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    normalization_key: Mapped[str] = mapped_column(sa.String(64))
    geography: Mapped[str] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    sample_quality: Mapped[SampleQuality] = mapped_column(str_enum(SampleQuality, "sample_quality"))
    data_nature: Mapped[DataNature] = mapped_column(str_enum(DataNature, "data_nature"))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    source_reference: Mapped[str] = mapped_column(sa.Text)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class ChannelRow(Base):
    __tablename__ = "channels"
    __table_args__ = (sa.Index("ix_channels_metadata_expires_at", "metadata_expires_at"),)

    channel_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    title: Mapped[str] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)
    custom_url: Mapped[str | None] = mapped_column(sa.Text)
    country: Mapped[str | None] = mapped_column(sa.String(8))
    published_at: Mapped[datetime | None]
    thumbnails: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    source_provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    metadata_collected_at: Mapped[datetime]
    metadata_expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class VideoRow(Base):
    __tablename__ = "videos"
    __table_args__ = (
        sa.Index("ix_videos_channel_id", "channel_id"),
        sa.Index("ix_videos_metadata_expires_at", "metadata_expires_at"),
        sa.CheckConstraint(
            "metadata_expires_at > metadata_collected_at", name="expiry_after_collection"
        ),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="non_negative_duration"
        ),
    )

    video_id: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    channel_id: Mapped[str] = mapped_column(sa.String(64))
    channel_title: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)
    published_at: Mapped[datetime | None]
    duration_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    duration_iso: Mapped[str | None] = mapped_column(sa.String(32))
    category_id: Mapped[str | None] = mapped_column(sa.String(8))
    tags: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    default_language: Mapped[str | None] = mapped_column(sa.String(16))
    default_audio_language: Mapped[str | None] = mapped_column(sa.String(16))
    live_broadcast_content: Mapped[str | None] = mapped_column(sa.String(16))
    thumbnails: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    source_provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    metadata_collected_at: Mapped[datetime]
    metadata_expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class VideoStatisticsRow(Base):
    __tablename__ = "video_statistics"
    __table_args__ = (
        sa.UniqueConstraint(
            "video_id",
            "collected_at",
            "source_provider_id",
            name="uq_video_statistics_snapshot",
        ),
        sa.CheckConstraint(
            "(view_count IS NULL OR view_count >= 0) AND (like_count IS NULL OR like_count >= 0)"
            " AND (comment_count IS NULL OR comment_count >= 0)",
            name="non_negative_counts",
        ),
        sa.CheckConstraint("expires_at > collected_at", name="expiry_after_collection"),
        sa.Index("ix_video_statistics_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    video_id: Mapped[str] = mapped_column(sa.ForeignKey("videos.video_id", ondelete="CASCADE"))
    collected_at: Mapped[datetime]
    view_count: Mapped[int | None] = mapped_column(sa.BigInteger)
    like_count: Mapped[int | None] = mapped_column(sa.BigInteger)
    comment_count: Mapped[int | None] = mapped_column(sa.BigInteger)
    source_provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()


class VideoSearchResultRow(Base):
    """Which videos a provider returned for a query at a point in time (supply snapshot)."""

    __tablename__ = "video_search_results"
    __table_args__ = (
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
        sa.CheckConstraint("rank >= 1 AND page >= 1", name="positive_rank"),
        sa.Index("ix_video_search_results_query", "query_id", "collected_at"),
        sa.Index("ix_video_search_results_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    query_id: Mapped[int] = mapped_column(sa.ForeignKey("search_queries.id", ondelete="RESTRICT"))
    provider_id: Mapped[int] = mapped_column(sa.ForeignKey("providers.id", ondelete="RESTRICT"))
    video_id: Mapped[str] = mapped_column(sa.ForeignKey("videos.video_id", ondelete="CASCADE"))
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    region: Mapped[str | None] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    search_order: Mapped[str] = mapped_column(sa.String(16))
    published_after: Mapped[datetime | None]
    published_before: Mapped[datetime | None]
    rank: Mapped[int] = mapped_column(sa.Integer)
    page: Mapped[int] = mapped_column(sa.Integer)
    collected_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()


class ProviderRateLimitRow(Base):
    """Locally estimated quota usage per provider bucket and reset window."""

    __tablename__ = "provider_rate_limits"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider_name", "bucket", "window_start", name="uq_provider_rate_limits_window"
        ),
        sa.CheckConstraint("units_used >= 0", name="non_negative_usage"),
        sa.CheckConstraint("window_end > window_start", name="window_order"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    # Name rather than FK: quota must be enforceable before a provider row is synced.
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    bucket: Mapped[str] = mapped_column(sa.String(32))
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    effective_limit: Mapped[int] = mapped_column(sa.Integer)
    units_used: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    server_exhausted: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    server_exhausted_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = _updated()


class RejectedRecordRow(Base):
    """Quarantined records with diagnostic reasons (payloads are secret-redacted)."""

    __tablename__ = "rejected_records"
    __table_args__ = (
        sa.Index("ix_rejected_records_run", "ingestion_run_id"),
        sa.Index(
            "ix_rejected_records_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("ingestion_runs.id", ondelete="CASCADE")
    )
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    record_kind: Mapped[RecordKind] = mapped_column(str_enum(RecordKind, "record_kind"))
    reason_code: Mapped[RejectionReason] = mapped_column(
        str_enum(RejectionReason, "rejection_reason")
    )
    reason_detail: Mapped[str] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


# Phase 2 tables live in their own module; importing it registers them on Base.metadata
# wherever the Phase 1 models are imported (application, Alembic, tests).
from trend_agent.db import content_models as _content_models  # noqa: E402,F401  (Phase 5 tables)
from trend_agent.db import detection_models as _detection_models  # noqa: E402,F401  (Phase 4 tables)
from trend_agent.db import discovery_models as _discovery_models  # noqa: E402, F401
from trend_agent.db import forecast_models as _forecast_models  # noqa: E402,F401  (Phase 6 tables)
from trend_agent.db import monitoring_models as _monitoring_models  # noqa: E402,F401  (Phase 3 tables)
from trend_agent.db import ops_models as _ops_models  # noqa: E402,F401  (Phase 8 tables)
from trend_agent.db import recommendation_models as _recommendation_models  # noqa: E402,F401  (Phase 7 tables)
from trend_agent.db import verification_models as _verification_models  # noqa: E402,F401  (verification layer)
