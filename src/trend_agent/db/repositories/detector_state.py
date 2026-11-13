"""Per-series detector state (CUSUM, change point, baseline cache, watermark).

Concurrency: a detection worker takes a transaction-scoped advisory lock on the series
(``try_lock_series``) and saves state with optimistic version checks; a worker that lost
the race skips the series (it will be processed again from the queue / next run).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.detection_models import DetectorStateRow
from trend_agent.schemas.detection import DetectorType
from trend_agent.schemas.monitoring import MeasurementCategory

LOCK_NAMESPACE = 0x7D3E  # advisory-lock class id for detection (two-key form)


@dataclass(frozen=True)
class StoredState:
    payloads: dict[DetectorType, dict[str, Any]]
    version: int
    updated_at: datetime | None


class DetectorStateRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def try_lock_series(self, series_id: int) -> bool:
        """Transaction-scoped advisory lock; False if another worker holds it."""
        return bool(
            self._s.execute(
                sa.text("SELECT pg_try_advisory_xact_lock(:ns, :sid)"),
                {"ns": LOCK_NAMESPACE, "sid": int(series_id) % 2_147_483_647},
            ).scalar_one()
        )

    def load(self, series_id: int) -> StoredState | None:
        rows = (
            self._s.execute(
                sa.select(DetectorStateRow).where(DetectorStateRow.series_id == series_id)
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        payloads = {r.detector_type: r.state_payload for r in rows}
        if DetectorType.ENGINE not in payloads:
            return None
        engine = next(r for r in rows if r.detector_type is DetectorType.ENGINE)
        return StoredState(payloads, engine.version, engine.updated_at)

    def save(
        self,
        *,
        series_id: int,
        target_id: int | None,
        provider: str,
        measurement: MeasurementCategory,
        normalization_key: str,
        payloads: dict[DetectorType, dict[str, Any]],
        expected_version: int | None,
        last_observation_id: int | None,
        last_period_end: datetime | None,
        watermark: int,
        algorithm_version: str,
        configuration_version: str,
        is_synthetic: bool,
        now: datetime,
    ) -> bool:
        """Write all payloads; False if the stored version changed since it was read."""
        t = DetectorStateRow
        current = self._s.execute(
            sa.select(t.version)
            .where(t.series_id == series_id, t.detector_type == DetectorType.ENGINE)
            .with_for_update()
        ).scalar_one_or_none()
        if current is not None and expected_version is not None and current != expected_version:
            return False
        if current is not None and expected_version is None:
            return False
        new_version = (current or 0) + 1
        for detector, payload in payloads.items():
            values = {
                "series_id": series_id,
                "target_id": target_id,
                "provider_name": provider,
                "measurement_category": measurement,
                "normalization_key": normalization_key,
                "detector_type": detector,
                "state_payload": payload,
                "last_processed_observation_id": last_observation_id,
                "last_processed_period_end": last_period_end,
                "watermark_observation_id": watermark,
                "algorithm_version": algorithm_version,
                "configuration_version": configuration_version,
                "version": new_version,
                "is_synthetic": is_synthetic,
                "updated_at": now,
            }
            stmt = pg_insert(t).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[t.series_id, t.detector_type],
                set_={
                    k: stmt.excluded[k] for k in values if k not in ("series_id", "detector_type")
                },
            )
            self._s.execute(stmt)
        return True

    def summaries(self, series_ids: list[int] | None = None) -> list[DetectorStateRow]:
        stmt = sa.select(DetectorStateRow).where(
            DetectorStateRow.detector_type == DetectorType.ENGINE
        )
        if series_ids is not None:
            stmt = stmt.where(DetectorStateRow.series_id.in_(series_ids))
        return list(self._s.execute(stmt.order_by(DetectorStateRow.series_id)).scalars())

    def count(self) -> int:
        return int(
            self._s.execute(
                sa.select(sa.func.count())
                .select_from(DetectorStateRow)
                .where(DetectorStateRow.detector_type == DetectorType.ENGINE)
            ).scalar_one()
        )
