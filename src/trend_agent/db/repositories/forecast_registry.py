"""Model registry, training runs and evaluations in PostgreSQL (Phase 6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from trend_agent.db.forecast_models import (
    ForecastEvaluationRow,
    ForecastModelEventRow,
    ForecastModelRow,
    ForecastTrainingRunRow,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.forecasting.model_registry import ModelEvent
from trend_agent.schemas.model_metadata import (
    DataOrigin,
    ModelMetadata,
    ModelStatus,
    ModelType,
    Period,
    TargetFamily,
    TargetType,
)


def _to_meta(r: ForecastModelRow) -> ModelMetadata:
    return ModelMetadata(
        model_id=r.id,
        model_name=r.model_name,
        version=r.version,
        model_type=ModelType(r.model_type),
        target_family=TargetFamily(r.target_family),
        target_type=TargetType(r.target_type),
        target_definition=r.target_definition,
        horizon_hours=r.horizon_hours,
        granularity=r.granularity,
        category_scope=r.category_scope,
        provider_scope=tuple(r.provider_scope or ()),
        data_origin=DataOrigin(r.data_origin),
        feature_schema_version=r.feature_schema_version,
        feature_names=tuple(r.feature_names),
        dataset_version=r.dataset_version,
        dataset_hash=r.dataset_hash,
        training_period=Period(start=r.training_start, end=r.training_end),
        calibration_period=Period(start=r.calibration_start, end=r.calibration_end),
        test_period=Period(start=r.test_start, end=r.test_end),
        hyperparameters=r.hyperparameters or {},
        metrics=r.metrics or {},
        calibration=r.calibration or {},
        artifact_checksum=r.artifact_checksum,
        trained_at=r.trained_at,
        code_version=r.code_version,
        status=ModelStatus(r.status),
        status_reasons=tuple(r.status_reasons or ()),
    )


class SqlModelStore:
    """``ModelStore`` backed by ``forecast_models`` / ``forecast_model_events``."""

    def __init__(self, factory: SessionFactory, training_run_id: uuid.UUID | None = None) -> None:
        self._factory = factory
        self.training_run_id = training_run_id

    def add(self, meta: ModelMetadata, location: str) -> None:
        with transaction(self._factory) as s:
            s.add(
                ForecastModelRow(
                    id=meta.model_id,
                    training_run_id=self.training_run_id,
                    model_name=meta.model_name,
                    model_type=meta.model_type.value,
                    version=meta.version,
                    target_family=meta.target_family.value,
                    target_type=meta.target_type.value,
                    target_definition=meta.target_definition,
                    horizon_hours=meta.horizon_hours,
                    granularity=meta.granularity,
                    category_scope=meta.category_scope,
                    provider_scope=list(meta.provider_scope),
                    data_origin=meta.data_origin.value,
                    feature_schema_version=meta.feature_schema_version,
                    feature_names=list(meta.feature_names),
                    dataset_version=meta.dataset_version,
                    dataset_hash=meta.dataset_hash,
                    training_start=meta.training_period.start,
                    training_end=meta.training_period.end,
                    calibration_start=meta.calibration_period.start,
                    calibration_end=meta.calibration_period.end,
                    test_start=meta.test_period.start,
                    test_end=meta.test_period.end,
                    hyperparameters=_jsonable(meta.hyperparameters),
                    metrics=_jsonable(meta.metrics),
                    calibration=_jsonable(meta.calibration),
                    artifact_location=location,
                    artifact_checksum=meta.artifact_checksum,
                    code_version=meta.code_version,
                    status=meta.status.value,
                    status_reasons=list(meta.status_reasons),
                    trained_at=meta.trained_at,
                )
            )
            s.flush()
            s.add(
                ForecastModelEventRow(
                    model_id=meta.model_id,
                    from_status=None,
                    to_status=meta.status.value,
                    reason="registered",
                    evidence={},
                    at=meta.trained_at,
                )
            )

    def get(self, model_id: str) -> tuple[ModelMetadata, str] | None:
        with transaction(self._factory) as s:
            r = s.get(ForecastModelRow, model_id)
            return (_to_meta(r), r.artifact_location) if r is not None else None

    def all(self) -> list[ModelMetadata]:
        with transaction(self._factory) as s:
            rows = s.execute(sa.select(ForecastModelRow).order_by(ForecastModelRow.created_at))
            return [_to_meta(r) for r in rows.scalars()]

    def set_status(
        self,
        model_id: str,
        status: ModelStatus,
        reason: str,
        at: datetime,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        with transaction(self._factory) as s:
            r = s.get(ForecastModelRow, model_id, with_for_update=True)
            if r is None:
                raise LookupError(model_id)
            previous = r.status
            r.status = status.value
            r.updated_at = at
            s.add(
                ForecastModelEventRow(
                    model_id=model_id,
                    from_status=previous,
                    to_status=status.value,
                    reason=reason,
                    evidence=_jsonable(evidence or {}),
                    at=at,
                )
            )

    def events(self, model_id: str | None = None) -> list[ModelEvent]:
        with transaction(self._factory) as s:
            stmt = sa.select(ForecastModelEventRow).order_by(
                ForecastModelEventRow.at, ForecastModelEventRow.id
            )
            if model_id is not None:
                stmt = stmt.where(ForecastModelEventRow.model_id == model_id)
            return [
                ModelEvent(e.model_id, e.from_status, e.to_status, e.reason, e.at, e.evidence)
                for e in s.execute(stmt).scalars()
            ]


class TrainingRunRepository:
    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def start(
        self,
        *,
        started_at: datetime,
        source: str,
        origin: str,
        horizons: list[int],
        configuration_version: str,
        feature_schema_version: str,
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        with transaction(self._factory) as s:
            s.add(
                ForecastTrainingRunRow(
                    id=run_id,
                    started_at=started_at,
                    status="running",
                    source=source,
                    data_origin=origin,
                    horizons=horizons,
                    configuration_version=configuration_version,
                    feature_schema_version=feature_schema_version,
                )
            )
        return run_id

    def finish(
        self,
        run_id: uuid.UUID,
        *,
        completed_at: datetime,
        status: str,
        manifest: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        periods: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with transaction(self._factory) as s:
            r = s.get(ForecastTrainingRunRow, run_id)
            if r is None:
                raise LookupError(str(run_id))
            r.completed_at = completed_at
            r.status = status
            if manifest is not None:
                r.manifest = _jsonable(manifest)
                r.dataset_version = manifest.get("dataset_version")
                r.dataset_hash = manifest.get("rows_hash")
                r.sample_count = int(manifest.get("rows") or 0)
            r.metrics = _jsonable(metrics or {})
            r.periods = _jsonable(periods or {})
            r.error = error

    def runs(self, limit: int = 20) -> list[ForecastTrainingRunRow]:
        with transaction(self._factory) as s:
            rows = list(
                s.execute(
                    sa.select(ForecastTrainingRunRow)
                    .order_by(ForecastTrainingRunRow.started_at.desc())
                    .limit(limit)
                ).scalars()
            )
            s.expunge_all()
            return rows


class EvaluationRepository:
    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def add(
        self,
        *,
        model_id: str | None,
        kind: str,
        metrics: dict[str, Any],
        at: datetime,
        dataset_version: str | None = None,
        period: tuple[datetime | None, datetime | None] = (None, None),
        slices: dict[str, Any] | None = None,
        synthetic: bool = False,
    ) -> int:
        with transaction(self._factory) as s:
            row = ForecastEvaluationRow(
                model_id=model_id,
                evaluation_kind=kind,
                period_start=period[0],
                period_end=period[1],
                dataset_version=dataset_version,
                metrics=_jsonable(metrics),
                slices=_jsonable(slices or {}),
                is_synthetic=synthetic,
                evaluated_at=at,
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def latest(self, kind: str | None = None, limit: int = 20) -> list[ForecastEvaluationRow]:
        with transaction(self._factory) as s:
            stmt = sa.select(ForecastEvaluationRow).order_by(
                ForecastEvaluationRow.evaluated_at.desc()
            )
            if kind:
                stmt = stmt.where(ForecastEvaluationRow.evaluation_kind == kind)
            rows = list(s.execute(stmt.limit(limit)).scalars())
            s.expunge_all()
            return rows


def _jsonable(o: Any) -> Any:
    import json
    import math

    import numpy as np

    def conv(x: Any) -> Any:
        if isinstance(x, dict):
            return {str(k): conv(v) for k, v in x.items()}
        if isinstance(x, list | tuple):
            return [conv(v) for v in x]
        if isinstance(x, np.generic):
            x = x.item()
        if isinstance(x, float) and not math.isfinite(x):
            return None
        if isinstance(x, datetime):
            return x.isoformat()
        return x

    out = conv(o)
    json.dumps(out)
    return out
