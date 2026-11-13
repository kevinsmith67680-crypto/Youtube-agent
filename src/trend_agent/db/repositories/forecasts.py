"""Forecast predictions and their children: idempotent writes, reads, retention."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trend_agent.db.forecast_models import (
    ForecastModelRow,
    ForecastPredictionRow,
    ForecastTrajectoryRow,
    OpportunityWindowRow,
    PeakEstimateRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import MonitoringTargetRow, SeriesRow
from trend_agent.db.repositories.forecast_registry import _jsonable
from trend_agent.schemas.forecast import ForecastOutput, PeakStatus


def _exists(s: Session, model: Any, key: Any) -> bool:
    return key is not None and s.get(model, key) is not None


class ForecastRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def save(self, out: ForecastOutput, *, expires_at: datetime | None) -> bool:
        """Insert one forecast; an identical prediction id already stored is a no-op."""
        pid = uuid.UUID(out.prediction_id)
        if self._s.get(ForecastPredictionRow, pid) is not None:
            return False
        models = [str(m) for m in out.evidence.get("models", [])]
        known = {m for m in models if self._s.get(ForecastModelRow, m) is not None}
        breakout_id = (
            out.breakout.model.model_id
            if out.breakout is not None
            and out.breakout.model is not None
            and out.breakout.model.model_id in known
            else None
        )
        traj_id = next((m for m in models if "trajectory" in m and m in known), None)
        traj = out.trajectory[0] if out.trajectory else None
        stmt = (
            insert(ForecastPredictionRow)
            .values(
                id=pid,
                series_id=out.series_id if _exists(self._s, SeriesRow, out.series_id) else None,
                target_id=out.target_id
                if _exists(self._s, MonitoringTargetRow, out.target_id)
                else None,
                query_id=out.query_id if _exists(self._s, SearchQueryRow, out.query_id) else None,
                provider_name=out.provider,
                model_ref="|".join(sorted(models)) or "baseline",
                breakout_model_id=breakout_id,
                trajectory_model_id=traj_id,
                prediction_timestamp=out.prediction_timestamp,
                data_cutoff=out.data_cutoff,
                horizon_hours=out.forecast_horizon_hours,
                target_family=out.target_family,
                source_classification=out.source_classification.value,
                forecast_status=out.status.value,
                predicted_value=traj.value if traj else None,
                breakout_probability=out.breakout.probability if out.breakout else None,
                breakout_score=out.breakout.score if out.breakout else None,
                continuation_probability=out.continuation.probability if out.continuation else None,
                calibration_status=out.breakout.calibration_status.value if out.breakout else None,
                uncertainty=_jsonable(out.uncertainty.model_dump(mode="json")),
                quality_status=out.data_quality.status.value,
                features=_jsonable(out.features),
                output=out.model_dump(mode="json"),
                is_synthetic=out.synthetic,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing()
        )
        res = self._s.execute(stmt)
        if not getattr(res, "rowcount", 0):
            return False
        for p in out.trajectory:
            self._s.add(
                ForecastTrajectoryRow(
                    prediction_id=pid,
                    lead_hours=p.lead_hours,
                    forecast_timestamp=p.forecast_time,
                    predicted_value=p.value,
                    lower_bound=p.lower,
                    upper_bound=p.upper,
                    interval_level=p.interval_level,
                    interval_method=p.interval_method,
                )
            )
        pe = out.peak_estimate
        if pe is not None:
            censoring = (
                "right_censored_likely"
                if pe.status is PeakStatus.PEAK_LIKELY_BEYOND_HORIZON
                else "not_estimable"
                if pe.status is PeakStatus.PEAK_NOT_ESTIMABLE
                else "within_window"
            )
            self._s.add(
                PeakEstimateRow(
                    prediction_id=pid,
                    status=pe.status.value,
                    window_hours=pe.horizon_hours,
                    earliest_hours=pe.earliest_hours,
                    latest_hours=pe.latest_hours,
                    median_hours=pe.median_hours,
                    probability_beyond_horizon=pe.probability_beyond_horizon,
                    censoring_status=censoring,
                    confidence_status="experimental" if pe.experimental else "validated",
                    estimate_metadata={"analogs": pe.analogs, "note": pe.note},
                )
            )
        for w in out.opportunity_windows:
            self._s.add(
                OpportunityWindowRow(
                    prediction_id=pid,
                    production_profile=w.production_profile,
                    production_hours=w.production_hours,
                    estimated_publication_time=w.earliest_publication,
                    opportunity_status=w.status.value,
                    probability_elevated=w.probability_elevated_at_publication,
                    estimated_persistence=w.uncertainty,
                    uncertainty_metadata={
                        "interval": list(w.probability_interval)
                        if w.probability_interval
                        else None,
                        "evidence": list(w.evidence),
                        "horizon_covered": w.horizon_covered,
                    },
                )
            )
        self._s.flush()
        return True

    # ------------------------------------------------------------------ reads
    def get(self, prediction_id: str) -> ForecastPredictionRow | None:
        try:
            pid = uuid.UUID(prediction_id)
        except ValueError:
            return None
        return self._s.get(ForecastPredictionRow, pid)

    def latest(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        limit: int = 50,
        include_synthetic: bool = False,
        statuses: Sequence[str] | None = None,
    ) -> list[ForecastPredictionRow]:
        p = ForecastPredictionRow
        stmt = sa.select(p)
        if query:
            stmt = stmt.where(sa.func.lower(p.output["query"].astext) == query.lower())
        if category:
            stmt = stmt.where(p.output["category"].astext == category)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(p.is_synthetic))
        if statuses:
            stmt = stmt.where(p.forecast_status.in_(list(statuses)))
        return list(self._s.execute(stmt.order_by(p.created_at.desc()).limit(limit)).scalars())

    def peaks(
        self, *, limit: int = 50, include_synthetic: bool = False
    ) -> list[tuple[PeakEstimateRow, ForecastPredictionRow]]:
        stmt = sa.select(PeakEstimateRow, ForecastPredictionRow).join(
            ForecastPredictionRow, ForecastPredictionRow.id == PeakEstimateRow.prediction_id
        )
        if not include_synthetic:
            stmt = stmt.where(sa.not_(ForecastPredictionRow.is_synthetic))
        rows = self._s.execute(
            stmt.order_by(ForecastPredictionRow.created_at.desc()).limit(limit)
        ).all()
        return [(r[0], r[1]) for r in rows]

    def windows(
        self, *, limit: int = 50, include_synthetic: bool = False, status: str | None = None
    ) -> list[tuple[OpportunityWindowRow, ForecastPredictionRow]]:
        stmt = sa.select(OpportunityWindowRow, ForecastPredictionRow).join(
            ForecastPredictionRow, ForecastPredictionRow.id == OpportunityWindowRow.prediction_id
        )
        if not include_synthetic:
            stmt = stmt.where(sa.not_(ForecastPredictionRow.is_synthetic))
        if status:
            stmt = stmt.where(OpportunityWindowRow.opportunity_status == status)
        rows = self._s.execute(
            stmt.order_by(ForecastPredictionRow.created_at.desc()).limit(limit)
        ).all()
        return [(r[0], r[1]) for r in rows]

    def recent_features(self, model_id: str, since: datetime) -> list[dict[str, Any]]:
        p = ForecastPredictionRow
        return [
            r[0]
            for r in self._s.execute(
                sa.select(p.features).where(
                    sa.or_(p.breakout_model_id == model_id, p.trajectory_model_id == model_id),
                    p.created_at >= since,
                )
            ).all()
        ]

    def for_maturity(self, model_id: str, before: datetime) -> list[ForecastPredictionRow]:
        """Predictions whose horizon has ended (labels can be computed now)."""
        p = ForecastPredictionRow
        return list(
            self._s.execute(
                sa.select(p).where(
                    p.breakout_model_id == model_id,
                    p.breakout_probability.is_not(None),
                    p.prediction_timestamp + sa.func.make_interval(0, 0, 0, 0, p.horizon_hours)
                    <= before,
                )
            ).scalars()
        )

    # -------------------------------------------------------------- retention
    def count_expired(self, now: datetime) -> dict[str, int]:
        n = self._s.execute(
            sa.select(sa.func.count())
            .select_from(ForecastPredictionRow)
            .where(ForecastPredictionRow.expires_at <= now)
        ).scalar_one()
        return {"forecast_predictions": int(n)}

    def purge_expired(self, now: datetime) -> dict[str, int]:
        res = self._s.execute(
            sa.delete(ForecastPredictionRow).where(ForecastPredictionRow.expires_at <= now)
        )
        return {"forecast_predictions": int(getattr(res, "rowcount", 0) or 0)}
