"""Detection runs, anomaly results, calibrations, evaluations and the detection outbox."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.detection_models import (
    AnomalyResultRow,
    DetectionCalibrationRow,
    DetectionEvaluationRow,
    DetectionQueueRow,
    DetectionRunRow,
)
from trend_agent.schemas.detection import DetectorType, RunStatus, RunType

DEFAULT_CHANNEL = "trend_agent_detection"


class DetectionRunRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def start(
        self,
        *,
        run_type: RunType,
        scope: dict[str, Any],
        now: datetime,
        as_of: datetime,
        algorithm_version: str,
        configuration_version: str,
        threshold_version: str,
        is_synthetic: bool = False,
    ) -> uuid.UUID:
        row = DetectionRunRow(
            id=uuid.uuid4(),
            run_type=run_type,
            status=RunStatus.RUNNING,
            scope=scope,
            started_at=now,
            as_of=as_of,
            algorithm_version=algorithm_version,
            configuration_version=configuration_version,
            threshold_version=threshold_version,
            is_synthetic=is_synthetic,
        )
        self._s.add(row)
        self._s.flush()
        return row.id

    def finish(self, run_id: uuid.UUID, values: dict[str, Any]) -> None:
        self._s.execute(
            sa.update(DetectionRunRow).where(DetectionRunRow.id == run_id).values(**values)
        )

    def get(self, run_id: uuid.UUID) -> DetectionRunRow | None:
        return self._s.get(DetectionRunRow, run_id)

    def recent(self, limit: int = 20, *, include_synthetic: bool = True) -> list[DetectionRunRow]:
        stmt = sa.select(DetectionRunRow)
        if not include_synthetic:
            stmt = stmt.where(DetectionRunRow.is_synthetic.is_(False))
        return list(
            self._s.execute(stmt.order_by(DetectionRunRow.started_at.desc()).limit(limit)).scalars()
        )

    # ------------------------------------------------------------- anomaly results
    def insert_results(self, rows: Sequence[dict[str, Any]]) -> dict[tuple[int, str], int]:
        """Insert results idempotently; returns (observation_id, detector) -> result id.

        Re-running detection on an unchanged observation with the same algorithm and
        configuration version hits the unique constraint and returns the existing ids.
        """
        if not rows:
            return {}
        t = AnomalyResultRow
        self._s.execute(
            pg_insert(t)
            .values(list(rows))
            .on_conflict_do_nothing(constraint="uq_anomaly_results_identity")
        )
        keys = {(int(r["observation_id"]), str(r["detector_type"])) for r in rows}
        version = rows[0]["configuration_version"]
        algorithm = rows[0]["algorithm_version"]
        found = self._s.execute(
            sa.select(t.id, t.observation_id, t.detector_type).where(
                t.observation_id.in_({k[0] for k in keys}),
                t.configuration_version == version,
                t.algorithm_version == algorithm,
            )
        ).all()
        return {(int(r[1]), r[2].value): int(r[0]) for r in found}

    def results(
        self,
        *,
        target_ids: Iterable[int] | None = None,
        series_id: int | None = None,
        limit: int = 200,
        triggered_only: bool = False,
    ) -> list[AnomalyResultRow]:
        t = AnomalyResultRow
        stmt = sa.select(t)
        if target_ids is not None:
            stmt = stmt.where(t.target_id.in_(list(target_ids)))
        if series_id is not None:
            stmt = stmt.where(t.series_id == series_id)
        if triggered_only:
            stmt = stmt.where(t.result_status.in_(["triggered", "watch", "rejected"]))
        return list(
            self._s.execute(
                stmt.order_by(t.period_start.desc(), t.detector_type).limit(limit)
            ).scalars()
        )

    def result_counts(self, since: datetime | None = None) -> dict[str, dict[str, int]]:
        t = AnomalyResultRow
        stmt = sa.select(t.detector_type, t.result_status, sa.func.count()).group_by(
            t.detector_type, t.result_status
        )
        if since is not None:
            stmt = stmt.where(t.evaluated_at >= since)
        out: dict[str, dict[str, int]] = {}
        for det, status, n in self._s.execute(stmt).all():
            out.setdefault(det.value, {})[status.value] = int(n)
        return out

    def count_expired(self, now: datetime) -> int:
        t = AnomalyResultRow
        return int(
            self._s.execute(
                sa.select(sa.func.count()).select_from(t).where(t.expires_at <= now)
            ).scalar_one()
        )

    def purge_expired(self, now: datetime) -> int:
        result = self._s.execute(
            sa.delete(AnomalyResultRow).where(AnomalyResultRow.expires_at <= now)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------- calibrations
    def save_calibration(
        self,
        *,
        version: str,
        stratum: str,
        source: str,
        dataset: str,
        method: str,
        parameters: dict[str, Any],
        train_start: datetime | None,
        train_end: datetime | None,
    ) -> None:
        t = DetectionCalibrationRow
        self._s.execute(
            sa.update(t).where(t.stratum_key == stratum, t.source == source).values(active=False)
        )
        stmt = pg_insert(t).values(
            calibration_version=version,
            stratum_key=stratum,
            source=source,
            dataset=dataset,
            method=method,
            parameters=parameters,
            train_start=train_start,
            train_end=train_end,
            active=True,
        )
        self._s.execute(
            stmt.on_conflict_do_update(
                constraint="uq_detection_calibrations_stratum",
                set_={
                    "parameters": stmt.excluded.parameters,
                    "dataset": stmt.excluded.dataset,
                    "method": stmt.excluded.method,
                    "train_start": stmt.excluded.train_start,
                    "train_end": stmt.excluded.train_end,
                    "active": True,
                },
            )
        )

    def active_calibrations(self) -> list[DetectionCalibrationRow]:
        t = DetectionCalibrationRow
        return list(
            self._s.execute(
                sa.select(t).where(t.active.is_(True)).order_by(t.stratum_key)
            ).scalars()
        )

    # ------------------------------------------------------------- evaluations
    def save_evaluation(self, values: dict[str, Any]) -> uuid.UUID:
        row = DetectionEvaluationRow(id=uuid.uuid4(), **values)
        self._s.add(row)
        self._s.flush()
        return row.id

    def evaluations(self, limit: int = 20) -> list[DetectionEvaluationRow]:
        t = DetectionEvaluationRow
        return list(
            self._s.execute(sa.select(t).order_by(t.started_at.desc()).limit(limit)).scalars()
        )


@dataclass(frozen=True)
class QueuedSeries:
    series_id: int
    target_id: int | None
    marks: int
    last_marked_at: datetime
    earliest_period_start: datetime | None


class DetectionQueueRepository:
    """Transactional outbox: written in the same transaction as new observations."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def mark(
        self,
        entries: Iterable[tuple[int, int | None, datetime | None]],
        now: datetime,
        *,
        channel: str | None = DEFAULT_CHANNEL,
    ) -> int:
        """Queue series (id, target id, earliest changed period) and NOTIFY listeners."""
        t = DetectionQueueRow
        merged: dict[int, tuple[int | None, datetime | None]] = {}
        for sid, tid, period in entries:
            held = merged.get(sid)
            earliest = (
                period if held is None or held[1] is None else min(held[1], period or held[1])
            )
            merged[sid] = (tid, earliest)
        for sid, (tid, period) in merged.items():
            stmt = pg_insert(t).values(
                series_id=sid,
                target_id=tid,
                first_marked_at=now,
                last_marked_at=now,
                marks=1,
                earliest_period_start=period,
            )
            self._s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[t.series_id],
                    set_={
                        "last_marked_at": now,
                        "marks": t.marks + 1,
                        "earliest_period_start": sa.func.least(
                            t.earliest_period_start, stmt.excluded.earliest_period_start
                        ),
                    },
                )
            )
        if merged and channel:
            # Delivered to LISTENing detection workers when this transaction commits.
            self._s.execute(
                sa.text("SELECT pg_notify(:channel, :payload)"),
                {"channel": channel, "payload": ",".join(str(s) for s in sorted(merged))[:7000]},
            )
        return len(merged)

    def claim(self, worker: str, now: datetime, lease: timedelta, limit: int) -> list[QueuedSeries]:
        t = DetectionQueueRow
        sub = (
            sa.select(t.series_id)
            .where(sa.or_(t.lease_expires_at.is_(None), t.lease_expires_at < now))
            .order_by(t.last_marked_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        rows = self._s.execute(
            sa.update(t)
            .where(t.series_id.in_(sub))
            .values(claimed_by=worker, lease_expires_at=now + lease)
            .returning(t.series_id, t.target_id, t.marks, t.last_marked_at, t.earliest_period_start)
        ).all()
        return [QueuedSeries(int(r[0]), r[1], int(r[2]), r[3], r[4]) for r in rows]

    def done(self, series_id: int, marked_until: datetime) -> None:
        """Remove the entry unless it was marked again after processing started."""
        t = DetectionQueueRow
        self._s.execute(
            sa.delete(t).where(t.series_id == series_id, t.last_marked_at <= marked_until)
        )
        self._s.execute(
            sa.update(t)
            .where(t.series_id == series_id)
            .values(claimed_by=None, lease_expires_at=None)
        )

    def release(self, series_id: int) -> None:
        t = DetectionQueueRow
        self._s.execute(
            sa.update(t)
            .where(t.series_id == series_id)
            .values(claimed_by=None, lease_expires_at=None)
        )

    def pending(self) -> list[QueuedSeries]:
        t = DetectionQueueRow
        rows = self._s.execute(sa.select(t).order_by(t.last_marked_at)).scalars()
        return [
            QueuedSeries(
                r.series_id, r.target_id, r.marks, r.last_marked_at, r.earliest_period_start
            )
            for r in rows
        ]

    def depth(self) -> int:
        return int(
            self._s.execute(sa.select(sa.func.count()).select_from(DetectionQueueRow)).scalar_one()
        )


def result_row(
    base: dict[str, Any],
    *,
    detector: DetectorType,
    score: float | None,
    threshold: float | None,
    p_value: float | None,
    status: str,
    reasons: list[str],
    details: dict[str, Any],
) -> dict[str, Any]:
    """One ``anomaly_results`` row: ``base`` holds the observation/run fields shared by
    every detector of one evaluation (see ``DetectionService._result_rows``)."""
    return {
        "run_id": base["run_id"],
        "target_id": base["target_id"],
        "series_id": base["series_id"],
        "observation_id": base["observation_id"],
        "observation_revision": base["revision"],
        "period_start": base["period_start"],
        "period_end": base["period_end"],
        "detector_type": detector.value,
        "baseline_model": base["baseline_model"],
        "baseline_value": base["baseline_value"],
        "baseline_dispersion": base["baseline_dispersion"],
        "observed_value": base["observed"],
        "anomaly_score": score,
        "threshold": threshold,
        "p_value": p_value,
        "evidence_quality": base["quality"],
        "result_status": status,
        "support_status": base["support"],
        "reasons": reasons,
        "details": details,
        "available_at": base["available_at"],
        "evaluated_at": base["evaluated_at"],
        "is_late": base["is_late"],
        "algorithm_version": base["algorithm_version"],
        "configuration_version": base["configuration_version"],
        "is_synthetic": base["is_synthetic"],
        "expires_at": base["expires_at"],
    }
