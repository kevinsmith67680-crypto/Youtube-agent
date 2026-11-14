"""E2E scenarios 16-25: the production process model under failure - leader election and
takeover, crash recovery, isolation of failing and hanging stages, alert deduplication,
worker liveness and capacity, graceful shutdown on SIGTERM, a fault injected into one
collection job, and dead-letter resolution through the CLI."""

from __future__ import annotations

import asyncio
import os
import random
import signal
import time
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.e2e.conftest import AUTH, make_container
from tests.helpers import MutableClock
from tests.integration.test_monitoring import World
from tests.integration.test_monitoring import world as world
from trend_agent import cli
from trend_agent.api.app import create_app
from trend_agent.container import AppContainer
from trend_agent.db.ops_models import ScheduledRunRow, SchedulerLeaseRow
from trend_agent.db.repositories.ops import (
    DeadLetterRepository,
    HeartbeatRepository,
    LeaseRepository,
    OpsAlertRepository,
    ScheduledRunRepository,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.ops.alerts import OpsAlerter
from trend_agent.ops.config import OperationsConfig
from trend_agent.ops.health import SCHEDULER_LEASE, Heartbeat
from trend_agent.ops.pipeline import PipelineScheduler, PipelineWorker, Stage, StageContext
from trend_agent.schemas.monitoring import JobStatus

pytestmark = pytest.mark.db


def _cfg(**intervals: int) -> OperationsConfig:
    cfg = OperationsConfig()
    tasks = dict(cfg.scheduler.tasks)
    for k, v in intervals.items():
        tasks[k] = tasks[k].model_copy(update={"interval_seconds": v})
    return cfg.model_copy(update={"scheduler": cfg.scheduler.model_copy(update={"tasks": tasks})})


def _ok(ctx: StageContext) -> dict[str, Any]:
    return {"ok": True}


# 16
def test_s16_standby_scheduler_takes_over_after_the_leader_dies(container: AppContainer) -> None:
    a = PipelineScheduler(container, _cfg(), owner="leader", stages=(Stage("forecasting", _ok),))
    b = PipelineScheduler(container, _cfg(), owner="standby", stages=(Stage("forecasting", _ok),))
    assert asyncio.run(a.tick()).leader
    assert not asyncio.run(b.tick()).leader
    # the leader dies without releasing: its lease simply expires
    with transaction(container.session_factory) as s:
        s.execute(
            sa.update(SchedulerLeaseRow).values(
                expires_at=sa.func.clock_timestamp() - sa.text("interval '1 second'")
            )
        )
    rep = asyncio.run(b.tick())
    assert rep.leader
    assert rep.token == 2  # new fencing token
    with transaction(container.session_factory) as s:
        assert not LeaseRepository(s).holds(SCHEDULER_LEASE, "leader", 1)


# 17
def test_s17_a_new_leader_closes_runs_abandoned_by_a_crashed_scheduler(
    container: AppContainer, clock: MutableClock
) -> None:
    with transaction(container.session_factory) as s:
        ScheduledRunRepository(s).start(
            task="forecasting",
            owner="crashed",
            token=1,
            correlation_id="sched-x",
            mode="DEGRADED",
            now=clock() - timedelta(hours=5),
        )
    sched = PipelineScheduler(container, _cfg(), owner="new", stages=())
    asyncio.run(sched.tick())
    with transaction(container.session_factory) as s:
        run = s.scalars(sa.select(ScheduledRunRow).where(ScheduledRunRow.owner == "crashed")).one()
        assert run.status == "failed"
        assert "abandoned" in (run.error or "")


# 18
def test_s18_one_failing_stage_does_not_stop_the_others(container: AppContainer) -> None:
    def boom(ctx: StageContext) -> dict[str, Any]:
        raise ValueError("malformed upstream payload")

    stages = (Stage("monitor_plan", boom), Stage("forecasting", _ok), Stage("retention", _ok))
    rep = asyncio.run(PipelineScheduler(container, _cfg(), owner="s", stages=stages).tick())
    assert rep.ran == {
        "monitor_plan": "failed",
        "forecasting": "succeeded",
        "retention": "succeeded",
    }


# 19
def test_s19_a_hanging_stage_times_out(container: AppContainer) -> None:
    async def hang(ctx: StageContext) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    cfg = _cfg()
    tasks = dict(cfg.scheduler.tasks)
    tasks["forecasting"] = tasks["forecasting"].model_copy(update={"timeout_seconds": 5})
    cfg = cfg.model_copy(update={"scheduler": cfg.scheduler.model_copy(update={"tasks": tasks})})
    sched = PipelineScheduler(container, cfg, owner="s", stages=(Stage("forecasting", hang),))
    started = time.perf_counter()
    assert asyncio.run(sched.tick()).ran == {"forecasting": "failed"}
    assert time.perf_counter() - started < 15
    with transaction(container.session_factory) as s:
        run = ScheduledRunRepository(s).last("forecasting")
        assert run is not None
        assert "timed out" in (run.error or "")


# 20
def test_s20_recurring_failures_notify_once_per_cooldown(
    container: AppContainer, clock: MutableClock
) -> None:
    sent: list[str] = []

    class Channel:
        name, configured, external = "pager", True, True

        def send(self, event: Any) -> None:
            sent.append(event.title)

    def boom(ctx: StageContext) -> dict[str, Any]:
        raise RuntimeError("upstream 500")

    channels: list[Any] = [Channel()]
    alerter = OpsAlerter(
        container.session_factory, clock=clock, cooldown=timedelta(hours=1), channels=channels
    )
    sched = PipelineScheduler(
        container,
        _cfg(forecasting=60),
        owner="s",
        stages=(Stage("forecasting", boom),),
        alerter=alerter,
    )
    for _ in range(6):
        asyncio.run(sched.tick())
        clock.advance(timedelta(minutes=5))
    with transaction(container.session_factory) as s:
        (alert,) = [a for a in OpsAlertRepository(s).open() if a.kind == "stage_failed"]
        assert alert.occurrences == 6
    assert len(sent) == 1  # six failures, one notification within the cooldown


# 21
def test_s21_stale_workers_are_reported_by_health_and_alerted(
    container: AppContainer, clock: MutableClock
) -> None:
    Heartbeat(
        container.session_factory,
        role="worker",
        interval=timedelta(seconds=15),
        clock=clock,
        service_id="worker:old",
    ).beat()
    clock.advance(timedelta(minutes=30))
    app = create_app(container.dashboard_backend(), container.settings, ops=container)
    r = TestClient(app).get("/health/workers", headers=AUTH)
    assert r.status_code == 503
    body = r.json()
    assert body["live"] == {}
    assert any(s["status"] == "stale" for s in body["services"])
    sched = PipelineScheduler(container, _cfg(), owner="s", synthetic=True)
    sched.stages = tuple(s for s in sched.stages if s.name == "ops_checks")
    asyncio.run(sched.tick())
    with transaction(container.session_factory) as s:
        assert any(a.kind == "workers_missing" for a in OpsAlertRepository(s).open())


# 22
def test_s22_worker_capacity_is_enforced(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = make_container(clock, max_concurrent_workers=2)
    try:
        for i in range(2):
            Heartbeat(
                session_factory,
                role="worker",
                interval=timedelta(seconds=15),
                clock=clock,
                service_id=f"worker:{i}",
            ).beat()
        w = PipelineWorker(c, OperationsConfig(), worker_id="worker:third")
        with pytest.raises(Exception, match="MAX_CONCURRENT_WORKERS=2"):
            asyncio.run(w.run(max_ticks=1, install_signals=False))
    finally:
        c.engine.dispose()


# 23
async def test_s23_sigterm_stops_the_scheduler_gracefully(container: AppContainer) -> None:
    sched = PipelineScheduler(
        container, _cfg(), owner="graceful", stages=(Stage("retention", _ok),)
    )
    sched.cfg = sched.cfg.model_copy(
        update={"scheduler": sched.cfg.scheduler.model_copy(update={"tick_seconds": 1.0})}
    )
    task = asyncio.create_task(sched.run(install_signals=True))
    for _ in range(100):
        await asyncio.sleep(0.1)
        with transaction(container.session_factory) as s:
            if ScheduledRunRepository(s).last("retention") is not None:
                break
    os.kill(os.getpid(), signal.SIGTERM)  # handled by the scheduler loop, not the process
    ticks = await asyncio.wait_for(task, timeout=20)
    assert ticks >= 1
    with transaction(container.session_factory) as s:
        hb = HeartbeatRepository(s).all("scheduler")[0]
        assert hb.status == "stopped"
        lease = LeaseRepository(s).get(SCHEDULER_LEASE)
        assert lease is not None
        assert not LeaseRepository(s).holds(SCHEDULER_LEASE, "graceful", lease.fencing_token)


# 24
async def test_s24_a_fault_in_one_collection_job_does_not_stop_the_others(world: World) -> None:
    world.svc.sync_targets()
    world.svc.plan()
    targets = world.container.registry
    provider = targets.get("synthetic_trends")
    original = provider.fetch_search_interest  # type: ignore[attr-defined]
    calls = {"n": 0}

    async def flaky(request: Any, ctx: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("malformed record from provider")
        return await original(request, ctx)

    provider.fetch_search_interest = flaky  # type: ignore[attr-defined]
    reports = await world.svc.executor("w", rng=random.Random(1)).drain()
    outcomes = [o for r in reports for o in r.outcomes]
    statuses = [o.status for o in outcomes]
    assert len(outcomes) > 1
    assert JobStatus.RETRY_WAIT in statuses  # the faulty job is retried later
    assert any(s in (JobStatus.SUCCEEDED, JobStatus.NO_NEW_DATA) for s in statuses)


# 25
def test_s25_dead_letters_are_listed_and_resolved_from_the_cli(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, clock: MutableClock
) -> None:
    with transaction(container.session_factory) as s:
        DeadLetterRepository(s).record(
            source="collection_job",
            reference="job-9",
            error_type="capability",
            error="unsupported",
            now=clock(),
        )
        dl_id = DeadLetterRepository(s).open()[0].id
    monkeypatch.setattr(cli, "CONTAINER_FACTORY", lambda settings=None, **kw: make_container(clock))
    runner = CliRunner()
    out = runner.invoke(cli.app, ["system", "dead-letters", "--json"])
    assert out.exit_code == 0, out.output
    assert "job-9" in out.output
    out = runner.invoke(
        cli.app, ["system", "dead-letters", "--resolve", str(dl_id), "--note", "provider fixed"]
    )
    assert out.exit_code == 0, out.output
    with transaction(container.session_factory) as s:
        assert DeadLetterRepository(s).open() == []
