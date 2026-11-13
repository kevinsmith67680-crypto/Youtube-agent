"""Durable monitoring job queue in PostgreSQL.

* Claiming uses ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT n)`` so
  concurrent workers never claim the same job; each claim gets a fresh ``lease_token``.
* Every state change after the claim is *fenced* on that token: a worker whose lease
  expired (and whose job was reclaimed) cannot overwrite the new owner's state.
* Leases give at-least-once execution; idempotency lives in the observation write layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.monitoring_models import MonitoringJobAttemptRow, MonitoringJobRow
from trend_agent.schemas.monitoring import (
    CLAIMABLE_JOB_STATUSES,
    LEASED_JOB_STATUSES,
    ErrorCategory,
    JobStatus,
)

OPEN_STATUSES = tuple(s.value for s in (*CLAIMABLE_JOB_STATUSES, *LEASED_JOB_STATUSES))


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ creation
    def enqueue(self, values: dict[str, Any]) -> uuid.UUID | None:
        """Insert a job unless its idempotency key exists. Returns the new id or None."""
        stmt = (
            pg_insert(MonitoringJobRow)
            .values(id=uuid.uuid4(), status=JobStatus.PENDING, attempt_count=0, **values)
            .on_conflict_do_nothing(index_elements=[MonitoringJobRow.idempotency_key])
            .returning(MonitoringJobRow.id)
        )
        new_id: uuid.UUID | None = self._s.execute(stmt).scalar_one_or_none()
        return new_id

    def get(self, job_id: uuid.UUID) -> MonitoringJobRow | None:
        return self._s.get(MonitoringJobRow, job_id)

    def by_key(self, key: str) -> MonitoringJobRow | None:
        return self._s.execute(
            sa.select(MonitoringJobRow).where(MonitoringJobRow.idempotency_key == key)
        ).scalar_one_or_none()

    def open_job_targets(self, target_ids: Sequence[int]) -> set[int]:
        """Targets that already have a queued/running job (no duplicate planning)."""
        if not target_ids:
            return set()
        j = MonitoringJobRow
        rows = self._s.execute(
            sa.select(j.target_id).where(
                j.target_id.in_(list(target_ids)), j.status.in_(OPEN_STATUSES)
            )
        ).all()
        return {int(r[0]) for r in rows}

    # ------------------------------------------------------------------ claiming
    def claim(
        self,
        *,
        worker: str,
        now: datetime,
        lease: timedelta,
        limit: int,
        providers: Sequence[str] | None = None,
        job_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[MonitoringJobRow]:
        j = MonitoringJobRow
        due = sa.or_(j.retry_after.is_(None), j.retry_after <= now)
        candidates = (
            sa.select(j.id)
            .where(
                j.status.in_([s.value for s in CLAIMABLE_JOB_STATUSES]),
                j.scheduled_at <= now,
                due,
            )
            .order_by(j.priority.desc(), j.scheduled_at, j.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if providers is not None:
            candidates = candidates.where(j.provider_name.in_(list(providers)))
        if job_ids is not None:
            candidates = candidates.where(j.id.in_(list(job_ids)))
        stmt = (
            sa.update(j)
            .where(j.id.in_(candidates.scalar_subquery()))
            .values(
                status=JobStatus.CLAIMED,
                lease_owner=worker,
                lease_token=sa.func.gen_random_uuid(),
                lease_expires_at=now + lease,
                attempt_count=j.attempt_count + 1,
                updated_at=now,
            )
            .returning(j)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        rows = list(self._s.execute(stmt).scalars())
        rows.sort(key=lambda r: (-r.priority, r.scheduled_at, str(r.id)))
        return rows

    def _fenced(self, job_id: uuid.UUID, token: uuid.UUID) -> Any:
        j = MonitoringJobRow
        return sa.update(j).where(
            j.id == job_id,
            j.lease_token == token,
            j.status.in_([s.value for s in LEASED_JOB_STATUSES]),
        )

    def start(self, job_id: uuid.UUID, token: uuid.UUID, now: datetime) -> bool:
        result = self._s.execute(
            self._fenced(job_id, token).values(
                status=JobStatus.RUNNING, started_at=now, updated_at=now
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def renew(self, job_id: uuid.UUID, token: uuid.UUID, until: datetime) -> bool:
        result = self._s.execute(self._fenced(job_id, token).values(lease_expires_at=until))
        return bool(getattr(result, "rowcount", 0))

    def finish(
        self,
        job_id: uuid.UUID,
        token: uuid.UUID,
        *,
        status: JobStatus,
        now: datetime,
        retry_after: datetime | None = None,
        scheduled_at: datetime | None = None,
        error_category: ErrorCategory | None = None,
        error_details: str | None = None,
        deferred: bool = False,
    ) -> bool:
        """Fenced state change out of CLAIMED/RUNNING. False when the lease was lost."""
        j = MonitoringJobRow
        values: dict[str, Any] = {
            "status": status,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "retry_after": retry_after,
            "error_category": error_category,
            "error_details": (error_details or None) and error_details[:2000],
            "updated_at": now,
        }
        if status in (
            JobStatus.SUCCEEDED,
            JobStatus.NO_NEW_DATA,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            values["completed_at"] = now
        if scheduled_at is not None:
            values["scheduled_at"] = scheduled_at
        if deferred:
            # Waiting for quota/provider is not a failed attempt.
            values["attempt_count"] = sa.func.greatest(j.attempt_count - 1, 0)
            values["deferral_count"] = j.deferral_count + 1
        result = self._s.execute(self._fenced(job_id, token).values(**values))
        return bool(getattr(result, "rowcount", 0))

    def recover_expired(self, now: datetime) -> list[MonitoringJobRow]:
        """Return expired leases to the queue (or fail them after max attempts)."""
        j = MonitoringJobRow
        expired = list(
            self._s.execute(
                sa.select(j)
                .where(
                    j.status.in_([s.value for s in LEASED_JOB_STATUSES]), j.lease_expires_at < now
                )
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        for row in expired:
            exhausted = row.attempt_count >= row.max_attempts
            row.status = JobStatus.FAILED if exhausted else JobStatus.RETRY_WAIT
            row.error_category = ErrorCategory.LEASE_EXPIRED
            row.error_details = (
                f"lease held by {row.lease_owner} expired at {row.lease_expires_at}; "
                + ("max attempts reached" if exhausted else "returned to queue")
            )
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.retry_after = None if exhausted else now
            row.updated_at = now
            if exhausted:
                row.completed_at = now
            self._s.execute(
                sa.update(MonitoringJobAttemptRow)
                .where(
                    MonitoringJobAttemptRow.job_id == row.id,
                    MonitoringJobAttemptRow.finished_at.is_(None),
                )
                .values(
                    finished_at=now,
                    outcome="lease_expired",
                    error_category=ErrorCategory.LEASE_EXPIRED,
                )
            )
        self._s.flush()
        return expired

    def cancel_for_target(self, target_id: int, now: datetime, reason: str) -> int:
        j = MonitoringJobRow
        result = self._s.execute(
            sa.update(j)
            .where(
                j.target_id == target_id, j.status.in_([s.value for s in CLAIMABLE_JOB_STATUSES])
            )
            .values(
                status=JobStatus.CANCELLED, completed_at=now, updated_at=now, error_details=reason
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def reset_for_resume(self, job_ids: Sequence[uuid.UUID], now: datetime) -> int:
        """Re-queue FAILED/CANCELLED jobs (backfill resume)."""
        if not job_ids:
            return 0
        j = MonitoringJobRow
        result = self._s.execute(
            sa.update(j)
            .where(j.id.in_(list(job_ids)), j.status.in_([JobStatus.FAILED, JobStatus.CANCELLED]))
            .values(
                status=JobStatus.PENDING,
                attempt_count=0,
                retry_after=None,
                scheduled_at=now,
                completed_at=None,
                updated_at=now,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ attempts
    def add_attempt(self, values: dict[str, Any]) -> int:
        return int(
            self._s.execute(
                sa.insert(MonitoringJobAttemptRow)
                .values(**values)
                .returning(MonitoringJobAttemptRow.id)
            ).scalar_one()
        )

    def finish_attempt(self, attempt_id: int, values: dict[str, Any]) -> None:
        self._s.execute(
            sa.update(MonitoringJobAttemptRow)
            .where(MonitoringJobAttemptRow.id == attempt_id)
            .values(**values)
        )

    def attempts(self, job_id: uuid.UUID) -> list[MonitoringJobAttemptRow]:
        return list(
            self._s.execute(
                sa.select(MonitoringJobAttemptRow)
                .where(MonitoringJobAttemptRow.job_id == job_id)
                .order_by(MonitoringJobAttemptRow.attempt, MonitoringJobAttemptRow.id)
            ).scalars()
        )

    # ------------------------------------------------------------------ stats
    def counts_by_status(self) -> dict[str, int]:
        j = MonitoringJobRow
        rows = self._s.execute(sa.select(j.status, sa.func.count()).group_by(j.status)).all()
        return {r[0].value: int(r[1]) for r in rows}

    def queue_depth(self) -> int:
        j = MonitoringJobRow
        return int(
            self._s.execute(
                sa.select(sa.func.count()).where(j.status.in_(OPEN_STATUSES))
            ).scalar_one()
        )

    def pending_cost(self, provider: str, since: datetime) -> int:
        """Estimated cost of queued, not yet executed jobs for ``provider``."""
        j = MonitoringJobRow
        return int(
            self._s.execute(
                sa.select(sa.func.coalesce(sa.func.sum(j.estimated_cost), 0)).where(
                    j.provider_name == provider,
                    j.status.in_([JobStatus.PENDING, JobStatus.CLAIMED, JobStatus.RETRY_WAIT]),
                    j.created_at >= since,
                )
            ).scalar_one()
        )

    def jobs(
        self,
        *,
        statuses: Sequence[JobStatus] | None = None,
        target_id: int | None = None,
        backfill_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[MonitoringJobRow]:
        j = MonitoringJobRow
        stmt = sa.select(j)
        if statuses is not None:
            stmt = stmt.where(j.status.in_([s.value for s in statuses]))
        if target_id is not None:
            stmt = stmt.where(j.target_id == target_id)
        if backfill_id is not None:
            stmt = stmt.where(j.backfill_id == backfill_id)
        return list(self._s.execute(stmt.order_by(j.scheduled_at, j.id).limit(limit)).scalars())

    def attempt_metrics(self, since: datetime) -> dict[str, Any]:
        """Aggregates over attempts started since ``since`` (low-cardinality labels only)."""
        a = MonitoringJobAttemptRow
        totals = self._s.execute(
            sa.select(
                sa.func.count(),
                sa.func.count().filter(a.outcome == JobStatus.SUCCEEDED.value),
                sa.func.count().filter(a.outcome == JobStatus.NO_NEW_DATA.value),
                sa.func.count().filter(a.outcome == JobStatus.RETRY_WAIT.value),
                sa.func.count().filter(a.outcome == JobStatus.FAILED.value),
                sa.func.count().filter(a.outcome == JobStatus.DEFERRED.value),
                sa.func.count().filter(a.outcome == "lease_expired"),
                sa.func.coalesce(sa.func.sum(a.new), 0),
                sa.func.coalesce(sa.func.sum(a.unchanged), 0),
                sa.func.coalesce(sa.func.sum(a.revised), 0),
                sa.func.coalesce(sa.func.sum(a.conflicting), 0),
                sa.func.coalesce(sa.func.sum(a.rejected), 0),
                sa.func.coalesce(sa.func.sum(a.in_batch_duplicates), 0),
                sa.func.coalesce(sa.func.sum(a.withheld), 0),
                sa.func.percentile_cont(0.5).within_group(a.latency_ms),
                sa.func.percentile_cont(0.95).within_group(a.latency_ms),
                sa.func.percentile_cont(0.5).within_group(a.scheduled_delay_ms),
                sa.func.percentile_cont(0.95).within_group(a.scheduled_delay_ms),
            ).where(a.started_at >= since)
        ).one()
        keys = [
            "attempts",
            "succeeded",
            "no_new_data",
            "retry_wait",
            "failed",
            "deferred",
            "lease_expired",
            "new_observations",
            "unchanged_observations",
            "revised_observations",
            "conflicting_observations",
            "rejected_records",
            "in_batch_duplicates",
            "withheld_by_policy",
            "latency_ms_p50",
            "latency_ms_p95",
            "scheduled_delay_ms_p50",
            "scheduled_delay_ms_p95",
        ]
        out: dict[str, Any] = {}
        for key, value in zip(keys, totals, strict=True):
            out[key] = round(float(value), 1) if isinstance(value, float) else value
        by_error = self._s.execute(
            sa.select(a.error_category, sa.func.count())
            .where(a.started_at >= since, a.error_category.is_not(None))
            .group_by(a.error_category)
        ).all()
        out["errors_by_category"] = {r[0].value: int(r[1]) for r in by_error}
        by_provider = self._s.execute(
            sa.select(
                a.provider_name,
                a.category,
                sa.func.count(),
                sa.func.coalesce(sa.func.sum(a.new), 0),
            )
            .where(a.started_at >= since)
            .group_by(a.provider_name, a.category)
        ).all()
        out["by_provider_category"] = [
            {"provider": r[0], "category": r[1].value, "attempts": int(r[2]), "new": int(r[3])}
            for r in by_provider
        ]
        quota = self._s.execute(
            sa.select(a.provider_name, a.quota_used).where(
                a.started_at >= since, a.quota_used != sa.text("'{}'::jsonb")
            )
        ).all()
        used: dict[str, dict[str, int]] = {}
        for provider, ledger in quota:
            bucket = used.setdefault(provider, {})
            for name, units in (ledger or {}).items():
                bucket[name] = bucket.get(name, 0) + int(units)
        out["quota_used_by_provider"] = used
        return out

    def attempt_stats_by_target(
        self, target_ids: Sequence[int], since: datetime
    ) -> dict[int, dict[str, int]]:
        if not target_ids:
            return {}
        a, j = MonitoringJobAttemptRow, MonitoringJobRow
        rows = self._s.execute(
            sa.select(
                j.target_id,
                sa.func.count(),
                sa.func.count().filter(a.outcome.in_(["failed", "retry_wait"])),
                sa.func.coalesce(sa.func.sum(a.in_batch_duplicates), 0),
                sa.func.coalesce(sa.func.sum(a.rejected), 0),
                sa.func.count().filter(a.error_category == ErrorCategory.AUTHORIZATION),
                sa.func.count().filter(a.error_category == ErrorCategory.VALIDATION),
                sa.func.coalesce(
                    sa.func.sum(
                        sa.cast(a.rejected_by_reason["invalid_timestamp"].astext, sa.Integer)
                    ),
                    0,
                ),
            )
            .join(j, j.id == a.job_id)
            .where(j.target_id.in_(list(target_ids)), a.started_at >= since)
            .group_by(j.target_id)
        ).all()
        return {
            int(r[0]): {
                "attempts": int(r[1]),
                "failures": int(r[2]),
                "in_batch_duplicates": int(r[3]),
                "rejected": int(r[4]),
                "auth_failures": int(r[5]),
                "validation_failures": int(r[6]),
                "invalid_timestamps": int(r[7]),
            }
            for r in rows
        }

    def attempts_for_target(self, target_id: int) -> list[MonitoringJobAttemptRow]:
        a, j = MonitoringJobAttemptRow, MonitoringJobRow
        return list(
            self._s.execute(
                sa.select(a)
                .join(j, j.id == a.job_id)
                .where(j.target_id == target_id)
                .order_by(a.started_at, a.id)
            ).scalars()
        )
