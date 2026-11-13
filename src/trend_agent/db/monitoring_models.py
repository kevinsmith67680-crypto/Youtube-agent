"""Phase 3 schema: historical monitoring, time series and the durable job queue.

Relationships::

    discovered_keywords 1--* monitoring_targets *--1 search_queries
    monitoring_targets 1--* monitoring_target_events (validated state transitions)
    monitoring_targets 1--* monitoring_jobs 1--* monitoring_job_attempts
    monitoring_targets 1--* ts_series 1--* ts_observations (append-only revisions)
    data_policies 1--* ts_observations ; monitoring_backfills 1--* monitoring_jobs

Invariants enforced in the database:

* one target per (query, region, language, measurement category)
* one job per idempotency key; one row per (series, window, revision) and at most one
  *current* revision per (series, window) - re-running a job can never duplicate data
* value/quality consistency (missing or suppressed values are NULL, never zero), finite
  values, window order, synthetic labelling consistency
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql
from trend_agent.discovery.models import Category
from trend_agent.schemas.enums import (
    DataNature,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.monitoring import (
    ErrorCategory,
    HealthStatus,
    JobStatus,
    JobType,
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
    QualityStatus,
    RevisionKind,
)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), onupdate=sa.func.now(), nullable=False)


class DataPolicyRow(Base):
    """Retention/permission policies applied to stored observations (versioned)."""

    __tablename__ = "data_policies"

    id: Mapped[str] = mapped_column(sa.String(80), primary_key=True)  # "<key>@<version>"
    policy_key: Mapped[str] = mapped_column(sa.String(60))
    version: Mapped[str] = mapped_column(sa.String(40))
    description: Mapped[str] = mapped_column(sa.Text)
    retention_days: Mapped[int | None] = mapped_column(sa.Integer)
    storage_allowed: Mapped[bool] = mapped_column(sa.Boolean)
    requires_authorization: Mapped[bool] = mapped_column(sa.Boolean)
    authorized: Mapped[bool] = mapped_column(sa.Boolean)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    config_hash: Mapped[str] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class MonitoringTargetRow(Base):
    __tablename__ = "monitoring_targets"
    __table_args__ = (
        sa.UniqueConstraint(
            "query_id",
            "region",
            "language",
            "measurement_category",
            name="uq_monitoring_targets_identity",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint("priority_score >= 0 AND priority_score <= 100", name="priority_range"),
        sa.CheckConstraint("consecutive_failures >= 0", name="failures_nonneg"),
        sa.Index("ix_monitoring_targets_due", "status", "next_collection_at"),
        sa.Index("ix_monitoring_targets_category_status", "category", "status"),
        sa.Index("ix_monitoring_targets_keyword", "keyword_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    keyword_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_keywords.id", ondelete="SET NULL")
    )
    query_id: Mapped[int] = mapped_column(sa.ForeignKey("search_queries.id", ondelete="RESTRICT"))
    category: Mapped[Category] = mapped_column(str_enum(Category, "monitoring_target_category"))
    topic_cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("topic_clusters.id", ondelete="SET NULL")
    )
    intent_cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("intent_clusters.id", ondelete="SET NULL")
    )
    region: Mapped[str] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    measurement_category: Mapped[MeasurementCategory] = mapped_column(
        str_enum(MeasurementCategory, "monitoring_target_measurement")
    )
    is_proxy: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    preferred_provider: Mapped[str | None] = mapped_column(sa.String(64))
    fallback_providers: Mapped[list[str]] = mapped_column(ARRAY(sa.String(64)), server_default="{}")
    provider_in_use: Mapped[str | None] = mapped_column(sa.String(64))
    tier: Mapped[MonitoringTier] = mapped_column(str_enum(MonitoringTier, "monitoring_tier"))
    tier_reason: Mapped[str] = mapped_column(sa.Text, server_default="")
    tier_changed_at: Mapped[datetime | None]
    status: Mapped[MonitoringStatus] = mapped_column(
        str_enum(MonitoringStatus, "monitoring_status")
    )
    status_reason: Mapped[str] = mapped_column(sa.Text)
    status_changed_at: Mapped[datetime]
    priority_score: Mapped[float] = mapped_column(sa.Double)
    novelty_kind: Mapped[str | None] = mapped_column(sa.String(32))
    entity_type: Mapped[str | None] = mapped_column(sa.String(32))
    entity_first_seen_at: Mapped[datetime | None]
    time_sensitive_until: Mapped[datetime | None]
    manual_override: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    activated_at: Mapped[datetime | None]
    last_attempt_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_new_observation_at: Mapped[datetime | None]
    last_no_data_at: Mapped[datetime | None]
    next_collection_at: Mapped[datetime | None]
    latest_period_end: Mapped[datetime | None]
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    last_error_category: Mapped[str | None] = mapped_column(sa.String(32))
    config_version: Mapped[str] = mapped_column(sa.String(40))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class MonitoringTargetEventRow(Base):
    __tablename__ = "monitoring_target_events"
    __table_args__ = (sa.Index("ix_monitoring_target_events_target", "target_id", "at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(sa.String(16))  # status | tier | override
    from_value: Mapped[str | None] = mapped_column(sa.String(40))
    to_value: Mapped[str] = mapped_column(sa.String(40))
    reason: Mapped[str] = mapped_column(sa.Text)
    actor: Mapped[str] = mapped_column(sa.String(40))
    at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class SeriesRow(Base):
    """One time series = one scale. Observations from different series are never mixed."""

    __tablename__ = "ts_series"
    __table_args__ = (
        sa.Index("ix_ts_series_query", "query_id"),
        sa.Index("ix_ts_series_target", "target_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    series_key: Mapped[str] = mapped_column(sa.String(64), unique=True)
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="SET NULL")
    )
    query_id: Mapped[int] = mapped_column(sa.ForeignKey("search_queries.id", ondelete="RESTRICT"))
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    measurement_category: Mapped[MeasurementCategory] = mapped_column(
        str_enum(MeasurementCategory, "ts_series_measurement")
    )
    simulated_category: Mapped[MeasurementCategory | None] = mapped_column(
        str_enum(MeasurementCategory, "ts_series_simulated")
    )
    is_proxy: Mapped[bool] = mapped_column(sa.Boolean)
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    search_property: Mapped[SearchProperty] = mapped_column(
        str_enum(SearchProperty, "ts_series_property")
    )
    region: Mapped[str] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    granularity: Mapped[TimeGranularity] = mapped_column(
        str_enum(TimeGranularity, "ts_series_granularity")
    )
    scale_type: Mapped[ScaleType] = mapped_column(str_enum(ScaleType, "ts_series_scale"))
    unit: Mapped[str] = mapped_column(sa.String(40))
    normalization_key: Mapped[str] = mapped_column(sa.String(64))
    normalization_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class ObservationRow(Base):
    __tablename__ = "ts_observations"
    __table_args__ = (
        sa.UniqueConstraint(
            "series_id",
            "period_start",
            "period_end",
            "revision",
            name="uq_ts_observations_revision",
        ),
        sa.Index(
            "uq_ts_observations_current",
            "series_id",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        sa.CheckConstraint("period_end >= period_start", name="window_order"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
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
            "is_synthetic = (measurement_category = 'synthetic_test_observation')",
            name="synthetic_label_consistent",
        ),
        sa.CheckConstraint(
            "simulated_category IS NULL OR is_synthetic", name="simulated_only_for_synthetic"
        ),
        sa.CheckConstraint(
            "provider_published_at IS NULL OR provider_published_at <= collected_at",
            name="published_before_collected",
        ),
        sa.CheckConstraint("ingested_at >= collected_at", name="ingested_after_collected"),
        sa.CheckConstraint("confirm_count >= 1", name="confirm_count_positive"),
        sa.Index("ix_ts_obs_query_period", "query_id", "period_start"),
        sa.Index("ix_ts_obs_series_period", "series_id", "period_start"),
        sa.Index("ix_ts_obs_provider_collected", "provider_name", "collected_at"),
        sa.Index("ix_ts_obs_category_region", "measurement_category", "region"),
        sa.Index("ix_ts_obs_normalization_key", "normalization_key"),
        sa.Index("ix_ts_obs_target", "target_id"),
        sa.Index(
            "ix_ts_obs_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    series_id: Mapped[int] = mapped_column(sa.ForeignKey("ts_series.id", ondelete="CASCADE"))
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="SET NULL")
    )
    query_id: Mapped[int] = mapped_column(sa.ForeignKey("search_queries.id", ondelete="RESTRICT"))
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    provider_version: Mapped[str] = mapped_column(sa.String(32))
    measurement_category: Mapped[MeasurementCategory] = mapped_column(
        str_enum(MeasurementCategory, "ts_obs_measurement")
    )
    simulated_category: Mapped[MeasurementCategory | None] = mapped_column(
        str_enum(MeasurementCategory, "ts_obs_simulated")
    )
    is_proxy: Mapped[bool] = mapped_column(sa.Boolean)
    unit: Mapped[str] = mapped_column(sa.String(40))
    dedup_key: Mapped[str] = mapped_column(sa.String(64))
    provider_observation_id: Mapped[str | None] = mapped_column(sa.String(200))
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    granularity: Mapped[TimeGranularity] = mapped_column(
        str_enum(TimeGranularity, "ts_obs_granularity")
    )
    event_time: Mapped[datetime | None]
    source_timezone: Mapped[str | None] = mapped_column(sa.String(64))
    provider_published_at: Mapped[datetime | None]
    first_available_at: Mapped[datetime | None]
    collected_at: Mapped[datetime]
    ingested_at: Mapped[datetime]
    value: Mapped[float | None] = mapped_column(sa.Double)
    raw_value: Mapped[float | None] = mapped_column(sa.Double)
    scale_type: Mapped[ScaleType] = mapped_column(str_enum(ScaleType, "ts_obs_scale"))
    normalization_key: Mapped[str] = mapped_column(sa.String(64))
    region: Mapped[str] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    search_property: Mapped[SearchProperty] = mapped_column(
        str_enum(SearchProperty, "ts_obs_property")
    )
    data_nature: Mapped[DataNature] = mapped_column(str_enum(DataNature, "ts_obs_data_nature"))
    sample_quality: Mapped[SampleQuality] = mapped_column(
        str_enum(SampleQuality, "ts_obs_sample_quality")
    )
    quality_status: Mapped[QualityStatus] = mapped_column(
        str_enum(QualityStatus, "ts_obs_quality_status")
    )
    source_reference: Mapped[str] = mapped_column(sa.Text)
    data_policy_id: Mapped[str] = mapped_column(
        sa.ForeignKey("data_policies.id", ondelete="RESTRICT")
    )
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    revision: Mapped[int] = mapped_column(sa.Integer)
    revision_kind: Mapped[RevisionKind] = mapped_column(
        str_enum(RevisionKind, "ts_obs_revision_kind")
    )
    supersedes_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ts_observations.id", ondelete="SET NULL")
    )
    is_current: Mapped[bool] = mapped_column(sa.Boolean)
    superseded_at: Mapped[datetime | None]
    last_confirmed_at: Mapped[datetime]
    confirm_count: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("monitoring_jobs.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None]


class MonitoringBackfillRow(Base):
    __tablename__ = "monitoring_backfills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signature: Mapped[str] = mapped_column(sa.String(64), unique=True)
    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    range_start: Mapped[datetime]
    range_end: Mapped[datetime]
    status: Mapped[str] = mapped_column(sa.String(16))
    windows_planned: Mapped[int] = mapped_column(sa.Integer)
    estimated_requests: Mapped[int] = mapped_column(sa.Integer)
    requested_by: Mapped[str] = mapped_column(sa.String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class MonitoringJobRow(Base):
    __tablename__ = "monitoring_jobs"
    __table_args__ = (
        sa.CheckConstraint("attempt_count >= 0", name="attempts_nonneg"),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        sa.Index("ix_monitoring_jobs_claim", "status", "scheduled_at", "priority"),
        sa.Index("ix_monitoring_jobs_target", "target_id", "status"),
        sa.Index(
            "ix_monitoring_jobs_lease",
            "lease_expires_at",
            postgresql_where=sa.text("status IN ('claimed', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), unique=True)
    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    job_type: Mapped[JobType] = mapped_column(str_enum(JobType, "monitoring_job_type"))
    category: Mapped[Category] = mapped_column(str_enum(Category, "monitoring_job_category"))
    scheduled_at: Mapped[datetime]
    priority: Mapped[int] = mapped_column(sa.Integer)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus, "monitoring_job_status"))
    attempt_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer)
    deferral_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(sa.String(120))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    retry_after: Mapped[datetime | None]
    error_category: Mapped[ErrorCategory | None] = mapped_column(
        str_enum(ErrorCategory, "monitoring_job_error_category")
    )
    error_details: Mapped[str | None] = mapped_column(sa.Text)
    estimated_cost: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    quota_bucket: Mapped[str | None] = mapped_column(sa.String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    backfill_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("monitoring_backfills.id", ondelete="SET NULL")
    )
    plan_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class MonitoringJobAttemptRow(Base):
    __tablename__ = "monitoring_job_attempts"
    __table_args__ = (
        sa.Index("ix_monitoring_job_attempts_job", "job_id"),
        sa.Index("ix_monitoring_job_attempts_started", "started_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_jobs.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(sa.Integer)
    worker: Mapped[str] = mapped_column(sa.String(120))
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    category: Mapped[Category] = mapped_column(str_enum(Category, "monitoring_attempt_category"))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    outcome: Mapped[str] = mapped_column(sa.String(24))
    error_category: Mapped[ErrorCategory | None] = mapped_column(
        str_enum(ErrorCategory, "monitoring_attempt_error_category")
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    window_start: Mapped[datetime | None]
    window_end: Mapped[datetime | None]
    received: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    new: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    unchanged: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    revised: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    conflicting: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    missing_values: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    in_batch_duplicates: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    withheld: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    rejected_by_reason: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    quota_used: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    scheduled_delay_ms: Mapped[int | None] = mapped_column(sa.BigInteger)


class MonitoringPlanRunRow(Base):
    __tablename__ = "monitoring_plan_runs"
    __table_args__ = (sa.Index("ix_monitoring_plan_runs_started", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime]
    dry_run: Mapped[bool] = mapped_column(sa.Boolean)
    capacity: Mapped[int] = mapped_column(sa.Integer)
    jobs_created: Mapped[int] = mapped_column(sa.Integer)
    config_version: Mapped[str] = mapped_column(sa.String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ProviderHealthRow(Base):
    __tablename__ = "monitoring_provider_health"

    provider_name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    status: Mapped[HealthStatus] = mapped_column(str_enum(HealthStatus, "provider_health_status"))
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    last_success_at: Mapped[datetime | None]
    last_failure_at: Mapped[datetime | None]
    last_error_category: Mapped[str | None] = mapped_column(sa.String(32))
    last_error_message: Mapped[str | None] = mapped_column(sa.Text)
    unhealthy_since: Mapped[datetime | None]
    updated_at: Mapped[datetime]
