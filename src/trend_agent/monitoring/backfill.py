"""Historical backfill for providers that legitimately offer history.

* The provider's profile must declare historical coverage; otherwise the request is
  refused with the limitation stated (nothing is synthesised from other data - e.g. video
  publication dates are never turned into "historical search demand").
* The requested range is clamped to the provider's maximum lookback (reported).
* The range is split into windows no wider than ``max_window_per_request``; windows that
  are already covered by stored observations are skipped.
* The cost is estimated against the provider's remaining quota; jobs beyond the budget are
  still queued but the executor defers them until the quota resets (never bypassed).
* A backfill is identified by (target, provider, range): re-issuing it resumes it -
  completed windows are kept, failed/cancelled windows are re-queued, and observation
  writes stay idempotent. Each job (and so each observation) links to the backfill run.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.orm import Session

from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.core import QueryRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.models import Category
from trend_agent.monitoring.collectors import REQUEST_BUCKET, CollectorRegistry
from trend_agent.monitoring.config import MonitoringConfig
from trend_agent.monitoring.lifecycle import MANUAL_ACTOR
from trend_agent.schemas.monitoring import (
    FINAL_JOB_STATUSES,
    JobStatus,
    JobType,
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
)
from trend_agent.utils.text import canonicalize_query
from trend_agent.utils.time import Clock

DAY = timedelta(days=1)


class BackfillError(ValueError):
    """The backfill cannot be performed (reason states the limitation)."""


@dataclass
class BackfillPlan:
    backfill_id: uuid.UUID | None
    target_id: int | None
    provider: str
    query: str
    measurement: str
    range_start: datetime
    range_end: datetime
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    skipped_covered: int = 0
    estimated_requests: int = 0
    quota_remaining: int | None = None
    quota_resets_at: datetime | None = None
    limitations: list[str] = field(default_factory=list)
    jobs_created: int = 0
    resumed: bool = False
    job_ids: list[uuid.UUID] = field(default_factory=list)


def _day(moment: datetime) -> datetime:
    return datetime.combine(moment.astimezone(UTC).date(), time.min, tzinfo=UTC)


class BackfillService:
    def __init__(
        self,
        session_factory: SessionFactory,
        config: MonitoringConfig,
        collectors: CollectorRegistry,
        *,
        clock: Clock,
    ) -> None:
        self._factory = session_factory
        self._config = config
        self._collectors = collectors
        self._clock = clock

    def plan(
        self,
        *,
        query: str,
        provider: str,
        days: int,
        measurement: MeasurementCategory | None = None,
        region: str | None = None,
        language: str | None = None,
        requested_by: str = "cli",
        dry_run: bool = False,
    ) -> BackfillPlan:
        now = self._clock()
        profile = self._collectors.profile(provider)
        if profile is None:
            raise BackfillError(f"unknown provider {provider!r}")
        if not profile.supports_history:
            raise BackfillError(
                f"{provider} does not provide historical data; backfill is not possible and "
                "no history will be synthesised"
            )
        avail = self._collectors.availability(provider)
        if not avail.available:
            raise BackfillError(f"{provider} is not available: {avail.status} - {avail.detail}")
        if days < 1:
            raise BackfillError("days must be >= 1")
        canonical = canonicalize_query(query)
        category = measurement or next(
            (
                c
                for c in self._config.measurement_preferences
                if c in profile.measurement_categories
            ),
            profile.measurement_categories[0],
        )
        if category not in profile.measurement_categories:
            raise BackfillError(f"{provider} does not measure {category.value}")
        region = region or self._config.regions[0]
        language = language if language is not None else self._config.languages[0]
        end = _day(now)
        start = end - timedelta(days=days)
        plan = BackfillPlan(None, None, provider, canonical, category.value, start, end)
        if profile.max_historical_lookback is not None:
            earliest = _day(now - profile.max_historical_lookback)
            if start < earliest:
                plan.limitations.append(
                    f"requested {days} days; {provider} offers at most "
                    f"{profile.max_historical_lookback.days} days - range clamped"
                )
                start = earliest
                plan.range_start = start
        session = self._factory()
        try:
            repo = MonitoringRepository(session)
            jobs = JobRepository(session)
            target = self._target(
                repo,
                session,
                canonical,
                query,
                region,
                language,
                category,
                profile.synthetic,
                now,
                provider,
            )
            plan.target_id = target.id
            signature = hashlib.sha256(
                f"{target.id}|{provider}|{start.isoformat()}|{end.isoformat()}".encode()
            ).hexdigest()
            existing = repo.backfill_by_signature(signature)
            ts = TimeSeriesRepository(session)
            covered = {
                p.period_start
                for p in ts.points(
                    target_id=target.id, provider_name=provider, start=start, end=end, limit=100_000
                )
                if p.value is not None or p.sample_quality.value in ("missing", "suppressed")
            }
            step = profile.max_window_per_request
            cursor = start
            while cursor < end:
                w_end = min(cursor + step, end)
                expected = {cursor + DAY * i for i in range(int((w_end - cursor) / DAY))}
                if expected and expected <= covered:
                    plan.skipped_covered += 1
                else:
                    plan.windows.append((cursor, w_end))
                cursor = w_end
            plan.estimated_requests = len(plan.windows) * profile.quota.request_cost
            quota = self._collectors.get(provider).quota_manager()
            if quota is not None:
                usage = quota.usage(REQUEST_BUCKET)
                plan.quota_remaining = usage.remaining
                plan.quota_resets_at = usage.window_end
                if plan.estimated_requests > usage.remaining:
                    plan.limitations.append(
                        f"{plan.estimated_requests} request(s) exceed the remaining quota "
                        f"({usage.remaining}); the rest wait for the reset at "
                        f"{usage.window_end.isoformat()} (deferred, not bypassed)"
                    )
            if existing is not None:
                # Resume: windows whose job already completed are not requested again (the
                # provider may simply hold no data that far back).
                done = {
                    j.payload.get("start")
                    for j in jobs.jobs(backfill_id=existing.id, limit=10_000)
                    if j.status in (JobStatus.SUCCEEDED, JobStatus.NO_NEW_DATA)
                }
                kept = [w for w in plan.windows if w[0].isoformat() not in done]
                plan.skipped_covered += len(plan.windows) - len(kept)
                plan.windows = kept
                plan.estimated_requests = len(kept) * profile.quota.request_cost
            if dry_run:
                session.rollback()
                return plan
            if existing is None:
                row = repo.add_backfill(
                    {
                        "signature": signature,
                        "target_id": target.id,
                        "provider_name": provider,
                        "range_start": start,
                        "range_end": end,
                        "status": "planned",
                        "windows_planned": len(plan.windows),
                        "estimated_requests": plan.estimated_requests,
                        "requested_by": requested_by,
                        "details": {
                            "limitations": plan.limitations,
                            "skipped_covered": plan.skipped_covered,
                        },
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                backfill_id = row.id
            else:
                backfill_id = existing.id
                plan.resumed = True
                existing.updated_at = now
                stale = [
                    j.id
                    for j in jobs.jobs(backfill_id=backfill_id, limit=10_000)
                    if j.status in (JobStatus.FAILED, JobStatus.CANCELLED)
                ]
                jobs.reset_for_resume(stale, now)
            plan.backfill_id = backfill_id
            for w_start, w_end in plan.windows:
                job_id = jobs.enqueue(
                    {
                        "idempotency_key": f"backfill:{backfill_id}:{w_start.date()}",
                        "target_id": target.id,
                        "provider_name": provider,
                        "job_type": JobType.BACKFILL,
                        "category": target.category,
                        "scheduled_at": now,
                        "priority": 50,
                        "max_attempts": self._config.jobs.max_attempts,
                        "estimated_cost": profile.quota.request_cost,
                        "quota_bucket": REQUEST_BUCKET if quota is not None else None,
                        "payload": {"start": w_start.isoformat(), "end": w_end.isoformat()},
                        "backfill_id": backfill_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                if job_id is not None:
                    plan.jobs_created += 1
            plan.job_ids = [
                j.id
                for j in jobs.jobs(backfill_id=backfill_id, limit=10_000)
                if j.status not in FINAL_JOB_STATUSES
            ]
            session.commit()
            return plan
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def _target(
        self,
        repo: MonitoringRepository,
        session: Session,
        canonical: str,
        original: str,
        region: str,
        language: str | None,
        category: MeasurementCategory,
        synthetic: bool,
        now: datetime,
        provider: str,
    ) -> MonitoringTargetRow:
        queries = QueryRepository(session)
        query_id = queries.get_or_create(canonical, original, language, None)
        target = repo.find_target(query_id, region, language, category)
        keyword = repo.keyword_for_query(canonical)
        if target is not None:
            if target.is_synthetic != synthetic:
                kind = "synthetic" if synthetic else "real"
                raise BackfillError(
                    f"refusing to backfill {kind} data into a "
                    f"{'synthetic' if target.is_synthetic else 'real'} target"
                )
            return target
        if keyword is not None and keyword.is_synthetic != synthetic:
            raise BackfillError(
                "synthetic providers may only backfill synthetic keywords "
                "(and real providers only real ones)"
            )
        row, created = repo.create_target(
            {
                "keyword_id": keyword.id if keyword else None,
                "query_id": query_id,
                "category": keyword.primary_category if keyword else Category.OTHER,
                "topic_cluster_id": keyword.topic_cluster_id if keyword else None,
                "intent_cluster_id": keyword.intent_cluster_id if keyword else None,
                "region": region,
                "language": language,
                "measurement_category": category,
                "is_proxy": False,
                "preferred_provider": provider,
                "fallback_providers": [],
                "provider_in_use": None,
                "tier": MonitoringTier.LOW,
                "tier_reason": "created for backfill",
                "tier_changed_at": now,
                "status": MonitoringStatus.PENDING,
                "status_reason": "created for backfill; not actively monitored",
                "status_changed_at": now,
                "priority_score": keyword.priority_score if keyword else 0.0,
                "novelty_kind": keyword.novelty_kind.value if keyword else None,
                "config_version": self._config.config_version,
                "is_synthetic": synthetic,
                # History only: never auto-activated (``monitor activate`` makes it monitored).
                "manual_override": {"backfill_only": True},
            }
        )
        if created:
            repo.add_event(
                row.id,
                kind="status",
                from_value=None,
                to_value=MonitoringStatus.PENDING.value,
                reason="created for backfill",
                actor=MANUAL_ACTOR,
                at=now,
            )
        return row

    def status(self, backfill_id: uuid.UUID) -> dict[str, object]:
        with transaction(self._factory) as session:
            jobs = JobRepository(session).jobs(backfill_id=backfill_id, limit=10_000)
            counts: dict[str, int] = {}
            for j in jobs:
                counts[j.status.value] = counts.get(j.status.value, 0) + 1
            done = sum(counts.get(s.value, 0) for s in (JobStatus.SUCCEEDED, JobStatus.NO_NEW_DATA))
            state = (
                "completed"
                if jobs and done == len(jobs)
                else (
                    "failed"
                    if counts.get(JobStatus.FAILED.value)
                    else "running"
                    if jobs
                    else "empty"
                )
            )
            return {
                "backfill_id": str(backfill_id),
                "jobs": counts,
                "state": state,
                "windows_done": done,
                "windows_total": len(jobs),
            }
