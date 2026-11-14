"""Phase 8 operations and evaluation tables (migration 0009).

* ``service_heartbeats``   one row per running process (API, scheduler, worker): role, host,
                           PID, version, last beat. ``/health/workers`` reads it.
* ``scheduler_leases``     expiring leader lease per schedule with a monotonically increasing
                           fencing token: only the lease holder runs the schedule.
* ``scheduled_runs``       every scheduled stage execution (task, owner, token, correlation
                           ID, status, duration, summary, error).
* ``ops_alerts``           operational alerts, deduplicated by fingerprint, with a
                           notification cooldown and an open/resolved state.
* ``dead_letters``         permanently failed work items and malformed source records, kept
                           for an operator (payloads are secret-redacted before storage).
* ``provider_checks``      results of bounded live smoke tests per provider.
* ``system_state``         small key/value state (current operating mode and its history).
* ``evaluation_labels``    ground-truth labels, REAL and SYNTHETIC kept apart by ``origin``.
* ``experiments``          experiment registry: hypothesis, configuration, dataset identity,
                           split, metrics, baseline metrics and the adopt/reject decision.
* ``recommendation_outcomes``  opt-in performance outcomes the creator records for a
                           published recommendation (never fetched without authorisation).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, utc_now_sql


def _now() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


class ServiceHeartbeatRow(Base):
    __tablename__ = "service_heartbeats"
    __table_args__ = (
        sa.CheckConstraint(
            "role IN ('api','scheduler','worker','detector','frontend')", name="known_role"
        ),
        sa.CheckConstraint("status IN ('running','stopping','stopped')", name="known_status"),
        sa.Index("ix_service_heartbeats_role", "role", "last_seen_at"),
    )

    service_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    role: Mapped[str] = mapped_column(sa.String(16))
    hostname: Mapped[str] = mapped_column(sa.String(128))
    pid: Mapped[int] = mapped_column(sa.Integer)
    version: Mapped[str] = mapped_column(sa.String(32))
    status: Mapped[str] = mapped_column(sa.String(16))
    started_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    interval_seconds: Mapped[float] = mapped_column(sa.Double)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class SchedulerLeaseRow(Base):
    __tablename__ = "scheduler_leases"

    name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(sa.String(128))
    fencing_token: Mapped[int] = mapped_column(sa.BigInteger)
    acquired_at: Mapped[datetime]
    renewed_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class ScheduledRunRow(Base):
    __tablename__ = "scheduled_runs"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','skipped')", name="known_status"
        ),
        sa.Index("ix_scheduled_runs_task", "task", "started_at"),
        sa.Index("ix_scheduled_runs_status", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    task: Mapped[str] = mapped_column(sa.String(48))
    owner: Mapped[str] = mapped_column(sa.String(128))
    fencing_token: Mapped[int] = mapped_column(sa.BigInteger)
    correlation_id: Mapped[str] = mapped_column(sa.String(64))
    operating_mode: Mapped[str] = mapped_column(sa.String(40))
    status: Mapped[str] = mapped_column(sa.String(16))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error: Mapped[str | None] = mapped_column(sa.Text)


class OpsAlertRow(Base):
    __tablename__ = "ops_alerts"
    __table_args__ = (
        sa.CheckConstraint("severity IN ('info','warning','critical')", name="known_severity"),
        sa.CheckConstraint("status IN ('open','resolved')", name="known_status"),
        sa.Index("ix_ops_alerts_status", "status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(sa.String(200), unique=True)
    kind: Mapped[str] = mapped_column(sa.String(48))
    severity: Mapped[str] = mapped_column(sa.String(16))
    title: Mapped[str] = mapped_column(sa.Text)
    detail: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(16))
    occurrences: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    last_notified_at: Mapped[datetime | None]
    notifications_sent: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    resolved_at: Mapped[datetime | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class DeadLetterRow(Base):
    __tablename__ = "dead_letters"
    __table_args__ = (
        sa.UniqueConstraint("source", "reference", name="uq_dead_letters_ref"),
        sa.Index("ix_dead_letters_open", "resolved_at", "last_failed_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(sa.String(48))
    reference: Mapped[str] = mapped_column(sa.String(200))
    error_type: Mapped[str] = mapped_column(sa.String(64))
    error: Mapped[str] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="1")
    created_at: Mapped[datetime] = _now()
    last_failed_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    resolution: Mapped[str | None] = mapped_column(sa.Text)


class ProviderCheckRow(Base):
    __tablename__ = "provider_checks"
    __table_args__ = (sa.Index("ix_provider_checks_provider", "provider_name", "checked_at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    checked_at: Mapped[datetime]
    ok: Mapped[bool] = mapped_column(sa.Boolean)
    status: Mapped[str] = mapped_column(sa.String(40))
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    units_spent: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    signal_classes: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    detail: Mapped[str] = mapped_column(sa.Text)


class SystemStateRow(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime]


class EvaluationLabelRow(Base):
    __tablename__ = "evaluation_labels"
    __table_args__ = (
        sa.CheckConstraint("origin IN ('real','synthetic')", name="known_origin"),
        sa.CheckConstraint(
            "kind IN ('surge_event','no_surge_window','content_gap','forecast_outcome',"
            "'recommendation_outcome')",
            name="known_kind",
        ),
        sa.UniqueConstraint(
            "origin", "kind", "subject_key", "event_time", name="uq_evaluation_labels_subject"
        ),
        sa.Index("ix_evaluation_labels_kind", "origin", "kind", "event_time"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    origin: Mapped[str] = mapped_column(sa.String(16))
    kind: Mapped[str] = mapped_column(sa.String(32))
    subject_key: Mapped[str] = mapped_column(sa.String(200))
    event_time: Mapped[datetime]
    # when the label (the ground truth) became known; it must never be used as a feature
    # for decisions taken before this time
    known_at: Mapped[datetime]
    label: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(sa.String(200))
    labeler: Mapped[str] = mapped_column(sa.String(64))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _now()


class ExperimentRow(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        sa.CheckConstraint(
            "decision IN ('adopted','rejected','inconclusive','recorded')", name="known_decision"
        ),
        sa.CheckConstraint("data_origin IN ('real','synthetic','mixed')", name="known_origin"),
        sa.Index("ix_experiments_component", "component", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    component: Mapped[str] = mapped_column(sa.String(32))
    hypothesis: Mapped[str] = mapped_column(sa.Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    dataset: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    data_origin: Mapped[str] = mapped_column(sa.String(16))
    split: Mapped[str] = mapped_column(sa.String(32))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    baseline_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    decision: Mapped[str] = mapped_column(sa.String(16))
    decision_reason: Mapped[str] = mapped_column(sa.Text)
    code_version: Mapped[str] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = _now()


class RecommendationOutcomeRow(Base):
    __tablename__ = "recommendation_outcomes"
    __table_args__ = (
        sa.CheckConstraint(
            "source IN ('creator_entered','youtube_analytics_owner')", name="known_source"
        ),
        sa.CheckConstraint("consent", name="consent_required"),
        sa.CheckConstraint("days_since_publication >= 0", name="non_negative_days"),
        sa.CheckConstraint(
            "search_traffic_share IS NULL OR (search_traffic_share >= 0 AND "
            "search_traffic_share <= 1)",
            name="share_range",
        ),
        sa.UniqueConstraint(
            "user_key",
            "recommendation_id",
            "days_since_publication",
            name="uq_recommendation_outcomes_day",
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    user_key: Mapped[str] = mapped_column(sa.String(64))
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("video_recommendations.id", ondelete="CASCADE")
    )
    published_video_id: Mapped[str | None] = mapped_column(sa.String(16))
    days_since_publication: Mapped[int] = mapped_column(sa.Integer)
    views: Mapped[int | None] = mapped_column(sa.BigInteger)
    impressions: Mapped[int | None] = mapped_column(sa.BigInteger)
    click_through_rate: Mapped[float | None] = mapped_column(sa.Double)
    average_view_duration_seconds: Mapped[float | None] = mapped_column(sa.Double)
    # share of views from YouTube search; only when the creator supplies it (never inferred)
    search_traffic_share: Mapped[float | None] = mapped_column(sa.Double)
    source: Mapped[str] = mapped_column(sa.String(32))
    consent: Mapped[bool] = mapped_column(sa.Boolean)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    recorded_at: Mapped[datetime] = _now()
