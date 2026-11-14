"""Phase 6 forecasting tables (migration 0007).

* ``forecast_training_runs``  one training invocation: dataset version/hash, manifest
                              (incl. the leakage audit), periods, per-scope summaries.
* ``forecast_models``         the registry: one row per trained model with its target
                              definition, scope, data origin, periods, metrics, artifact
                              location + SHA-256 and status. At most one CHAMPION per scope.
* ``forecast_model_events``   every status change (registration, promotion, retirement).
* ``forecast_predictions``    one forecast per (series, models, cutoff, horizon); the id is
                              deterministic, so re-running the same forecast is a no-op.
* ``forecast_trajectories``, ``peak_estimates``, ``opportunity_windows`` - children.
* ``forecast_evaluations``    test / rolling / comparison / drift / matured evaluations.

Predictions inherit the earliest expiry of the observations they were computed from and
are purged with them (children cascade).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, utc_now_sql


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


class ForecastTrainingRunRow(Base):
    __tablename__ = "forecast_training_runs"
    __table_args__ = (sa.Index("ix_forecast_training_runs_started", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(sa.String(16))  # running | completed | failed
    source: Mapped[str] = mapped_column(sa.String(16))  # database | synthetic
    data_origin: Mapped[str] = mapped_column(sa.String(16))  # real | synthetic
    dataset_version: Mapped[str | None] = mapped_column(sa.String(40))
    dataset_hash: Mapped[str | None] = mapped_column(sa.String(64))
    sample_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    horizons: Mapped[list[int]] = mapped_column(ARRAY(sa.Integer), server_default="{}")
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    feature_schema_version: Mapped[str] = mapped_column(sa.String(40))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    periods: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created()


class ForecastModelRow(Base):
    __tablename__ = "forecast_models"
    __table_args__ = (
        sa.Index(
            "uq_forecast_models_champion_scope",
            "target_family",
            "target_type",
            "granularity",
            "horizon_hours",
            "data_origin",
            "category_scope",
            unique=True,
            postgresql_where=sa.text("status = 'champion'"),
        ),
        sa.Index("ix_forecast_models_scope", "target_family", "granularity", "horizon_hours"),
        sa.Index("ix_forecast_models_status", "status"),
        sa.CheckConstraint("horizon_hours > 0", name="positive_horizon"),
        sa.CheckConstraint("data_origin IN ('real', 'synthetic')", name="known_origin"),
        sa.CheckConstraint(
            "status IN ('experimental','validated','champion','challenger','retired','rejected')",
            name="known_status",
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(80), primary_key=True)
    training_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("forecast_training_runs.id", ondelete="SET NULL")
    )
    model_name: Mapped[str] = mapped_column(sa.String(64))
    model_type: Mapped[str] = mapped_column(sa.String(32))
    version: Mapped[str] = mapped_column(sa.String(80))
    target_family: Mapped[str] = mapped_column(sa.String(40))
    target_type: Mapped[str] = mapped_column(sa.String(24))
    target_definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    horizon_hours: Mapped[int] = mapped_column(sa.Integer)
    granularity: Mapped[str] = mapped_column(sa.String(16))
    category_scope: Mapped[str] = mapped_column(sa.String(32), server_default="all")
    provider_scope: Mapped[list[str]] = mapped_column(ARRAY(sa.String(64)), server_default="{}")
    data_origin: Mapped[str] = mapped_column(sa.String(16))
    feature_schema_version: Mapped[str] = mapped_column(sa.String(40))
    feature_names: Mapped[list[str]] = mapped_column(JSONB)
    dataset_version: Mapped[str] = mapped_column(sa.String(40))
    dataset_hash: Mapped[str] = mapped_column(sa.String(64))
    training_start: Mapped[datetime | None]
    training_end: Mapped[datetime | None]
    calibration_start: Mapped[datetime | None]
    calibration_end: Mapped[datetime | None]
    test_start: Mapped[datetime | None]
    test_end: Mapped[datetime | None]
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    calibration: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    artifact_location: Mapped[str] = mapped_column(sa.Text)
    artifact_checksum: Mapped[str] = mapped_column(sa.String(64))
    code_version: Mapped[str] = mapped_column(sa.String(32))
    status: Mapped[str] = mapped_column(sa.String(16))
    status_reasons: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    trained_at: Mapped[datetime]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class ForecastModelEventRow(Base):
    __tablename__ = "forecast_model_events"
    __table_args__ = (sa.Index("ix_forecast_model_events_model", "model_id", "at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    model_id: Mapped[str] = mapped_column(sa.ForeignKey("forecast_models.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(sa.String(16))
    to_status: Mapped[str] = mapped_column(sa.String(16))
    reason: Mapped[str] = mapped_column(sa.Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    at: Mapped[datetime]


class ForecastPredictionRow(Base):
    __tablename__ = "forecast_predictions"
    __table_args__ = (
        sa.Index("ix_forecast_predictions_series_cutoff", "series_id", "data_cutoff"),
        sa.Index("ix_forecast_predictions_query", "query_id", "prediction_timestamp"),
        sa.Index("ix_forecast_predictions_status", "forecast_status"),
        sa.Index("ix_forecast_predictions_expires", "expires_at"),
        sa.UniqueConstraint(
            "series_id",
            "model_ref",
            "prediction_timestamp",
            "horizon_hours",
            name="uq_forecast_prediction",
        ),
        sa.CheckConstraint(
            "breakout_probability IS NULL OR (breakout_probability >= 0 AND "
            "breakout_probability <= 1)",
            name="probability_range",
        ),
        sa.CheckConstraint("horizon_hours > 0", name="positive_horizon"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    series_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ts_series.id", ondelete="SET NULL")
    )
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="SET NULL")
    )
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    model_ref: Mapped[str] = mapped_column(sa.Text)  # sorted model ids used (or "baseline")
    breakout_model_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("forecast_models.id", ondelete="SET NULL")
    )
    trajectory_model_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("forecast_models.id", ondelete="SET NULL")
    )
    prediction_timestamp: Mapped[datetime]
    data_cutoff: Mapped[datetime]
    horizon_hours: Mapped[int] = mapped_column(sa.Integer)
    target_family: Mapped[str] = mapped_column(sa.String(40))
    source_classification: Mapped[str] = mapped_column(sa.String(40))
    forecast_status: Mapped[str] = mapped_column(sa.String(32))
    predicted_value: Mapped[float | None] = mapped_column(sa.Double)
    breakout_probability: Mapped[float | None] = mapped_column(sa.Double)
    breakout_score: Mapped[float | None] = mapped_column(sa.Double)
    continuation_probability: Mapped[float | None] = mapped_column(sa.Double)
    calibration_status: Mapped[str | None] = mapped_column(sa.String(40))
    uncertainty: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    quality_status: Mapped[str] = mapped_column(sa.String(16))
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class ForecastTrajectoryRow(Base):
    __tablename__ = "forecast_trajectories"
    __table_args__ = (
        sa.UniqueConstraint("prediction_id", "lead_hours", name="uq_trajectory_lead"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("forecast_predictions.id", ondelete="CASCADE")
    )
    lead_hours: Mapped[int] = mapped_column(sa.Integer)
    forecast_timestamp: Mapped[datetime]
    predicted_value: Mapped[float | None] = mapped_column(sa.Double)
    lower_bound: Mapped[float | None] = mapped_column(sa.Double)
    upper_bound: Mapped[float | None] = mapped_column(sa.Double)
    interval_level: Mapped[float | None] = mapped_column(sa.Double)
    interval_method: Mapped[str] = mapped_column(sa.String(64))


class PeakEstimateRow(Base):
    __tablename__ = "peak_estimates"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("forecast_predictions.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(sa.String(40))
    window_hours: Mapped[int] = mapped_column(sa.Integer)
    earliest_hours: Mapped[float | None] = mapped_column(sa.Double)
    latest_hours: Mapped[float | None] = mapped_column(sa.Double)
    median_hours: Mapped[float | None] = mapped_column(sa.Double)
    probability_beyond_horizon: Mapped[float | None] = mapped_column(sa.Double)
    censoring_status: Mapped[str] = mapped_column(sa.String(32))
    confidence_status: Mapped[str] = mapped_column(sa.String(32))
    estimate_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class OpportunityWindowRow(Base):
    __tablename__ = "opportunity_windows"
    __table_args__ = (
        sa.UniqueConstraint("prediction_id", "production_profile", name="uq_window_profile"),
        sa.Index("ix_opportunity_windows_status", "opportunity_status"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("forecast_predictions.id", ondelete="CASCADE")
    )
    production_profile: Mapped[str] = mapped_column(sa.String(40))
    production_hours: Mapped[float] = mapped_column(sa.Double)
    estimated_publication_time: Mapped[datetime]
    opportunity_status: Mapped[str] = mapped_column(sa.String(24))
    probability_elevated: Mapped[float | None] = mapped_column(sa.Double)
    estimated_persistence: Mapped[str | None] = mapped_column(sa.Text)
    uncertainty_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class ForecastEvaluationRow(Base):
    __tablename__ = "forecast_evaluations"
    __table_args__ = (sa.Index("ix_forecast_evaluations_model", "model_id", "evaluated_at"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    model_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("forecast_models.id", ondelete="CASCADE")
    )
    evaluation_kind: Mapped[str] = mapped_column(sa.String(24))
    period_start: Mapped[datetime | None]
    period_end: Mapped[datetime | None]
    dataset_version: Mapped[str | None] = mapped_column(sa.String(40))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    slices: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    evaluated_at: Mapped[datetime]
