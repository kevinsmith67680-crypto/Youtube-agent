"""Phase 4 schema: detection runs, detector state, anomaly results and surge episodes.

Relationships::

    ts_series 1--* detector_states            (one row per series and detector type)
    ts_observations 1--* anomaly_results      (one row per observation revision, detector,
                                               algorithm version and configuration version)
    monitoring_targets 1--* surge_events 1--* surge_event_transitions
    surge_events 1--* surge_evidence *--1 anomaly_results / ts_observations
    ts_series 1--1 detection_queue            (transactional outbox from Phase 3 writes)

Invariants enforced in the database:

* re-running detection on an unchanged observation cannot create a second result
  (unique observation/detector/version) or a second open episode (partial unique index on
  the episode key while the episode is open);
* severity is within 0-100, maximum severity >= current severity;
* a synthetic episode only ever references synthetic results (checked in code) and is
  flagged ``is_synthetic`` so production listings exclude it.

Derived rows carry the expiry of the source observations they were computed from and are
deleted by retention (or by cascade when the source observation is purged).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql
from trend_agent.discovery.models import Category
from trend_agent.schemas.detection import (
    BaselineModel,
    DetectorType,
    ResultStatus,
    RunStatus,
    RunType,
    SupportStatus,
)
from trend_agent.schemas.evidence import AlertStage, SurgeClassification
from trend_agent.schemas.monitoring import MeasurementCategory

OPEN_STATUS_SQL = "status IN ('watch', 'candidate', 'confirmed')"


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


class DetectionRunRow(Base):
    __tablename__ = "detection_runs"
    __table_args__ = (sa.Index("ix_detection_runs_started", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_type: Mapped[RunType] = mapped_column(str_enum(RunType, "detection_run_type"))
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "detection_run_status"))
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    as_of: Mapped[datetime]
    algorithm_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    threshold_version: Mapped[str] = mapped_column(sa.String(80))
    targets_evaluated: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    targets_skipped: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    series_evaluated: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    observations_evaluated: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    hypotheses_tested: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    candidates_found: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    accepted_after_control: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    alerts_created: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    episodes_updated: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    errors: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    error_details: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    multiple_testing: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    created_at: Mapped[datetime] = _created()


class DetectorStateRow(Base):
    __tablename__ = "detector_states"
    __table_args__ = (
        sa.UniqueConstraint("series_id", "detector_type", name="uq_detector_states_series"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.Index("ix_detector_states_target", "target_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    series_id: Mapped[int] = mapped_column(sa.ForeignKey("ts_series.id", ondelete="CASCADE"))
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    measurement_category: Mapped[MeasurementCategory] = mapped_column(
        str_enum(MeasurementCategory, "detector_state_measurement")
    )
    normalization_key: Mapped[str] = mapped_column(sa.String(64))
    detector_type: Mapped[DetectorType] = mapped_column(
        str_enum(DetectorType, "detector_state_type")
    )
    state_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    last_processed_observation_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ts_observations.id", ondelete="SET NULL")
    )
    last_processed_period_end: Mapped[datetime | None]
    watermark_observation_id: Mapped[int] = mapped_column(sa.BigInteger, server_default="0")
    algorithm_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    version: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    updated_at: Mapped[datetime]


class AnomalyResultRow(Base):
    __tablename__ = "anomaly_results"
    __table_args__ = (
        sa.UniqueConstraint(
            "observation_id",
            "detector_type",
            "algorithm_version",
            "configuration_version",
            name="uq_anomaly_results_identity",
        ),
        sa.CheckConstraint(
            "p_value IS NULL OR (p_value > 0 AND p_value <= 1)", name="p_value_range"
        ),
        sa.Index("ix_anomaly_results_series_period", "series_id", "period_start"),
        sa.Index("ix_anomaly_results_target", "target_id", "evaluated_at"),
        sa.Index("ix_anomaly_results_status", "result_status", "evaluated_at"),
        sa.Index(
            "ix_anomaly_results_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("detection_runs.id", ondelete="SET NULL")
    )
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    series_id: Mapped[int] = mapped_column(sa.ForeignKey("ts_series.id", ondelete="CASCADE"))
    observation_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ts_observations.id", ondelete="CASCADE")
    )
    observation_revision: Mapped[int] = mapped_column(sa.Integer)
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    detector_type: Mapped[DetectorType] = mapped_column(
        str_enum(DetectorType, "anomaly_result_detector")
    )
    baseline_model: Mapped[BaselineModel] = mapped_column(
        str_enum(BaselineModel, "anomaly_result_baseline_model")
    )
    baseline_value: Mapped[float | None] = mapped_column(sa.Double)
    baseline_dispersion: Mapped[float | None] = mapped_column(sa.Double)
    observed_value: Mapped[float | None] = mapped_column(sa.Double)
    anomaly_score: Mapped[float | None] = mapped_column(sa.Double)
    threshold: Mapped[float | None] = mapped_column(sa.Double)
    p_value: Mapped[float | None] = mapped_column(sa.Double)
    evidence_quality: Mapped[str] = mapped_column(sa.String(40))
    result_status: Mapped[ResultStatus] = mapped_column(
        str_enum(ResultStatus, "anomaly_result_status")
    )
    support_status: Mapped[SupportStatus] = mapped_column(
        str_enum(SupportStatus, "anomaly_result_support")
    )
    reasons: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    available_at: Mapped[datetime]
    evaluated_at: Mapped[datetime]
    is_late: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    algorithm_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class SurgeEventRow(Base):
    """One surge episode: a continuing event for one target, evidence category and source."""

    __tablename__ = "surge_events"
    __table_args__ = (
        sa.Index(
            "uq_surge_events_open_episode",
            "episode_key",
            unique=True,
            postgresql_where=sa.text(OPEN_STATUS_SQL),
        ),
        sa.CheckConstraint("severity >= 0 AND severity <= 100", name="severity_range"),
        sa.CheckConstraint(
            "maximum_severity >= severity AND maximum_severity <= 100", name="max_severity"
        ),
        sa.CheckConstraint(
            "status <> 'confirmed' OR evidence_classification = 'direct_search_surge'",
            name="confirmed_only_direct",
        ),
        sa.Index("ix_surge_events_status_category", "status", "category"),
        sa.Index("ix_surge_events_target", "target_id", "first_detected_at"),
        sa.Index("ix_surge_events_key_resolved", "episode_key", "resolved_at"),
        sa.Index(
            "ix_surge_events_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    episode_key: Mapped[str] = mapped_column(sa.String(64))
    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    series_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ts_series.id", ondelete="SET NULL")
    )
    query_id: Mapped[int] = mapped_column(sa.ForeignKey("search_queries.id", ondelete="RESTRICT"))
    keyword_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("discovered_keywords.id", ondelete="SET NULL")
    )
    topic_cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("topic_clusters.id", ondelete="SET NULL")
    )
    category: Mapped[Category] = mapped_column(str_enum(Category, "surge_event_category"))
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    measurement_category: Mapped[MeasurementCategory] = mapped_column(
        str_enum(MeasurementCategory, "surge_event_measurement")
    )
    is_proxy: Mapped[bool] = mapped_column(sa.Boolean)
    evidence_classification: Mapped[SurgeClassification] = mapped_column(
        str_enum(SurgeClassification, "surge_event_classification")
    )
    region: Mapped[str] = mapped_column(sa.String(16))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    status: Mapped[AlertStage] = mapped_column(str_enum(AlertStage, "surge_event_status"))
    confidence_status: Mapped[SupportStatus] = mapped_column(
        str_enum(SupportStatus, "surge_event_confidence")
    )
    episode_start: Mapped[datetime]
    first_detected_at: Mapped[datetime]
    last_updated_at: Mapped[datetime]
    last_observation_period_end: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    severity: Mapped[float] = mapped_column(sa.Double)
    maximum_severity: Mapped[float] = mapped_column(sa.Double)
    peak_value: Mapped[float | None] = mapped_column(sa.Double)
    peak_period_start: Mapped[datetime | None]
    peak_observation_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ts_observations.id", ondelete="SET NULL")
    )
    baseline_expected: Mapped[float | None] = mapped_column(sa.Double)
    baseline_dispersion: Mapped[float | None] = mapped_column(sa.Double)
    reopen_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    notifications: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    explanation: Mapped[str] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    algorithm_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class SurgeEventTransitionRow(Base):
    __tablename__ = "surge_event_transitions"
    __table_args__ = (sa.Index("ix_surge_event_transitions_event", "surge_event_id", "at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    surge_event_id: Mapped[int] = mapped_column(
        sa.ForeignKey("surge_events.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("detection_runs.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(sa.String(16))
    from_status: Mapped[str | None] = mapped_column(sa.String(24))
    to_status: Mapped[str] = mapped_column(sa.String(24))
    severity: Mapped[float] = mapped_column(sa.Double)
    is_notification: Mapped[bool] = mapped_column(sa.Boolean)
    reason: Mapped[str] = mapped_column(sa.Text)
    at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)


class SurgeEvidenceRow(Base):
    __tablename__ = "surge_evidence"
    __table_args__ = (
        sa.UniqueConstraint(
            "surge_event_id", "observation_id", "evidence_type", name="uq_surge_evidence_link"
        ),
        sa.Index("ix_surge_evidence_result", "detector_result_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    surge_event_id: Mapped[int] = mapped_column(
        sa.ForeignKey("surge_events.id", ondelete="CASCADE")
    )
    observation_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ts_observations.id", ondelete="CASCADE")
    )
    detector_result_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("anomaly_results.id", ondelete="CASCADE")
    )
    evidence_type: Mapped[str] = mapped_column(sa.String(40))
    source_reference: Mapped[str] = mapped_column(sa.Text)
    contribution: Mapped[float] = mapped_column(sa.Double)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime]


class DetectionEvaluationRow(Base):
    __tablename__ = "detection_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_dataset: Mapped[str] = mapped_column(sa.String(120))
    dataset_split: Mapped[str] = mapped_column(sa.String(40))
    detector_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime]
    precision: Mapped[float | None] = mapped_column(sa.Double)
    recall: Mapped[float | None] = mapped_column(sa.Double)
    false_alert_rate: Mapped[float | None] = mapped_column(sa.Double)  # per series-day
    false_alerts_per_day: Mapped[float | None] = mapped_column(sa.Double)
    detection_delay: Mapped[float | None] = mapped_column(sa.Double)  # median, periods
    detection_delay_p90: Mapped[float | None] = mapped_column(sa.Double)
    duplication_rate: Mapped[float | None] = mapped_column(sa.Double)
    coverage: Mapped[float | None] = mapped_column(sa.Double)
    severity_spearman: Mapped[float | None] = mapped_column(sa.Double)
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    evaluation_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class DetectionCalibrationRow(Base):
    """Empirical null distributions / thresholds fitted on a training split."""

    __tablename__ = "detection_calibrations"
    __table_args__ = (
        sa.UniqueConstraint(
            "calibration_version", "stratum_key", name="uq_detection_calibrations_stratum"
        ),
        sa.Index("ix_detection_calibrations_active", "stratum_key", "active"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    calibration_version: Mapped[str] = mapped_column(sa.String(80))
    stratum_key: Mapped[str] = mapped_column(sa.String(200))
    source: Mapped[str] = mapped_column(sa.String(24))  # synthetic | stored_history
    dataset: Mapped[str] = mapped_column(sa.String(120))
    method: Mapped[str] = mapped_column(sa.String(60))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    train_start: Mapped[datetime | None]
    train_end: Mapped[datetime | None]
    active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    created_at: Mapped[datetime] = _created()


class DetectionQueueRow(Base):
    """Transactional outbox: series with new or revised observations awaiting detection."""

    __tablename__ = "detection_queue"
    __table_args__ = (sa.Index("ix_detection_queue_marked", "last_marked_at"),)

    series_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ts_series.id", ondelete="CASCADE"), primary_key=True
    )
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="CASCADE")
    )
    first_marked_at: Mapped[datetime]
    last_marked_at: Mapped[datetime]
    marks: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    earliest_period_start: Mapped[datetime | None]
    claimed_by: Mapped[str | None] = mapped_column(sa.String(80))
    lease_expires_at: Mapped[datetime | None]
