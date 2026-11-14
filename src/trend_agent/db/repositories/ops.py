"""Repositories for the Phase 8 operations and evaluation tables.

Times passed in are application-clock times (UTC). Leases use the *database* clock
(``clock_timestamp()``) so that two hosts with skewed clocks cannot both believe they hold
the same lease.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trend_agent.db.ops_models import (
    DeadLetterRow,
    EvaluationLabelRow,
    ExperimentRow,
    OpsAlertRow,
    ProviderCheckRow,
    RecommendationOutcomeRow,
    ScheduledRunRow,
    SchedulerLeaseRow,
    ServiceHeartbeatRow,
    SystemStateRow,
)
from trend_agent.logging import redact_text

MAX_ERROR_CHARS = 4000


def _clip(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    text = redact_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------- heartbeats
class HeartbeatRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def beat(
        self,
        *,
        service_id: str,
        role: str,
        hostname: str,
        pid: int,
        version: str,
        started_at: datetime,
        now: datetime,
        interval_seconds: float,
        status: str = "running",
        details: dict[str, Any] | None = None,
    ) -> None:
        values = {
            "service_id": service_id,
            "role": role,
            "hostname": hostname[:128],
            "pid": pid,
            "version": version[:32],
            "status": status,
            "started_at": started_at,
            "last_seen_at": now,
            "interval_seconds": interval_seconds,
            "details": details or {},
        }
        stmt = insert(ServiceHeartbeatRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["service_id"],
            set_={k: stmt.excluded[k] for k in values if k not in ("service_id", "started_at")},
        )
        self.s.execute(stmt)

    def all(self, role: str | None = None) -> list[ServiceHeartbeatRow]:
        q = sa.select(ServiceHeartbeatRow).order_by(
            ServiceHeartbeatRow.role, ServiceHeartbeatRow.service_id
        )
        if role is not None:
            q = q.where(ServiceHeartbeatRow.role == role)
        return list(self.s.scalars(q))

    def live(self, role: str, now: datetime, *, grace: float = 3.0) -> list[ServiceHeartbeatRow]:
        """Running services of ``role`` seen within ``grace`` x their beat interval."""
        return [
            r
            for r in self.all(role)
            if r.status == "running"
            and (now - r.last_seen_at).total_seconds() <= grace * r.interval_seconds
        ]

    def prune(self, before: datetime) -> int:
        res = self.s.execute(
            sa.delete(ServiceHeartbeatRow).where(ServiceHeartbeatRow.last_seen_at < before)
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- leases
@dataclass(frozen=True)
class Lease:
    name: str
    owner: str
    token: int
    expires_at: datetime


class LeaseRepository:
    """Expiring leader lease with a fencing token (database clock)."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def acquire(self, name: str, owner: str, ttl: timedelta) -> Lease | None:
        """Take or renew the lease. Returns None while another owner holds an unexpired one.

        Renewal by the current owner keeps its token; a takeover increments it, so work
        stamped with an old token can be recognised as stale.
        """
        row = self.s.execute(
            sa.text(
                """
                INSERT INTO scheduler_leases
                    (name, owner, fencing_token, acquired_at, renewed_at, expires_at)
                VALUES (:name, :owner, 1, clock_timestamp(), clock_timestamp(),
                        clock_timestamp() + make_interval(secs => :ttl))
                ON CONFLICT (name) DO UPDATE SET
                    fencing_token = CASE WHEN scheduler_leases.owner = EXCLUDED.owner
                                         THEN scheduler_leases.fencing_token
                                         ELSE scheduler_leases.fencing_token + 1 END,
                    acquired_at = CASE WHEN scheduler_leases.owner = EXCLUDED.owner
                                       THEN scheduler_leases.acquired_at
                                       ELSE clock_timestamp() END,
                    owner = EXCLUDED.owner,
                    renewed_at = clock_timestamp(),
                    expires_at = EXCLUDED.expires_at
                WHERE scheduler_leases.owner = EXCLUDED.owner
                   OR scheduler_leases.expires_at < clock_timestamp()
                RETURNING name, owner, fencing_token, expires_at
                """
            ),
            {"name": name, "owner": owner, "ttl": ttl.total_seconds()},
        ).one_or_none()
        if row is None:
            return None
        return Lease(row.name, row.owner, int(row.fencing_token), row.expires_at)

    def release(self, name: str, owner: str) -> bool:
        res = self.s.execute(
            sa.update(SchedulerLeaseRow)
            .where(SchedulerLeaseRow.name == name, SchedulerLeaseRow.owner == owner)
            .values(expires_at=sa.func.clock_timestamp())
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]

    def holds(self, name: str, owner: str, token: int) -> bool:
        """True while ``owner`` still holds the lease with ``token`` (fencing check)."""
        row = self.s.execute(
            sa.text(
                "SELECT 1 FROM scheduler_leases WHERE name = :n AND owner = :o "
                "AND fencing_token = :t AND expires_at >= clock_timestamp()"
            ),
            {"n": name, "o": owner, "t": token},
        ).one_or_none()
        return row is not None

    def get(self, name: str) -> SchedulerLeaseRow | None:
        return self.s.get(SchedulerLeaseRow, name)

    def all(self) -> list[SchedulerLeaseRow]:
        return list(self.s.scalars(sa.select(SchedulerLeaseRow).order_by(SchedulerLeaseRow.name)))

    def db_now(self) -> datetime:
        return self.s.execute(sa.text("SELECT clock_timestamp()")).scalar_one()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------- scheduled runs
class ScheduledRunRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def start(
        self,
        *,
        task: str,
        owner: str,
        token: int,
        correlation_id: str,
        mode: str,
        now: datetime,
    ) -> int:
        row = ScheduledRunRow(
            task=task,
            owner=owner,
            fencing_token=token,
            correlation_id=correlation_id,
            operating_mode=mode,
            status="running",
            started_at=now,
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        now: datetime,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        row = self.s.get(ScheduledRunRow, run_id)
        if row is None:
            return
        row.status = status
        row.finished_at = now
        row.duration_ms = int((now - row.started_at).total_seconds() * 1000)
        row.summary = summary or {}
        row.error = _clip(error) if error else None

    def skipped(
        self,
        *,
        task: str,
        owner: str,
        token: int,
        correlation_id: str,
        mode: str,
        now: datetime,
        reason: str,
    ) -> None:
        self.s.add(
            ScheduledRunRow(
                task=task,
                owner=owner,
                fencing_token=token,
                correlation_id=correlation_id,
                operating_mode=mode,
                status="skipped",
                started_at=now,
                finished_at=now,
                duration_ms=0,
                summary={"reason": reason},
            )
        )

    def last(self, task: str, statuses: Sequence[str] | None = None) -> ScheduledRunRow | None:
        q = sa.select(ScheduledRunRow).where(ScheduledRunRow.task == task)
        if statuses:
            q = q.where(ScheduledRunRow.status.in_(statuses))
        return self.s.scalars(q.order_by(ScheduledRunRow.started_at.desc()).limit(1)).first()

    def recent(self, limit: int = 50, task: str | None = None) -> list[ScheduledRunRow]:
        q = sa.select(ScheduledRunRow)
        if task:
            q = q.where(ScheduledRunRow.task == task)
        return list(self.s.scalars(q.order_by(ScheduledRunRow.started_at.desc()).limit(limit)))

    def consecutive_failures(self, task: str, limit: int = 20) -> int:
        n = 0
        for r in self.recent(limit, task):
            if r.status == "failed":
                n += 1
            elif r.status in ("succeeded", "skipped"):
                break
        return n

    def abandon_stale(self, before: datetime, now: datetime) -> int:
        """Close runs left 'running' by a crashed scheduler."""
        res = self.s.execute(
            sa.update(ScheduledRunRow)
            .where(ScheduledRunRow.status == "running", ScheduledRunRow.started_at < before)
            .values(status="failed", finished_at=now, error="abandoned: scheduler stopped")
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

    def stats(self, since: datetime) -> dict[str, dict[str, int]]:
        rows = self.s.execute(
            sa.select(ScheduledRunRow.task, ScheduledRunRow.status, sa.func.count())
            .where(ScheduledRunRow.started_at >= since)
            .group_by(ScheduledRunRow.task, ScheduledRunRow.status)
        ).all()
        out: dict[str, dict[str, int]] = {}
        for task, status, n in rows:
            out.setdefault(task, {})[status] = int(n)
        return out


# ---------------------------------------------------------------------- ops alerts
@dataclass(frozen=True)
class AlertDecision:
    alert_id: int
    notify: bool
    new: bool
    occurrences: int


class OpsAlertRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def raise_alert(
        self,
        *,
        fingerprint: str,
        kind: str,
        severity: str,
        title: str,
        detail: str,
        now: datetime,
        cooldown: timedelta,
        details: dict[str, Any] | None = None,
    ) -> AlertDecision:
        """Record an occurrence; notify only on first sight, on reopening, or after the
        cooldown. A recurring error therefore produces one notification per cooldown."""
        row = self.s.scalars(
            sa.select(OpsAlertRow).where(OpsAlertRow.fingerprint == fingerprint).with_for_update()
        ).first()
        if row is None:
            row = OpsAlertRow(
                fingerprint=fingerprint[:200],
                kind=kind,
                severity=severity,
                title=_clip(title, 300),
                detail=_clip(detail),
                status="open",
                occurrences=1,
                first_seen_at=now,
                last_seen_at=now,
                last_notified_at=now,
                notifications_sent=1,
                details=details or {},
            )
            self.s.add(row)
            self.s.flush()
            return AlertDecision(row.id, True, True, 1)
        reopened = row.status == "resolved"
        row.occurrences += 1
        row.last_seen_at = now
        row.detail = _clip(detail)
        row.severity = severity
        row.details = details or row.details
        if reopened:
            row.status = "open"
            row.resolved_at = None
        due = row.last_notified_at is None or now - row.last_notified_at >= cooldown
        notify = reopened or due
        if notify:
            row.last_notified_at = now
            row.notifications_sent += 1
        return AlertDecision(row.id, notify, False, row.occurrences)

    def resolve(self, fingerprint: str, now: datetime) -> bool:
        res = self.s.execute(
            sa.update(OpsAlertRow)
            .where(OpsAlertRow.fingerprint == fingerprint, OpsAlertRow.status == "open")
            .values(status="resolved", resolved_at=now)
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]

    def open(self) -> list[OpsAlertRow]:
        return list(
            self.s.scalars(
                sa.select(OpsAlertRow)
                .where(OpsAlertRow.status == "open")
                .order_by(OpsAlertRow.last_seen_at.desc())
            )
        )

    def recent(self, limit: int = 100) -> list[OpsAlertRow]:
        return list(
            self.s.scalars(
                sa.select(OpsAlertRow).order_by(OpsAlertRow.last_seen_at.desc()).limit(limit)
            )
        )

    def open_fingerprints(self, prefix: str) -> list[str]:
        return list(
            self.s.scalars(
                sa.select(OpsAlertRow.fingerprint).where(
                    OpsAlertRow.status == "open", OpsAlertRow.fingerprint.startswith(prefix)
                )
            )
        )


# ---------------------------------------------------------------------- dead letters
class DeadLetterRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def record(
        self,
        *,
        source: str,
        reference: str,
        error_type: str,
        error: str,
        now: datetime,
        payload: dict[str, Any] | None = None,
    ) -> None:
        values = {
            "source": source[:48],
            "reference": reference[:200],
            "error_type": error_type[:64],
            "error": _clip(error),
            "payload": _redact_payload(payload or {}),
            "attempts": 1,
            "last_failed_at": now,
        }
        stmt = insert(DeadLetterRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_dead_letters_ref",
            set_={
                "attempts": DeadLetterRow.attempts + 1,
                "error_type": stmt.excluded.error_type,
                "error": stmt.excluded.error,
                "payload": stmt.excluded.payload,
                "last_failed_at": stmt.excluded.last_failed_at,
                "resolved_at": None,
                "resolution": None,
            },
        )
        self.s.execute(stmt)

    def open(self, limit: int = 100, source: str | None = None) -> list[DeadLetterRow]:
        q = sa.select(DeadLetterRow).where(DeadLetterRow.resolved_at.is_(None))
        if source:
            q = q.where(DeadLetterRow.source == source)
        return list(self.s.scalars(q.order_by(DeadLetterRow.last_failed_at.desc()).limit(limit)))

    def resolve(self, dead_letter_id: int, resolution: str, now: datetime) -> bool:
        row = self.s.get(DeadLetterRow, dead_letter_id)
        if row is None or row.resolved_at is not None:
            return False
        row.resolved_at = now
        row.resolution = _clip(resolution, 500)
        self.s.flush()
        return True

    def count_open(self) -> dict[str, int]:
        rows = self.s.execute(
            sa.select(DeadLetterRow.source, sa.func.count())
            .where(DeadLetterRow.resolved_at.is_(None))
            .group_by(DeadLetterRow.source)
        ).all()
        return {s: int(n) for s, n in rows}


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in list(payload.items())[:50]:
        if isinstance(v, str):
            out[k] = _clip(v, 1000)
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = _clip(str(v), 1000)
    return out


# ---------------------------------------------------------------------- provider checks
class ProviderCheckRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def add(
        self,
        *,
        provider_name: str,
        now: datetime,
        ok: bool,
        status: str,
        detail: str,
        latency_ms: int | None,
        units_spent: int,
        signal_classes: Sequence[str],
    ) -> None:
        self.s.add(
            ProviderCheckRow(
                provider_name=provider_name,
                checked_at=now,
                ok=ok,
                status=status,
                latency_ms=latency_ms,
                units_spent=units_spent,
                signal_classes=list(signal_classes),
                detail=_clip(detail, 1000),
            )
        )

    def latest(self) -> dict[str, ProviderCheckRow]:
        sub = (
            sa.select(ProviderCheckRow)
            .distinct(ProviderCheckRow.provider_name)
            .order_by(ProviderCheckRow.provider_name, ProviderCheckRow.checked_at.desc())
        )
        return {r.provider_name: r for r in self.s.scalars(sub)}

    def last_success(self) -> dict[str, datetime]:
        rows = self.s.execute(
            sa.select(ProviderCheckRow.provider_name, sa.func.max(ProviderCheckRow.checked_at))
            .where(ProviderCheckRow.ok)
            .group_by(ProviderCheckRow.provider_name)
        ).all()
        return {str(n): t for n, t in rows}


# ---------------------------------------------------------------------- system state
class SystemStateRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.s.get(SystemStateRow, key)
        return dict(row.value) if row is not None else None

    def put(self, key: str, value: dict[str, Any], now: datetime) -> None:
        stmt = insert(SystemStateRow).values(key=key, value=value, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": now}
        )
        self.s.execute(stmt)


# ---------------------------------------------------------------------- labels
class LabelRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def upsert(
        self,
        *,
        origin: str,
        kind: str,
        subject_key: str,
        event_time: datetime,
        known_at: datetime,
        label: dict[str, Any],
        source: str,
        labeler: str,
        notes: str | None = None,
    ) -> bool:
        """Insert a label; returns False when an identical subject/time already exists."""
        stmt = (
            insert(EvaluationLabelRow)
            .values(
                origin=origin,
                kind=kind,
                subject_key=subject_key[:200],
                event_time=event_time,
                known_at=known_at,
                label=label,
                source=source[:200],
                labeler=labeler[:64],
                notes=notes,
            )
            .on_conflict_do_nothing(constraint="uq_evaluation_labels_subject")
        )
        res = self.s.execute(stmt)
        return bool(res.rowcount)  # type: ignore[attr-defined]

    def counts(self) -> dict[str, dict[str, int]]:
        rows = self.s.execute(
            sa.select(EvaluationLabelRow.origin, EvaluationLabelRow.kind, sa.func.count()).group_by(
                EvaluationLabelRow.origin, EvaluationLabelRow.kind
            )
        ).all()
        out: dict[str, dict[str, int]] = {"real": {}, "synthetic": {}}
        for origin, kind, n in rows:
            out.setdefault(origin, {})[kind] = int(n)
        return out

    def list(
        self, *, origin: str, kind: str, known_before: datetime | None = None
    ) -> list[EvaluationLabelRow]:
        q = sa.select(EvaluationLabelRow).where(
            EvaluationLabelRow.origin == origin, EvaluationLabelRow.kind == kind
        )
        if known_before is not None:
            q = q.where(EvaluationLabelRow.known_at <= known_before)
        return list(self.s.scalars(q.order_by(EvaluationLabelRow.event_time)))


# ---------------------------------------------------------------------- experiments
class ExperimentRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def add(
        self,
        *,
        name: str,
        component: str,
        hypothesis: str,
        config: dict[str, Any],
        dataset: dict[str, Any],
        data_origin: str,
        split: str,
        metrics: dict[str, Any],
        baseline_metrics: dict[str, Any],
        decision: str,
        decision_reason: str,
        code_version: str,
    ) -> uuid.UUID:
        exp_id = uuid.uuid4()
        self.s.add(
            ExperimentRow(
                id=exp_id,
                name=name[:120],
                component=component,
                hypothesis=hypothesis,
                config=config,
                dataset=dataset,
                data_origin=data_origin,
                split=split,
                metrics=metrics,
                baseline_metrics=baseline_metrics,
                decision=decision,
                decision_reason=decision_reason,
                code_version=code_version[:64],
            )
        )
        self.s.flush()
        return exp_id

    def list(self, component: str | None = None, limit: int = 100) -> list[ExperimentRow]:
        q = sa.select(ExperimentRow)
        if component:
            q = q.where(ExperimentRow.component == component)
        return list(self.s.scalars(q.order_by(ExperimentRow.created_at.desc()).limit(limit)))


# ---------------------------------------------------------------------- outcomes
class OutcomeRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def record(self, *, user_key: str, recommendation_id: uuid.UUID, **fields: Any) -> int:
        values = {"user_key": user_key, "recommendation_id": recommendation_id, **fields}
        ins = insert(RecommendationOutcomeRow).values(**values)
        stmt = ins.on_conflict_do_update(
            constraint="uq_recommendation_outcomes_day",
            set_={k: ins.excluded[k] for k in fields},
        ).returning(RecommendationOutcomeRow.id)
        return int(self.s.execute(stmt).scalar_one())

    def for_user(self, user_key: str) -> list[RecommendationOutcomeRow]:
        return list(
            self.s.scalars(
                sa.select(RecommendationOutcomeRow)
                .where(RecommendationOutcomeRow.user_key == user_key)
                .order_by(RecommendationOutcomeRow.recorded_at.desc())
            )
        )

    def all(self) -> list[RecommendationOutcomeRow]:
        return list(self.s.scalars(sa.select(RecommendationOutcomeRow)))
