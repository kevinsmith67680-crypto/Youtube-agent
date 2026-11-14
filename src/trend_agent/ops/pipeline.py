"""The production process model: one pipeline scheduler, N workers.

``PipelineScheduler`` (``trend-agent system scheduler``)
    Holds the ``pipeline`` lease (database, expiring, fenced). Only the holder runs the
    schedule; a second scheduler waits as a hot standby and takes over when the lease
    expires. Each tick it runs the due stages - ops checks, monitoring planning, content
    analysis, forecasting, recommendations, retention - each gated by the operating mode's
    capabilities and the budgets, each recorded in ``scheduled_runs`` with a correlation ID.
    A failing stage is recorded and alerted (deduplicated); it never stops the others.

``PipelineWorker`` (``trend-agent system worker``)
    Executes collection jobs (Phase 3 ``SKIP LOCKED`` leases) and processes the detection
    queue (Phase 4, advisory locks per series). Any number can run, up to
    ``MAX_CONCURRENT_WORKERS`` (checked from heartbeats at start-up).

Both write heartbeats, handle SIGTERM/SIGINT by finishing the current tick, marking the
heartbeat ``stopped`` and (scheduler) releasing the lease, then exit.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from trend_agent.db.repositories.ops import (
    DeadLetterRepository,
    HeartbeatRepository,
    Lease,
    LeaseRepository,
    ScheduledRunRepository,
)
from trend_agent.db.session import transaction
from trend_agent.logging import correlation, get_logger, log_event, redact_text
from trend_agent.ops.alerts import OpsAlerter
from trend_agent.ops.budgets import BudgetState
from trend_agent.ops.config import OperationsConfig
from trend_agent.ops.health import SCHEDULER_LEASE, Heartbeat
from trend_agent.ops.modes import ModeDecision, SystemCapability
from trend_agent.ops.system import SystemService

if TYPE_CHECKING:
    from trend_agent.container import AppContainer

_log = get_logger(__name__)
C = SystemCapability

StageFn = Callable[["StageContext"], "dict[str, Any] | Awaitable[dict[str, Any]]"]


@dataclass(frozen=True)
class Stage:
    name: str
    run: StageFn
    # any one of these capabilities allows the stage (empty: always allowed)
    requires: tuple[SystemCapability, ...] = ()


@dataclass
class StageContext:
    container: AppContainer
    decision: ModeDecision
    synthetic: bool
    correlation_id: str
    max_per_run: int | None = None


@dataclass
class TickReport:
    leader: bool
    token: int | None = None
    ran: dict[str, str] = field(default_factory=dict)  # task -> succeeded|failed|skipped
    mode: str | None = None


# ---------------------------------------------------------------------- stages
def _ops_checks(ctx: StageContext) -> dict[str, Any]:
    # mode recording and alerting happen in the scheduler (it owns the alerter)
    return {"mode": ctx.decision.mode.value, "base_mode": ctx.decision.base_mode.value}


async def _discovery(ctx: StageContext) -> dict[str, Any]:
    """Unattended discovery across all categories and providers (GitHub included)."""
    c = ctx.container
    if ctx.synthetic:
        return {"skipped": "synthetic mode: no live discovery"}
    if not c.settings.scheduled_discovery:
        return {"skipped": "disabled: set TREND_AGENT_SCHEDULED_DISCOVERY=true"}
    from trend_agent.discovery.models import TARGET_CATEGORIES

    summary = await c.discovery().run(TARGET_CATEGORIES, max_fetches=ctx.max_per_run)
    by_provider: dict[str, dict[str, int]] = {}
    for s in summary.sources:
        p = by_provider.setdefault(s.provider, {"fetched": 0, "failed": 0, "items_new": 0})
        p["fetched"] += 1
        p["failed"] += int(s.status == "failed")
        p["items_new"] += s.items_new
    return {
        "status": summary.status.value,
        "candidates_new": sum(v.candidates_new for v in summary.categories.values()),
        "providers": by_provider,
    }


def _monitor_plan(ctx: StageContext) -> dict[str, Any]:
    svc = ctx.container.monitoring(include_synthetic=ctx.synthetic or None)
    sync = svc.sync_targets()
    plan = svc.plan()
    return {
        "targets_activated": sync.activated,
        "jobs_created": plan.jobs_created,
        "skipped_no_new_data": plan.skipped_no_new_data,
        "deferred_quota": plan.deferred_quota,
    }


async def _content_analysis(ctx: StageContext) -> dict[str, Any]:
    c = ctx.container
    exclude = c.budget_guard().topics_over_analysis_limit(c.clock())
    svc = c.content_analysis()
    report = await svc.run(
        include_synthetic=ctx.synthetic,
        limit=ctx.max_per_run,
        exclude_targets=frozenset(exclude),
    )
    return {
        "planned": len(report.plan.selected),
        "results": report.counts,
        "topics_at_daily_limit": len(exclude),
        "budget_notes": svc.budget_notes[-5:],
    }


def _forecasting(ctx: StageContext) -> dict[str, Any]:
    rows = ctx.container.forecasting().predict(include_synthetic=ctx.synthetic)
    return {"forecasts": len(rows), "created": sum(1 for _, created in rows if created)}


def _recommendations(ctx: StageContext) -> dict[str, Any]:
    c = ctx.container
    svc = c.recommendations(include_synthetic=ctx.synthetic)
    svc.alerts_enabled = ctx.decision.allows(C.CREATOR_ALERTS) or ctx.synthetic
    res = svc.generate(
        all_categories=True,
        max_opportunities=c.settings.max_recommendations_per_run,
        trigger="scheduled",
    )
    return {
        "run_id": res.run_id,
        "considered": res.considered,
        "generated": res.generated,
        "reused": res.reused,
        "approved": res.approved,
        "research": res.research,
        "rejected": res.rejected,
        "generator": res.generator,
        "estimated_cost_usd": res.estimated_cost_usd,
        "alerts_enabled": svc.alerts_enabled,
        "notes": res.notes[:5],
    }


def _retention(ctx: StageContext) -> dict[str, Any]:
    purged = ctx.container.retention().purge_expired()
    with transaction(ctx.container.session_factory) as s:
        hb = HeartbeatRepository(s).prune(ctx.container.clock() - timedelta(days=7))
    return {"purged": purged, "stale_heartbeats_pruned": hb}


DEFAULT_STAGES: tuple[Stage, ...] = (
    Stage("ops_checks", _ops_checks),
    Stage("discovery", _discovery, (C.COLLECT_REAL_DATA,)),
    Stage("monitor_plan", _monitor_plan, (C.COLLECT_REAL_DATA,)),
    Stage("content_analysis", _content_analysis, (C.CONTENT_GAP_ANALYSIS,)),
    Stage("forecasting", _forecasting, (C.PROXY_FORECASTS, C.DIRECT_SEARCH_FORECASTS)),
    Stage("recommendations", _recommendations, (C.RECOMMENDATIONS,)),
    Stage("retention", _retention),
)


# ---------------------------------------------------------------------- scheduler
class PipelineScheduler:
    def __init__(
        self,
        container: AppContainer,
        cfg: OperationsConfig,
        *,
        synthetic: bool = False,
        owner: str | None = None,
        stages: tuple[Stage, ...] = DEFAULT_STAGES,
        alerter: OpsAlerter | None = None,
    ) -> None:
        self.c = container
        self.cfg = cfg
        self.synthetic = synthetic
        self.stages = stages
        self.system = SystemService(container)
        self.heartbeat = Heartbeat(
            container.session_factory,
            role="scheduler",
            interval=timedelta(seconds=cfg.scheduler.tick_seconds),
            clock=container.clock,
            service_id=owner,
        )
        self.owner = self.heartbeat.service_id
        self.alerter = alerter or OpsAlerter(
            container.session_factory,
            clock=container.clock,
            cooldown=timedelta(minutes=container.settings.ops_alert_cooldown_minutes),
        )
        self.lease: Lease | None = None

    # -- lease
    def _acquire(self) -> Lease | None:
        ttl = timedelta(seconds=self.cfg.scheduler.lease_ttl_seconds)
        with transaction(self.c.session_factory) as s:
            lease = LeaseRepository(s).acquire(SCHEDULER_LEASE, self.owner, ttl)
        if lease is not None and (self.lease is None or self.lease.token != lease.token):
            log_event(_log, "scheduler.lease_acquired", owner=self.owner, token=lease.token)
            with transaction(self.c.session_factory) as s:
                ScheduledRunRepository(s).abandon_stale(
                    self.c.clock() - timedelta(minutes=self.cfg.scheduler.stale_run_minutes),
                    self.c.clock(),
                )
        if lease is None and self.lease is not None:
            log_event(_log, "scheduler.lease_lost", logging.WARNING, owner=self.owner)
        self.lease = lease
        return lease

    def _still_leader(self) -> bool:
        if self.lease is None:
            return False
        with transaction(self.c.session_factory) as s:
            return LeaseRepository(s).holds(SCHEDULER_LEASE, self.owner, self.lease.token)

    def release(self) -> None:
        if self.lease is not None:
            with transaction(self.c.session_factory) as s:
                LeaseRepository(s).release(SCHEDULER_LEASE, self.owner)
            log_event(_log, "scheduler.lease_released", owner=self.owner)
            self.lease = None

    # -- scheduling
    def _due(self, task: str, now: datetime) -> bool:
        tcfg = self.cfg.scheduler.tasks.get(task)
        if tcfg is None or not tcfg.enabled:
            return False
        with transaction(self.c.session_factory) as s:
            repo = ScheduledRunRepository(s)
            last = repo.last(task, ("succeeded", "failed", "skipped"))
            fails = repo.consecutive_failures(task) if last and last.status == "failed" else 0
        if last is None:
            return True
        wait = tcfg.interval
        if fails:
            backoff = timedelta(seconds=self.cfg.scheduler.failure_retry_seconds * 2 ** (fails - 1))
            wait = min(wait, backoff)
        return now - last.started_at >= wait

    def _allowed(self, stage: Stage, decision: ModeDecision) -> tuple[bool, str]:
        if not stage.requires:
            return True, ""
        if self.synthetic:
            ok = decision.allows(C.SYNTHETIC_DEMO)
            return ok, "" if ok else "synthetic demo capability withdrawn"
        for cap in stage.requires:
            if decision.allows(cap):
                return True, ""
        reasons = [decision.capabilities[c].reason for c in stage.requires]
        return False, "; ".join(reasons)

    async def tick(self) -> TickReport:
        now = self.c.clock()
        lease = self._acquire()
        if lease is None:
            self.heartbeat.beat(details={"leader": False})
            return TickReport(leader=False)
        snapshot = await asyncio.to_thread(self.system.snapshot)
        decision = snapshot.decision
        report = TickReport(leader=True, token=lease.token, mode=decision.mode.value)
        self.heartbeat.beat(
            details={"leader": True, "token": lease.token, "mode": decision.mode.value}
        )
        for stage in self.stages:
            if not self._due(stage.name, now):
                continue
            if not self._still_leader():  # fencing: never run work after losing the lease
                log_event(_log, "scheduler.fenced", logging.WARNING, task=stage.name)
                break
            report.ran[stage.name] = await self._run_stage(stage, decision, lease, snapshot)
        return report

    async def _run_stage(
        self, stage: Stage, decision: ModeDecision, lease: Lease, snapshot: Any
    ) -> str:
        cid = f"sched-{stage.name}-{uuid.uuid4().hex[:12]}"
        factory = self.c.session_factory
        tcfg = self.cfg.scheduler.tasks[stage.name]
        allowed, why = self._allowed(stage, decision)
        if not allowed:
            with transaction(factory) as s:
                ScheduledRunRepository(s).skipped(
                    task=stage.name, owner=self.owner, token=lease.token, correlation_id=cid,
                    mode=decision.mode.value, now=self.c.clock(), reason=why,
                )  # fmt: skip
            log_event(_log, "scheduler.stage_skipped", task=stage.name, reason=why)
            return "skipped"
        with transaction(factory) as s:
            run_id = ScheduledRunRepository(s).start(
                task=stage.name, owner=self.owner, token=lease.token, correlation_id=cid,
                mode=decision.mode.value, now=self.c.clock(),
            )  # fmt: skip
        ctx = StageContext(self.c, decision, self.synthetic, cid, tcfg.max_per_run)
        started = time.perf_counter()
        status, summary, error = "succeeded", {}, None
        with correlation(cid):
            try:
                if stage.name == "ops_checks":
                    summary = await asyncio.to_thread(self._ops_checks, snapshot)
                else:
                    summary = await asyncio.wait_for(
                        self._invoke(stage, ctx), timeout=tcfg.timeout_seconds
                    )
            except TimeoutError:
                status, error = "failed", f"timed out after {tcfg.timeout_seconds}s"
            except Exception as exc:
                status, error = "failed", redact_text(f"{type(exc).__name__}: {exc}")
        summary = {**summary, "seconds": round(time.perf_counter() - started, 3)}
        with transaction(factory) as s:
            repo = ScheduledRunRepository(s)
            repo.finish(run_id, status=status, now=self.c.clock(), summary=summary, error=error)
            fails = repo.consecutive_failures(stage.name)
        log_event(
            _log,
            "scheduler.stage_finished",
            logging.INFO if status == "succeeded" else logging.ERROR,
            task=stage.name, status=status, correlation_id=cid, error=error,
        )  # fmt: skip
        self._stage_alerts(stage.name, status, error, fails, cid)
        return status

    async def _invoke(self, stage: Stage, ctx: StageContext) -> dict[str, Any]:
        if inspect.iscoroutinefunction(stage.run):
            return await stage.run(ctx)  # type: ignore[no-any-return]
        out = await asyncio.to_thread(stage.run, ctx)
        if inspect.isawaitable(out):
            return await out
        return out

    def _stage_alerts(
        self, task: str, status: str, error: str | None, fails: int, cid: str
    ) -> None:
        threshold = self.cfg.alerts.stage_failure_threshold
        if status == "failed":
            self.alerter.raise_alert(
                "stage_failed",
                task,
                severity="critical" if fails >= threshold else "warning",
                title=f"Scheduled stage {task} failed ({fails} consecutive)",
                detail=error or "unknown error",
                details={"correlation_id": cid, "consecutive_failures": fails},
            )
            if fails >= threshold:
                with transaction(self.c.session_factory) as s:
                    DeadLetterRepository(s).record(
                        source="scheduled_stage",
                        reference=task,
                        error_type="repeated_failure",
                        error=error or "unknown error",
                        now=self.c.clock(),
                        payload={"consecutive_failures": fails, "correlation_id": cid},
                    )
        elif status == "succeeded":
            self.alerter.resolve("stage_failed", task)

    def _ops_checks(self, snapshot: Any) -> dict[str, Any]:
        decision: ModeDecision = snapshot.decision
        change = self.system.record(decision, self.alerter)
        active: set[str] = set()
        budgets = snapshot.budgets
        if budgets is not None:
            for line in budgets.lines:
                if line.state in (BudgetState.WARNING, BudgetState.EXHAUSTED):
                    key = f"{line.name}:{line.state.value}"
                    active.add(key)
                    self.alerter.raise_alert(
                        "budget",
                        key,
                        severity="critical" if line.state is BudgetState.EXHAUSTED else "warning",
                        title=f"{line.name} {line.state.value.lower()}",
                        detail=f"{line.used:.4f} of {line.limit:.2f} {line.unit} used "
                        f"({line.window})",
                    )
        self.alerter.resolve_missing("budget", active)
        now = self.c.clock()
        missing_after = timedelta(minutes=self.cfg.alerts.worker_missing_minutes)
        with transaction(self.c.session_factory) as s:
            hb = HeartbeatRepository(s)
            workers = hb.live("worker", now)
            ever = hb.all("worker")
            open_dead = DeadLetterRepository(s).count_open()
        collects = decision.allows(C.COLLECT_REAL_DATA) or self.synthetic
        last_seen = max((b.last_seen_at for b in ever), default=None)
        if collects and not workers and (last_seen is None or now - last_seen > missing_after):
            self.alerter.raise_alert(
                "workers_missing",
                "worker",
                severity="critical",
                title="No live worker",
                detail="collection jobs and the detection queue are not being processed"
                + (f"; last worker seen {last_seen.isoformat()}" if last_seen else ""),
            )
        elif workers:
            self.alerter.resolve("workers_missing", "worker")
        if sum(open_dead.values()):
            self.alerter.raise_alert(
                "dead_letters",
                "open",
                severity="warning",
                title=f"{sum(open_dead.values())} unresolved dead letter(s)",
                detail=", ".join(f"{k}: {v}" for k, v in sorted(open_dead.items())),
            )
        else:
            self.alerter.resolve("dead_letters", "open")
        return {
            "mode": decision.mode.value,
            "base_mode": decision.base_mode.value,
            "mode_changed": change is not None,
            "budget_alerts": sorted(active),
            "live_workers": len(workers),
            "open_dead_letters": open_dead,
        }

    async def run(self, *, max_ticks: int | None = None, install_signals: bool = True) -> int:
        from trend_agent.monitoring.scheduler import SchedulerLoop

        loop = SchedulerLoop(
            self.tick, interval=timedelta(seconds=self.cfg.scheduler.tick_seconds),
            name="system.scheduler",
        )  # fmt: skip
        if install_signals:
            loop.install_signal_handlers()
        try:
            return await loop.run(max_ticks=max_ticks)
        finally:
            self.release()
            self.heartbeat.stop()


# ---------------------------------------------------------------------- worker
class WorkerLimitError(RuntimeError):
    pass


class PipelineWorker:
    def __init__(
        self,
        container: AppContainer,
        cfg: OperationsConfig,
        *,
        worker_id: str | None = None,
        synthetic: bool = False,
    ) -> None:
        self.c = container
        self.cfg = cfg
        self.synthetic = synthetic
        self.heartbeat = Heartbeat(
            container.session_factory,
            role="worker",
            interval=timedelta(seconds=cfg.worker.tick_seconds),
            clock=container.clock,
            service_id=worker_id,
        )
        self.worker_id = self.heartbeat.service_id
        self.monitoring = container.monitoring(include_synthetic=synthetic or None)
        self.executor = self.monitoring.executor(self.worker_id)
        self.detection = container.detection(include_synthetic=synthetic or None)
        self.totals = {"jobs": 0, "series": 0, "ticks": 0, "errors": 0}

    def check_capacity(self) -> None:
        with transaction(self.c.session_factory) as s:
            live = [
                b
                for b in HeartbeatRepository(s).live("worker", self.c.clock())
                if b.service_id != self.worker_id
            ]
        limit = self.c.settings.max_concurrent_workers
        if len(live) >= limit:
            raise WorkerLimitError(
                f"{len(live)} worker(s) already live; MAX_CONCURRENT_WORKERS={limit}"
            )

    async def tick(self) -> dict[str, Any]:
        cid = f"worker-{uuid.uuid4().hex[:12]}"
        out: dict[str, Any] = {"jobs": 0, "series": 0}
        with correlation(cid):
            try:
                reports = await self.executor.drain(max_batches=self.cfg.worker.max_job_batches)
                out["jobs"] = sum(r.claimed for r in reports)
            except Exception as exc:
                self.totals["errors"] += 1
                out["jobs_error"] = redact_text(f"{type(exc).__name__}: {exc}")
                log_event(_log, "worker.jobs_failed", logging.ERROR, error=out["jobs_error"])
            try:
                from trend_agent.schemas.detection import RunType

                det = await asyncio.to_thread(
                    self.detection.run,
                    run_type=RunType.WORKER,
                    from_queue=True,
                    worker=self.worker_id,
                )
                out["series"] = det.series_evaluated
            except Exception as exc:
                self.totals["errors"] += 1
                out["detection_error"] = redact_text(f"{type(exc).__name__}: {exc}")
                log_event(
                    _log, "worker.detection_failed", logging.ERROR, error=out["detection_error"]
                )
        self.totals["ticks"] += 1
        self.totals["jobs"] += out["jobs"]
        self.totals["series"] += out["series"]
        self.heartbeat.beat(details=dict(self.totals))
        return out

    async def run(self, *, max_ticks: int | None = None, install_signals: bool = True) -> int:
        from trend_agent.monitoring.scheduler import SchedulerLoop

        self.check_capacity()
        self.heartbeat.beat(details=dict(self.totals))
        loop = SchedulerLoop(
            self.tick, interval=timedelta(seconds=self.cfg.worker.tick_seconds),
            name="system.worker",
        )  # fmt: skip
        if install_signals:
            loop.install_signal_handlers()
        try:
            return await loop.run(max_ticks=max_ticks)
        finally:
            self.heartbeat.stop()
