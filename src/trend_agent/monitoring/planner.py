"""Planning cycle: turn due targets into durable jobs within capacity, budget and quota.

Order of decisions for every due target (ACTIVE / STALE / QUOTA_BLOCKED):

1. provider still available?             no  -> target PROVIDER_UNAVAILABLE, no job
2. will the source have new data?        no  -> push ``next_collection_at``; count
                                                ``skipped_no_new_data`` (no request made)
3. score = tier weight x priority x aging (overdue time); starving targets jump the queue
4. split this cycle's capacity by category (45/25/30 by default) with a 20 % exploration
   reserve inside each category; unused slots are redistributed, so no category starves
   while another has spare room, and nothing is invented to fill a share
5. quota: estimated cost of the job + already-queued jobs vs. the provider's remaining
   quota (safety margin included). Over budget -> target QUOTA_BLOCKED until the
   provider's documented reset (DST-aware), counted as a quota incident
6. create the job with an idempotency key (target, provider, expected period, hour) so
   concurrent planners cannot double-schedule

``dry_run`` performs every read and decision inside a transaction that is rolled back.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory
from trend_agent.discovery.budget import allocate_slots
from trend_agent.discovery.models import TARGET_CATEGORIES, Category
from trend_agent.monitoring.collectors import REQUEST_BUCKET, CollectorRegistry
from trend_agent.monitoring.config import MonitoringConfig
from trend_agent.monitoring.freshness import estimate_delay
from trend_agent.monitoring.lifecycle import PLANNER_ACTOR
from trend_agent.monitoring.scheduler import next_check, tier_rule
from trend_agent.monitoring.target_manager import set_status
from trend_agent.schemas.monitoring import (
    COLLECTABLE_STATUSES,
    JobType,
    MonitoringStatus,
)
from trend_agent.schemas.scheduling import (
    CategoryUsage,
    DelayEstimate,
    NextCheck,
    PlannedJob,
    PlanReport,
    ProviderProfile,
)
from trend_agent.utils.time import Clock

TRANSIENT_STATUSES = frozenset({"temporarily_unavailable", "rate_limited"})


@dataclass
class _Eligible:
    target: MonitoringTargetRow
    provider: str
    profile: ProviderProfile
    check: NextCheck
    score: float
    exploratory: bool
    forced: bool


class Planner:
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

    def plan(self, *, dry_run: bool = False) -> PlanReport:
        now = self._clock()
        session = self._factory()
        try:
            report = self._plan(session, now, dry_run)
            if dry_run:
                session.rollback()
            else:
                session.commit()
            return report
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ internals
    def _plan(self, session: Session, now: datetime, dry_run: bool) -> PlanReport:
        cfg = self._config
        repo = MonitoringRepository(session)
        jobs = JobRepository(session)
        ts = TimeSeriesRepository(session)
        queue_depth = jobs.queue_depth()
        capacity = max(
            0, min(cfg.capacity.max_jobs_per_cycle, cfg.capacity.max_queue_depth - queue_depth)
        )
        report = PlanReport(started_at=now, dry_run=dry_run, capacity=capacity)
        if capacity == 0:
            report.notes.append(
                f"queue depth {queue_depth} at limit; no new jobs (no backlog growth)"
            )
        if not dry_run:
            for name in self._collectors.names():
                # A status check reporting an outage is a health signal (no request made).
                avail = self._collectors.availability(name)
                if avail.status in TRANSIENT_STATUSES:
                    repo.record_health(
                        name,
                        success=False,
                        now=now,
                        unhealthy_after=cfg.jobs.unhealthy_after_failures,
                        error_category="transient",
                        error_message=f"status check: {avail.status} - {avail.detail}",
                    )
        delays: dict[str, DelayEstimate] = {}
        eligible: list[_Eligible] = []
        after: int | None = None
        while True:
            page = repo.due_targets(
                list(COLLECTABLE_STATUSES),
                now,
                after_id=after,
                limit=cfg.capacity.planning_page_size,
            )
            if not page:
                break
            after = page[-1].id
            report.due_targets += len(page)
            queued = jobs.open_job_targets([t.id for t in page])
            for target in page:
                if target.id in queued:
                    report.jobs_already_queued += 1
                    continue
                item = self._evaluate_target(repo, ts, target, now, delays, report, dry_run)
                if item is not None:
                    eligible.append(item)
        chosen = self._allocate(eligible, capacity, report, now)
        self._create_jobs(session, repo, jobs, chosen, report, now, dry_run)
        if not dry_run:
            repo.add_plan_run(
                {
                    "started_at": now,
                    "completed_at": self._clock(),
                    "dry_run": False,
                    "capacity": capacity,
                    "jobs_created": report.jobs_created,
                    "config_version": cfg.config_version,
                    "details": report.model_dump(mode="json", exclude={"planned"}),
                }
            )
        return report

    def _evaluate_target(
        self,
        repo: MonitoringRepository,
        ts: TimeSeriesRepository,
        target: MonitoringTargetRow,
        now: datetime,
        delays: dict[str, DelayEstimate],
        report: PlanReport,
        dry_run: bool,
    ) -> _Eligible | None:
        provider = target.provider_in_use
        avail = self._collectors.availability(provider) if provider else None
        if provider is None or avail is None or not avail.available:
            report.provider_unavailable += 1
            detail = avail.detail if avail else "no provider assigned"
            set_status(
                repo,
                target,
                MonitoringStatus.PROVIDER_UNAVAILABLE,
                reason=f"provider {provider} unavailable: {detail}",
                actor=PLANNER_ACTOR,
                now=now,
            )
            return None
        profile = self._collectors.profile(provider)
        assert profile is not None
        if provider not in delays:
            delays[provider] = estimate_delay(ts.delay_samples(provider), profile)
        override = target.manual_override or {}
        force_at = override.get("force_at")
        forced_at = datetime.fromisoformat(force_at) if isinstance(force_at, str) else None
        rule = tier_rule(self._config, target.tier)
        check = next_check(
            now=now,
            profile=profile,
            tier_rule=rule,
            delay=delays[provider],
            latest_period_end=target.latest_period_end,
            last_attempt_at=target.last_attempt_at,
            last_no_data_at=target.last_no_data_at,
            force_at=forced_at,
        )
        if not check.eligible:
            if "minimum polling" in check.reason:
                report.skipped_backoff += 1
            else:
                report.skipped_no_new_data += 1
            target.next_collection_at = check.next_at
            return None
        overdue = now - (target.next_collection_at or now)
        aging = 1.0 + min(3.0, max(0.0, overdue.total_seconds()) / 86400.0)
        score = rule.weight * (0.5 + target.priority_score / 100.0) * aging
        if overdue >= self._config.capacity.starvation_after:
            score += 1000.0  # starvation guard: overdue targets go first
        exploratory = target.latest_period_end is None or (
            target.activated_at is not None
            and now - target.activated_at <= self._config.exploration_window
        )
        return _Eligible(
            target, provider, profile, check, round(score, 4), exploratory, forced_at is not None
        )

    def _allocate(
        self, eligible: list[_Eligible], capacity: int, report: PlanReport, now: datetime
    ) -> list[tuple[_Eligible, str]]:
        cfg = self._config
        shares = {c: cfg.category_budgets.share(c) for c in TARGET_CATEGORIES}
        slots = allocate_slots(capacity, shares, TARGET_CATEGORIES) if capacity else {}
        by_cat: dict[Category, list[_Eligible]] = defaultdict(list)
        for item in eligible:
            by_cat[item.target.category].append(item)
        chosen: list[tuple[_Eligible, str]] = []
        leftovers: list[_Eligible] = []
        spare = 0
        for category in TARGET_CATEGORIES:
            items = sorted(
                by_cat.get(category, []), key=lambda e: (-int(e.forced), -e.score, e.target.id)
            )
            usage = report.categories.setdefault(category.value, CategoryUsage())
            usage.candidates = len(items)
            reserved = slots.get(category, 0)
            usage.reserved = reserved
            explore = math.ceil(reserved * cfg.exploration_share) if reserved else 0
            usage.exploration_reserved = explore
            exploratory = [e for e in items if e.exploratory][:explore]
            taken = {id(e) for e in exploratory}
            picks = [(e, "exploration") for e in exploratory]
            for e in items:
                if len(picks) >= reserved:
                    break
                if id(e) not in taken:
                    picks.append((e, "exploitation"))
                    taken.add(id(e))
            usage.used = len(picks)
            usage.exploration_used = sum(1 for _, pool in picks if pool == "exploration")
            chosen.extend(picks)
            leftovers.extend(e for e in items if id(e) not in taken)
            spare += reserved - len(picks)
        # Redistribute unused capacity to the best remaining targets of any category.
        for e in sorted(leftovers, key=lambda e: (-e.score, e.target.id))[:spare]:
            chosen.append((e, "redistributed"))
            report.categories[e.target.category.value].redistributed_in += 1
            report.categories[e.target.category.value].used += 1
        # Targets left over stay due; aging raises their score next cycle.
        return chosen

    def _create_jobs(
        self,
        session: Session,
        repo: MonitoringRepository,
        jobs: JobRepository,
        chosen: list[tuple[_Eligible, str]],
        report: PlanReport,
        now: datetime,
        dry_run: bool,
    ) -> None:
        planned_cost: dict[str, int] = defaultdict(int)
        remaining_cache: dict[str, tuple[int | None, datetime | None]] = {}
        for item, pool in chosen:
            target = item.target
            collector = self._collectors.get(item.provider)
            quota = collector.quota_manager()
            cost = item.profile.quota.request_cost
            bucket = REQUEST_BUCKET if quota is not None else None
            if quota is not None:
                if item.provider not in remaining_cache:
                    usage = quota.usage(REQUEST_BUCKET)
                    pending = jobs.pending_cost(item.provider, usage.window_start)
                    remaining_cache[item.provider] = (usage.remaining - pending, usage.window_end)
                remaining, resets = remaining_cache[item.provider]
                assert remaining is not None
                if remaining - planned_cost[item.provider] < cost:
                    report.deferred_quota += 1
                    msg = (
                        f"{item.provider}: estimated quota exhausted for this window; "
                        f"deferred until {resets.isoformat() if resets else 'reset'}"
                    )
                    if msg not in report.quota_incidents:
                        report.quota_incidents.append(msg)
                    set_status(
                        repo,
                        target,
                        MonitoringStatus.QUOTA_BLOCKED,
                        reason=msg,
                        actor=PLANNER_ACTOR,
                        now=now,
                    )
                    target.next_collection_at = resets
                    continue
                planned_cost[item.provider] += cost
                est = report.estimated_quota.setdefault(item.provider, {})
                est[bucket or "requests"] = est.get(bucket or "requests", 0) + cost
            period_key = (
                item.check.expected_period_end.isoformat()
                if item.check.expected_period_end
                else "initial"
            )
            job_type = JobType.MANUAL_COLLECTION if item.forced else JobType.SCHEDULED_COLLECTION
            key = f"{job_type.value}:{target.id}:{item.provider}:{period_key}:{now:%Y%m%dT%H}"
            planned = PlannedJob(
                target_id=target.id,
                provider_name=item.provider,
                category=target.category,
                tier=target.tier,
                score=item.score,
                pool=pool,
                estimated_cost=cost,
                quota_bucket=bucket,
                reason=item.check.reason,
                expected_period_end=item.check.expected_period_end,
            )
            report.planned.append(planned)
            if dry_run:
                continue
            created = jobs.enqueue(
                {
                    "idempotency_key": key,
                    "target_id": target.id,
                    "provider_name": item.provider,
                    "job_type": job_type,
                    "category": target.category,
                    "scheduled_at": now,
                    "priority": round(item.score * 100),
                    "max_attempts": self._config.jobs.max_attempts,
                    "estimated_cost": cost,
                    "quota_bucket": bucket,
                    "payload": {
                        "pool": pool,
                        "reason": item.check.reason,
                        "expected_period_end": period_key,
                    },
                    "created_at": now,
                    "updated_at": now,
                }
            )
            if created is not None:
                report.jobs_created += 1
            else:
                report.jobs_already_queued += 1
            # Provisional: the executor sets the real next time from the result.
            rule = tier_rule(self._config, target.tier)
            target.next_collection_at = now + max(
                rule.recheck_after_no_data, item.profile.min_poll_interval
            )
            if item.forced:
                override = dict(target.manual_override or {})
                override.pop("force_at", None)
                target.manual_override = override
        session.flush()


def window_for(
    *,
    now: datetime,
    profile: ProviderProfile,
    latest_period_end: datetime | None,
    initial_lookback: timedelta,
) -> tuple[datetime, datetime]:
    """Request window for a scheduled collection (never wider than the provider allows)."""
    from trend_agent.schemas.scheduling import WindowStrategy

    end = now
    if profile.window_strategy is WindowStrategy.FULL_WINDOW:
        start = now - profile.full_window
    elif latest_period_end is None:
        start = now - initial_lookback
    else:
        start = latest_period_end - profile.revision_window
    if profile.max_historical_lookback is not None:
        start = max(start, now - profile.max_historical_lookback)
    start = max(start, end - profile.max_window_per_request)
    return start, end
