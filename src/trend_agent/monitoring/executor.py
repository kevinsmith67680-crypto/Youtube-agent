"""Worker: claim jobs, collect, write observations idempotently, record every attempt.

Execution semantics are at-least-once. A job may run twice (a worker died after writing
but before marking the job done, and the expired lease was reclaimed). That is safe:

* job state changes are fenced on the claim's lease token, so a stale worker cannot
  overwrite the state written by the worker that reclaimed the job;
* observation writes are idempotent at the database layer (``TimeSeriesRepository``):
  the second run finds identical rows and records them as ``unchanged``.

A successful HTTP request is not automatically a data refresh: jobs end ``SUCCEEDED`` only
when new or revised observations were stored, otherwise ``NO_NEW_DATA``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time as _time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.detection_runs import DEFAULT_CHANNEL, DetectionQueueRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.logging import get_logger, log_event
from trend_agent.monitoring.collectors import CollectorRegistry
from trend_agent.monitoring.config import MonitoringConfig
from trend_agent.monitoring.freshness import estimate_delay
from trend_agent.monitoring.job_state import PERMANENT, classify, decide_retry
from trend_agent.monitoring.leases import keep_lease
from trend_agent.monitoring.lifecycle import EXECUTOR_ACTOR
from trend_agent.monitoring.planner import window_for
from trend_agent.monitoring.scheduler import next_check, tier_rule
from trend_agent.monitoring.target_manager import set_status, snapshot
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    ProviderDisabledError,
    ProviderError,
    ProviderNotConfiguredError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.quota import QuotaLedger
from trend_agent.schemas.enums import ProviderStatus
from trend_agent.schemas.monitoring import (
    COLLECTABLE_STATUSES,
    ErrorCategory,
    JobStatus,
    JobType,
    MonitoringStatus,
    WriteOutcome,
)
from trend_agent.schemas.provider import ProviderStatusReport
from trend_agent.utils.time import Clock

_log = get_logger(__name__)
M = MonitoringStatus


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    token: uuid.UUID
    target_id: int
    provider_name: str
    job_type: JobType
    category: str
    attempt: int
    max_attempts: int
    scheduled_at: datetime
    payload: dict[str, Any]
    backfill_id: uuid.UUID | None


@dataclass
class ExecutionOutcome:
    job_id: uuid.UUID
    status: JobStatus | None  # None: lease lost before start
    counts: dict[str, int] = field(default_factory=dict)
    error_category: ErrorCategory | None = None
    message: str = ""


@dataclass
class WorkerReport:
    worker: str
    recovered_leases: int = 0
    claimed: int = 0
    crashed: bool = False
    outcomes: list[ExecutionOutcome] = field(default_factory=list)

    def by_status(self) -> dict[str, int]:
        return dict(Counter(o.status.value if o.status else "lease_lost" for o in self.outcomes))

    def totals(self) -> dict[str, int]:
        total: Counter[str] = Counter()
        for o in self.outcomes:
            total.update(o.counts)
        return dict(total)


class DatabaseWriteError(Exception):
    """Wraps a storage failure so it is classified (and retried) as DB_WRITE."""


def error_from_status(report: ProviderStatusReport) -> ProviderError:
    name = report.provider_name
    detail = report.detail
    match report.status:
        case ProviderStatus.NOT_CONFIGURED:
            return ProviderNotConfiguredError(
                detail, missing=report.missing_configuration, provider=name
            )
        case ProviderStatus.ACCESS_RESTRICTED:
            return AccessRestrictedError(detail, provider=name)
        case ProviderStatus.UNSUPPORTED:
            return CapabilityNotSupportedError(detail, provider=name)
        case ProviderStatus.RATE_LIMITED:
            return RateLimitedError(detail, provider=name)
        case ProviderStatus.QUOTA_EXHAUSTED:
            return QuotaExhaustedError(
                detail, bucket="provider", resets_at=None, source="server", provider=name
            )
        case ProviderStatus.DISABLED:
            return ProviderDisabledError(detail, provider=name)
        case _:
            return TemporarilyUnavailableError(detail, provider=name)


class Executor:
    def __init__(
        self,
        session_factory: SessionFactory,
        config: MonitoringConfig,
        collectors: CollectorRegistry,
        *,
        clock: Clock,
        worker_id: str | None = None,
        rng: random.Random | None = None,
        detection_channel: str | None = DEFAULT_CHANNEL,
    ) -> None:
        self._factory = session_factory
        self._config = config
        self._collectors = collectors
        self._clock = clock
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        # Phase 4 outbox: None disables queueing new observations for detection.
        self._detection_channel = detection_channel

    # ------------------------------------------------------------------ claiming
    def claim(
        self, *, limit: int | None = None, job_ids: list[uuid.UUID] | None = None
    ) -> tuple[int, list[ClaimedJob]]:
        now = self._clock()
        with transaction(self._factory) as session:
            jobs = JobRepository(session)
            recovered = jobs.recover_expired(now)
            for row in recovered:
                log_event(
                    _log,
                    "monitor.lease_recovered",
                    logging.WARNING,
                    job_id=str(row.id),
                    provider=row.provider_name,
                    status=row.status.value,
                )
        with transaction(self._factory) as session:
            rows = JobRepository(session).claim(
                worker=self.worker_id,
                now=now,
                lease=self._config.jobs.lease_duration,
                limit=limit or self._config.jobs.claim_batch,
                job_ids=job_ids,
            )
            claimed = []
            for r in rows:
                assert r.lease_token is not None
                claimed.append(
                    ClaimedJob(
                        id=r.id,
                        token=r.lease_token,
                        target_id=r.target_id,
                        provider_name=r.provider_name,
                        job_type=r.job_type,
                        category=r.category.value,
                        attempt=r.attempt_count,
                        max_attempts=r.max_attempts,
                        scheduled_at=r.scheduled_at,
                        payload=dict(r.payload or {}),
                        backfill_id=r.backfill_id,
                    )
                )
        return len(recovered), claimed

    async def run_once(
        self,
        *,
        limit: int | None = None,
        job_ids: list[uuid.UUID] | None = None,
        crash_after_claim: bool = False,
    ) -> WorkerReport:
        """Claim one batch and execute it with bounded concurrency.

        ``crash_after_claim`` simulates a worker that dies right after claiming (tests and
        the demo): the leases simply expire and another worker recovers the jobs.
        """
        report = WorkerReport(worker=self.worker_id)
        report.recovered_leases, claimed = self.claim(limit=limit, job_ids=job_ids)
        report.claimed = len(claimed)
        if crash_after_claim:
            report.crashed = True
            return report
        semaphore = asyncio.Semaphore(self._config.jobs.worker_concurrency)

        async def bounded(job: ClaimedJob) -> ExecutionOutcome:
            async with semaphore:
                return await self.execute(job)

        report.outcomes = list(await asyncio.gather(*(bounded(j) for j in claimed)))
        return report

    async def drain(self, *, max_batches: int = 1000) -> list[WorkerReport]:
        """Run batches until nothing is claimable (used by ``monitor run --once``)."""
        reports = []
        for _ in range(max_batches):
            report = await self.run_once()
            if report.claimed == 0:
                break
            reports.append(report)
        return reports

    # ------------------------------------------------------------------ execution
    async def execute(self, job: ClaimedJob) -> ExecutionOutcome:
        started = self._clock()
        started_wall = _time.perf_counter()
        with transaction(self._factory) as session:
            jobs = JobRepository(session)
            if not jobs.start(job.id, job.token, started):
                return ExecutionOutcome(job.id, None, message="lease lost before start")
            repo = MonitoringRepository(session)
            target = repo.get_target(job.target_id)
            backfill = job.job_type is JobType.BACKFILL
            allowed = target is not None and (
                target.status is not M.DISABLED
                if backfill
                else target.status in COLLECTABLE_STATUSES
            )
            if target is None or not allowed:
                status = target.status.value if target else "missing"
                jobs.finish(
                    job.id,
                    job.token,
                    status=JobStatus.CANCELLED,
                    now=started,
                    error_details=f"target is {status}; collection cancelled",
                )
                return ExecutionOutcome(job.id, JobStatus.CANCELLED, message=f"target {status}")
            texts = repo.query_text([target.query_id])[target.query_id]
            snap = snapshot(target, *texts)
            latest = target.latest_period_end
            profile = self._collectors.profile(job.provider_name)
            if backfill:
                window = (
                    datetime.fromisoformat(job.payload["start"]),
                    datetime.fromisoformat(job.payload["end"]),
                )
            elif profile is not None:
                window = window_for(
                    now=started,
                    profile=profile,
                    latest_period_end=latest,
                    initial_lookback=self._config.jobs.initial_lookback,
                )
            else:
                window = (started, started)
            attempt_id = jobs.add_attempt(
                {
                    "job_id": job.id,
                    "attempt": job.attempt,
                    "worker": self.worker_id,
                    "provider_name": job.provider_name,
                    "category": job.category,
                    "started_at": started,
                    "outcome": "running",
                    "window_start": window[0],
                    "window_end": window[1],
                    "scheduled_delay_ms": int((started - job.scheduled_at).total_seconds() * 1000),
                }
            )
        ctx = RequestContext(ledger=QuotaLedger())
        try:
            if profile is None:
                raise ProviderNotConfiguredError(
                    f"no monitoring profile for {job.provider_name!r}", provider=job.provider_name
                )
            collector = self._collectors.get(job.provider_name)
            status_report = collector.status()
            if status_report.status is not ProviderStatus.AVAILABLE:
                raise error_from_status(status_report)
            async with keep_lease(
                self._factory,
                job.id,
                job.token,
                lease=self._config.jobs.lease_duration,
                clock=self._clock,
            ) as lease_state:
                result = await asyncio.wait_for(
                    collector.collect(snap, window[0], window[1], ctx),
                    timeout=self._config.jobs.collect_timeout.total_seconds(),
                )
            if lease_state.lost:
                log_event(
                    _log,
                    "monitor.lease_lost_during_collect",
                    logging.WARNING,
                    job_id=str(job.id),
                    provider=job.provider_name,
                )
        except TimeoutError:
            exc: BaseException = TemporarilyUnavailableError(
                "collection timed out", provider=job.provider_name
            )
            return self._fail(job, exc, attempt_id, started, started_wall, ctx, backfill)
        except Exception as exc:  # classified below; unexpected errors are logged
            if not isinstance(exc, ProviderError):
                log_event(
                    _log,
                    "monitor.unexpected_collect_error",
                    logging.ERROR,
                    job_id=str(job.id),
                    provider=job.provider_name,
                    error_type=type(exc).__name__,
                )
            return self._fail(job, exc, attempt_id, started, started_wall, ctx, backfill)

        try:
            return self._store(
                job, result, attempt_id, started, started_wall, ctx, backfill, profile
            )
        except SQLAlchemyError as exc:
            log_event(
                _log,
                "monitor.db_write_failed",
                logging.ERROR,
                job_id=str(job.id),
                provider=job.provider_name,
                error_type=type(exc).__name__,
            )
            return self._fail(
                job,
                DatabaseWriteError(type(exc).__name__),
                attempt_id,
                started,
                started_wall,
                ctx,
                backfill,
            )

    def _store(
        self,
        job: ClaimedJob,
        result: Any,
        attempt_id: int,
        started: datetime,
        started_wall: float,
        ctx: RequestContext,
        backfill: bool,
        profile: Any,
    ) -> ExecutionOutcome:
        now = self._clock()
        counts: Counter[str] = Counter()
        with transaction(self._factory) as session:
            ts = TimeSeriesRepository(session)
            changed: dict[int, tuple[int | None, datetime]] = {}
            for draft in result.drafts:
                written = ts.write(
                    draft,
                    revision_behaviour=profile.revision_behaviour,
                    ingested_at=self._clock(),
                    job_id=job.id,
                )
                counts[written.outcome.value] += 1
                if draft.value is None:
                    counts["missing_values"] += 1
                if written.outcome in (WriteOutcome.NEW, WriteOutcome.REVISED):
                    held = changed.get(written.series_id)
                    start = draft.period_start if held is None else min(held[1], draft.period_start)
                    changed[written.series_id] = (draft.target_id, start)
            if changed and self._detection_channel is not None:
                # Transactional outbox for Phase 4: the series is queued for detection in
                # the same transaction as its new/revised observations (NOTIFY on commit).
                DetectionQueueRepository(session).mark(
                    ((sid, tid, start) for sid, (tid, start) in changed.items()),
                    now,
                    channel=self._detection_channel or None,
                )
            fresh = counts[WriteOutcome.NEW.value] + counts[WriteOutcome.REVISED.value]
            status = JobStatus.SUCCEEDED if fresh else JobStatus.NO_NEW_DATA
            jobs = JobRepository(session)
            if not jobs.finish(job.id, job.token, status=status, now=now):
                log_event(
                    _log,
                    "monitor.lease_lost_on_finish",
                    logging.WARNING,
                    job_id=str(job.id),
                    provider=job.provider_name,
                )
            repo = MonitoringRepository(session)
            target = repo.get_target(job.target_id, lock=True)
            if target is not None:
                target.last_attempt_at = now
                target.last_success_at = now
                if fresh:
                    target.last_new_observation_at = now
                else:
                    target.last_no_data_at = now
                target.latest_period_end = ts.latest_period_end(target.id, job.provider_name)
                if not backfill:
                    target.consecutive_failures = 0
                    target.last_error_category = None
                    if target.status in (M.STALE, M.QUOTA_BLOCKED):
                        set_status(
                            repo,
                            target,
                            M.ACTIVE,
                            reason="collection succeeded",
                            actor=EXECUTOR_ACTOR,
                            now=now,
                        )
                    delay = estimate_delay(ts.delay_samples(job.provider_name), profile)
                    nxt = next_check(
                        now=now,
                        profile=profile,
                        tier_rule=tier_rule(self._config, target.tier),
                        delay=delay,
                        latest_period_end=target.latest_period_end,
                        last_attempt_at=now,
                        last_no_data_at=target.last_no_data_at,
                    )
                    target.next_collection_at = nxt.next_at
            repo.record_health(
                job.provider_name,
                success=True,
                now=now,
                unhealthy_after=self._config.jobs.unhealthy_after_failures,
            )
            jobs.finish_attempt(
                attempt_id,
                {
                    "finished_at": now,
                    "outcome": status.value,
                    "received": result.received,
                    "new": counts[WriteOutcome.NEW.value],
                    "unchanged": counts[WriteOutcome.UNCHANGED.value],
                    "revised": counts[WriteOutcome.REVISED.value],
                    "conflicting": counts[WriteOutcome.CONFLICTING.value],
                    "missing_values": counts["missing_values"],
                    "rejected": result.rejected,
                    "in_batch_duplicates": result.in_batch_duplicates,
                    "withheld": result.withheld,
                    "rejected_by_reason": result.rejected_by_reason,
                    "quota_used": dict(ctx.ledger.used),
                    "latency_ms": int((_time.perf_counter() - started_wall) * 1000),
                },
            )
        log_event(
            _log,
            "monitor.job_completed",
            job_id=str(job.id),
            provider=job.provider_name,
            status=status.value,
            **dict(counts),
        )
        return ExecutionOutcome(job.id, status, dict(counts))

    def _fail(
        self,
        job: ClaimedJob,
        exc: BaseException,
        attempt_id: int,
        started: datetime,
        started_wall: float,
        ctx: RequestContext,
        backfill: bool,
    ) -> ExecutionOutcome:
        now = self._clock()
        category = ErrorCategory.DB_WRITE if isinstance(exc, DatabaseWriteError) else classify(exc)
        decision = decide_retry(
            exc,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            now=now,
            base=self._config.jobs.retry_base,
            maximum=self._config.jobs.retry_max,
            rng=self._rng,
        )
        message = f"{type(exc).__name__}: {exc}"[:500]
        with transaction(self._factory) as session:
            jobs = JobRepository(session)
            jobs.finish(
                job.id,
                job.token,
                status=decision.status,
                now=now,
                retry_after=decision.retry_at,
                scheduled_at=decision.retry_at if decision.deferred else None,
                error_category=category,
                error_details=message,
                deferred=decision.deferred,
            )
            if decision.status is JobStatus.FAILED:
                # Phase 8: a permanently failed job is kept for an operator, not just closed.
                from trend_agent.db.repositories.ops import DeadLetterRepository

                DeadLetterRepository(session).record(
                    source="collection_job",
                    reference=str(job.id),
                    error_type=category.value,
                    error=message,
                    now=now,
                    payload={
                        "target_id": job.target_id,
                        "attempt": job.attempt,
                        "max_attempts": job.max_attempts,
                        "backfill": backfill,
                    },
                )
            repo = MonitoringRepository(session)
            target = repo.get_target(job.target_id, lock=True)
            if target is not None and not backfill:
                target.last_attempt_at = now
                target.last_error_category = category.value
                if category is ErrorCategory.QUOTA:
                    if target.status in (M.ACTIVE, M.STALE):
                        set_status(
                            repo,
                            target,
                            M.QUOTA_BLOCKED,
                            reason=f"quota exhausted; deferred until {decision.retry_at}",
                            actor=EXECUTOR_ACTOR,
                            now=now,
                        )
                    target.next_collection_at = decision.retry_at
                elif category in PERMANENT:
                    set_status(
                        repo,
                        target,
                        M.PROVIDER_UNAVAILABLE,
                        reason=f"{category.value}: {message[:200]}",
                        actor=EXECUTOR_ACTOR,
                        now=now,
                    )
                else:
                    target.consecutive_failures += 1
                    if (
                        target.consecutive_failures >= self._config.jobs.stale_after_failures
                        and target.status is M.ACTIVE
                    ):
                        set_status(
                            repo,
                            target,
                            M.STALE,
                            reason=f"{target.consecutive_failures} consecutive failures",
                            actor=EXECUTOR_ACTOR,
                            now=now,
                        )
                    target.next_collection_at = decision.retry_at or now
            if category is not ErrorCategory.QUOTA:
                repo.record_health(
                    job.provider_name,
                    success=False,
                    now=now,
                    unhealthy_after=self._config.jobs.unhealthy_after_failures,
                    error_category=category.value,
                    error_message=message,
                )
            jobs.finish_attempt(
                attempt_id,
                {
                    "finished_at": now,
                    "outcome": decision.status.value,
                    "error_category": category,
                    "error_message": message,
                    "quota_used": dict(ctx.ledger.used),
                    "latency_ms": int((_time.perf_counter() - started_wall) * 1000),
                },
            )
        log_event(
            _log,
            "monitor.job_failed",
            logging.WARNING,
            job_id=str(job.id),
            provider=job.provider_name,
            error_category=category.value,
            next_status=decision.status.value,
        )
        return ExecutionOutcome(job.id, decision.status, error_category=category, message=message)
