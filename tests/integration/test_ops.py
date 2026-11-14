"""Phase 8 operations against the real test database: leases and fencing, heartbeats,
deduplicated alerts, dead letters, the pipeline scheduler and worker, measured spend,
backups with a restore drill, and the health endpoints with a live container."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.helpers import TEST_DB_URL, MutableClock, make_settings
from trend_agent.api.app import create_app
from trend_agent.container import AppContainer, build_container
from trend_agent.db.ops_models import ScheduledRunRow, SchedulerLeaseRow
from trend_agent.db.repositories.ops import (
    DeadLetterRepository,
    HeartbeatRepository,
    LeaseRepository,
    OpsAlertRepository,
    ScheduledRunRepository,
    SystemStateRepository,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.ops.alerts import OpsAlerter
from trend_agent.ops.budgets import measure_spend
from trend_agent.ops.config import OperationsConfig
from trend_agent.ops.health import Heartbeat
from trend_agent.ops.modes import SystemCapability
from trend_agent.ops.pipeline import (
    PipelineScheduler,
    PipelineWorker,
    Stage,
    StageContext,
    WorkerLimitError,
)
from trend_agent.ops.system import MODE_STATE_KEY, SystemService
from trend_agent.providers.quota import InMemoryQuotaStore

pytestmark = pytest.mark.db
NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(NOW)


@pytest.fixture
def container(session_factory: SessionFactory, clock: MutableClock) -> Any:
    c = build_container(make_settings(), clock=clock, quota_store=InMemoryQuotaStore())
    yield c
    c.engine.dispose()


# ---------------------------------------------------------------------- leases
def test_lease_single_owner_renewal_takeover_and_fencing(session_factory: SessionFactory) -> None:
    ttl = timedelta(seconds=60)
    with transaction(session_factory) as s:
        a = LeaseRepository(s).acquire("pipeline", "A", ttl)
    with transaction(session_factory) as s:
        assert LeaseRepository(s).acquire("pipeline", "B", ttl) is None  # held by A
    with transaction(session_factory) as s:
        again = LeaseRepository(s).acquire("pipeline", "A", ttl)
    assert a is not None
    assert again is not None
    assert again.token == a.token == 1
    # A crashes: its lease expires; B takes over with a new fencing token
    with transaction(session_factory) as s:
        s.execute(
            sa.update(SchedulerLeaseRow).values(
                expires_at=sa.func.clock_timestamp() - sa.text("interval '1 second'")
            )
        )
    with transaction(session_factory) as s:
        b = LeaseRepository(s).acquire("pipeline", "B", ttl)
    assert b is not None
    assert b.token == 2
    with transaction(session_factory) as s:
        repo = LeaseRepository(s)
        assert not repo.holds("pipeline", "A", 1)  # A's stale work is fenced off
        assert repo.holds("pipeline", "B", 2)
        assert repo.release("pipeline", "B")
        assert not repo.holds("pipeline", "B", 2)


def test_concurrent_acquisition_has_exactly_one_winner(session_factory: SessionFactory) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def grab(owner: str) -> bool:
        with transaction(session_factory) as s:
            return LeaseRepository(s).acquire("race", owner, timedelta(seconds=30)) is not None

    with ThreadPoolExecutor(8) as pool:
        wins = list(pool.map(grab, [f"o{i}" for i in range(8)]))
    assert sum(wins) == 1


# ---------------------------------------------------------------------- heartbeats
def test_heartbeats_liveness_and_stop(session_factory: SessionFactory, clock: MutableClock) -> None:
    hb = Heartbeat(
        session_factory,
        role="worker",
        interval=timedelta(seconds=10),
        clock=clock,
        service_id="worker:test:1",
    )
    assert hb.beat(details={"jobs": 3})
    with transaction(session_factory) as s:
        live = HeartbeatRepository(s).live("worker", clock())
    assert [b.service_id for b in live] == ["worker:test:1"]
    clock.advance(timedelta(seconds=31))  # > 3 x interval without a beat
    with transaction(session_factory) as s:
        assert HeartbeatRepository(s).live("worker", clock()) == []
    hb.beat()
    hb.stop()
    with transaction(session_factory) as s:
        row = HeartbeatRepository(s).all("worker")[0]
        assert row.status == "stopped"
        assert HeartbeatRepository(s).live("worker", clock()) == []


# ---------------------------------------------------------------------- alerts
def test_alerts_deduplicate_cool_down_resolve_and_reopen(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    sent: list[str] = []

    class Channel:
        name, configured, external = "test", True, True

        def send(self, event: Any) -> None:
            sent.append(event.title)

    al = OpsAlerter(session_factory, clock=clock, cooldown=timedelta(hours=1), channels=[Channel()])  # type: ignore[list-item]
    first = al.raise_alert(
        "stage_failed", "forecasting", severity="warning", title="t", detail="boom key=SECRETVALUE"
    )
    assert first.notified
    assert first.new
    for _ in range(5):  # the same error recurring: recorded, not re-sent
        clock.advance(timedelta(minutes=5))
        again = al.raise_alert(
            "stage_failed", "forecasting", severity="warning", title="t", detail="boom"
        )
        assert not again.notified
    clock.advance(timedelta(hours=1))
    assert al.raise_alert(
        "stage_failed", "forecasting", severity="warning", title="t", detail="boom"
    ).notified
    assert al.resolve("stage_failed", "forecasting")
    reopened = al.raise_alert(
        "stage_failed", "forecasting", severity="critical", title="t", detail="boom"
    )
    assert reopened.notified
    assert not reopened.new
    assert len(sent) == 3
    with transaction(session_factory) as s:
        row = OpsAlertRepository(s).open()[0]
        assert row.occurrences == 8
        assert row.notifications_sent == 3
        assert "SECRETVALUE" not in row.detail  # stored text is redacted


def test_dead_letters_upsert_and_resolve(session_factory: SessionFactory) -> None:
    with transaction(session_factory) as s:
        repo = DeadLetterRepository(s)
        for i in range(3):
            repo.record(
                source="collection_job",
                reference="job-1",
                error_type="capability",
                error=f"attempt {i} token=abc123",
                now=NOW,
                payload={"api_key": "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE1234"},
            )
    with transaction(session_factory) as s:
        repo = DeadLetterRepository(s)
        (row,) = repo.open()
        assert row.attempts == 3
        assert "abc123" not in row.error
        assert "AIza" not in str(row.payload)
        assert repo.count_open() == {"collection_job": 1}
        assert repo.resolve(row.id, "provider fixed", NOW)
        assert repo.open() == []


# ---------------------------------------------------------------------- scheduler
def _ops_cfg(**tasks: int) -> OperationsConfig:
    base = OperationsConfig()
    t = dict(base.scheduler.tasks)
    for name, interval in tasks.items():
        t[name] = t[name].model_copy(update={"interval_seconds": interval})
    return base.model_copy(update={"scheduler": base.scheduler.model_copy(update={"tasks": t})})


def test_scheduler_leader_runs_stages_once_standby_waits(container: AppContainer) -> None:
    calls: list[str] = []

    def ok(ctx: StageContext) -> dict[str, Any]:
        calls.append(ctx.correlation_id)
        return {"ok": True}

    stages = (Stage("ops_checks", ok), Stage("forecasting", ok))
    a = PipelineScheduler(container, _ops_cfg(), owner="sched-A", stages=stages)
    b = PipelineScheduler(container, _ops_cfg(), owner="sched-B", stages=stages)
    ra = asyncio.run(a.tick())
    rb = asyncio.run(b.tick())
    assert ra.leader
    assert not rb.leader
    assert ra.ran == {"ops_checks": "succeeded", "forecasting": "succeeded"}
    assert rb.ran == {}
    # not due again until its interval has passed
    assert asyncio.run(a.tick()).ran == {}
    with transaction(container.session_factory) as s:
        runs = ScheduledRunRepository(s).recent()
        assert {r.task for r in runs} == {"ops_checks", "forecasting"}
        assert all(r.owner == "sched-A" and r.fencing_token == 1 for r in runs)
        assert all(r.correlation_id.startswith("sched-") for r in runs)
        # the ops check recorded the operating mode (never silently)
        assert SystemStateRepository(s).get(MODE_STATE_KEY) is not None
        live = HeartbeatRepository(s).live("scheduler", container.clock())
        assert {h.service_id for h in live} == {"sched-A", "sched-B"}
    a.release()
    assert asyncio.run(b.tick()).leader  # the standby takes over after release


def test_failing_stage_is_recorded_alerted_retried_and_dead_lettered(
    container: AppContainer, clock: MutableClock
) -> None:
    def boom(ctx: StageContext) -> dict[str, Any]:
        raise RuntimeError("provider exploded token=SECRET")

    stages = (Stage("forecasting", boom),)
    sched = PipelineScheduler(container, _ops_cfg(forecasting=3600), owner="s", stages=stages)
    for _ in range(3):
        assert asyncio.run(sched.tick()).ran == {"forecasting": "failed"}
        clock.advance(timedelta(minutes=10))  # failure backoff (60s, 120s, ...) has passed
    with transaction(container.session_factory) as s:
        runs = [r for r in ScheduledRunRepository(s).recent() if r.task == "forecasting"]
        assert [r.status for r in runs] == ["failed"] * 3
        assert all("SECRET" not in (r.error or "") for r in runs)
        (alert,) = [a for a in OpsAlertRepository(s).open() if a.kind == "stage_failed"]
        assert alert.severity == "critical"
        assert alert.occurrences == 3
        assert DeadLetterRepository(s).count_open() == {"scheduled_stage": 1}
    # recovery resolves the alert
    sched.stages = (Stage("forecasting", lambda ctx: {"ok": True}),)
    clock.advance(timedelta(hours=2))
    assert asyncio.run(sched.tick()).ran == {"forecasting": "succeeded"}
    with transaction(container.session_factory) as s:
        assert not [a for a in OpsAlertRepository(s).open() if a.kind == "stage_failed"]


def test_stage_is_skipped_when_the_mode_lacks_the_capability(container: AppContainer) -> None:
    stages = (
        Stage(
            "monitor_plan",
            lambda ctx: {"ran": True},
            (SystemCapability.DIRECT_SEARCH_SURGE_CLAIMS,),
        ),
    )
    sched = PipelineScheduler(container, _ops_cfg(), owner="s", stages=stages)
    assert asyncio.run(sched.tick()).ran == {"monitor_plan": "skipped"}
    with transaction(container.session_factory) as s:
        run = s.scalars(sa.select(ScheduledRunRow)).one()
        assert run.status == "skipped"
        assert "not part of" in run.summary["reason"]


def test_lost_lease_fences_further_stages(container: AppContainer) -> None:
    def steal(ctx: StageContext) -> dict[str, Any]:
        with transaction(ctx.container.session_factory) as s:
            s.execute(sa.update(SchedulerLeaseRow).values(owner="intruder", fencing_token=99))
        return {}

    stages = (Stage("monitor_plan", steal), Stage("forecasting", lambda ctx: {"ran": True}))
    sched = PipelineScheduler(container, _ops_cfg(), owner="s", stages=stages)
    rep = asyncio.run(sched.tick())
    assert rep.ran == {"monitor_plan": "succeeded"}
    assert "forecasting" not in rep.ran  # never runs after the lease was lost


# ---------------------------------------------------------------------- worker
def test_worker_refuses_to_exceed_max_concurrent_workers(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    c = build_container(
        make_settings(max_concurrent_workers=1), clock=clock, quota_store=InMemoryQuotaStore()
    )
    try:
        Heartbeat(
            session_factory,
            role="worker",
            interval=timedelta(seconds=15),
            clock=clock,
            service_id="worker:other",
        ).beat()
        w = PipelineWorker(c, OperationsConfig(), worker_id="worker:new")
        with pytest.raises(WorkerLimitError, match="MAX_CONCURRENT_WORKERS=1"):
            w.check_capacity()
        out = asyncio.run(w.tick())  # an empty queue is fine and heartbeats
        assert out["jobs"] == 0
        with transaction(session_factory) as s:
            assert {h.service_id for h in HeartbeatRepository(s).all("worker")} == {
                "worker:other",
                "worker:new",
            }
    finally:
        c.engine.dispose()


# ---------------------------------------------------------------------- spend
def test_spend_is_measured_from_recorded_runs_and_quota(session_factory: SessionFactory) -> None:
    run = sa.text(
        "INSERT INTO recommendation_generation_runs (id, started_at, status, trigger, "
        "profile_id, generator, recommendation_version, configuration_version, "
        "estimated_cost_usd) VALUES (:id, :t, 'completed', 'cli', 'default', 'llm', 'v', "
        "'c', :usd)"
    )
    quota = sa.text(
        "INSERT INTO provider_rate_limits (provider_name, bucket, window_start, window_end, "
        "effective_limit, units_used) VALUES ('youtube_data_api', 'default', :a, :b, 9000, "
        "350)"
    )
    with transaction(session_factory) as s:
        s.execute(run, {"id": uuid.uuid4(), "t": NOW - timedelta(hours=1), "usd": 2.5})
        s.execute(run, {"id": uuid.uuid4(), "t": NOW - timedelta(days=3), "usd": 4.0})
        s.execute(quota, {"a": NOW - timedelta(hours=5), "b": NOW + timedelta(hours=19)})
    spend = measure_spend(session_factory, NOW)
    assert spend.llm_usd_today == pytest.approx(2.5)
    assert spend.llm_usd_month == pytest.approx(6.5)
    assert spend.api_units_today == 350


# ---------------------------------------------------------------------- backups
@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="PostgreSQL client tools missing")
def test_backup_and_restore_drill(session_factory: SessionFactory, tmp_path: Path) -> None:
    from trend_agent.ops.backup import backup, restore_drill

    with transaction(session_factory) as s:
        DeadLetterRepository(s).record(
            source="x", reference="r", error_type="e", error="m", now=NOW
        )
    res = backup(TEST_DB_URL, tmp_path)
    assert res.bytes > 0
    assert res.schema_revision is not None
    assert res.table_counts["dead_letters"] == 1
    drill = restore_drill(TEST_DB_URL, Path(res.path))
    assert drill.ok, drill.mismatches
    assert drill.tables_compared >= 70
    assert drill.scratch_database.endswith("_restore_drill")
    # a corrupted backup is rejected by its checksum
    Path(res.path).write_bytes(Path(res.path).read_bytes()[:-10] + b"0123456789")
    from trend_agent.ops.backup import BackupError

    with pytest.raises(BackupError, match="checksum"):
        restore_drill(TEST_DB_URL, Path(res.path))


# ---------------------------------------------------------------------- API with ops
def test_health_endpoints_with_a_live_container(container: AppContainer) -> None:
    app = create_app(container.dashboard_backend(), container.settings, ops=container)
    c = TestClient(app)
    ready = c.get("/health/ready")
    assert ready.status_code == 200, ready.json()
    db = c.get("/health/database").json()
    assert db["schema_revision"] == db["expected_revision"]
    workers = c.get("/health/workers")
    assert workers.status_code == 503  # nothing is running in the test
    mode = c.get("/api/system/mode").json()
    assert mode["direct_search_claims_enabled"] is False
    assert mode["mode"] in ("DEGRADED", "PROXY_OPPORTUNITY_RESEARCH", "DEMONSTRATION")
    text = c.get("/metrics").text
    assert "trend_agent_operating_mode" in text
    assert "trend_agent_budget" in text


def test_generation_is_refused_when_the_mode_withdraws_it(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trend_agent.ops.modes import Degradation, DegradationFact, decide_mode

    settings = make_settings(api_token="t" * 40)
    app = create_app(container.dashboard_backend(), settings, ops=container)
    snap = SystemService(container).snapshot()
    blocked = decide_mode(
        snap.readiness,
        now=NOW,
        degradations=[DegradationFact(Degradation.DATABASE_UNAVAILABLE, "simulated")],
    )
    monkeypatch.setattr(
        app.state.snapshots,
        "get",
        lambda: type(snap)(blocked, snap.readiness, None, snap.validation, []),
    )
    r = TestClient(app).post(
        "/api/recommendations/generate",
        json={"all": True},
        headers={"Authorization": "Bearer " + "t" * 40},
    )
    assert r.status_code == 503
    assert "recommendations is not available" in r.json()["detail"]
