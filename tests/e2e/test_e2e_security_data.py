"""E2E scenarios 26-40: security boundaries of the API and the production guard, secret
hygiene, and data integrity - synthetic isolation, contamination detection, label
provenance, backtest validity, readiness honesty, backups, the synthetic pipeline end to
end, and the evaluation CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.e2e.conftest import AUTH, TOKEN, make_container
from tests.helpers import TEST_DB_URL, MutableClock
from tests.integration.test_monitoring import World
from tests.integration.test_monitoring import world as world
from trend_agent import cli
from trend_agent.api.app import create_app
from trend_agent.container import AppContainer
from trend_agent.db.repositories.ops import ScheduledRunRepository, SystemStateRepository
from trend_agent.db.session import transaction
from trend_agent.evaluation import integrity, replay
from trend_agent.evaluation.labels import import_file
from trend_agent.logging import configure_logging, log_event
from trend_agent.ops.config import OperationsConfig
from trend_agent.ops.pipeline import PipelineScheduler, PipelineWorker
from trend_agent.ops.readiness import RESTORE_DRILL_KEY, ReleaseState, build_scorecard
from trend_agent.ops.system import SystemService

pytestmark = pytest.mark.db
runner = CliRunner()


def _client(c: AppContainer, **kw: Any) -> TestClient:
    return TestClient(create_app(c.dashboard_backend(), c.settings, ops=c, **kw))


# 26
def test_s26_mutations_and_private_data_need_the_token(container: AppContainer) -> None:
    c = _client(container)
    assert c.get("/api/profile").status_code == 401
    assert c.post("/api/recommendations/generate", json={"all": True}).status_code == 401
    bad = {"Authorization": "Bearer wrong-token-0000000000000000000000000"}
    assert c.get("/api/profile", headers=bad).status_code == 401
    assert c.get("/api/profile", headers=AUTH).status_code == 200


# 27
def test_s27_production_refuses_to_start_with_development_credentials(
    monkeypatch: pytest.MonkeyPatch, clock: MutableClock
) -> None:
    monkeypatch.setenv("TREND_AGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("TREND_AGENT_VERIFICATION_SIGNING_KEY", "k" * 40)
    monkeypatch.setenv("TREND_AGENT_DATABASE_URL", TEST_DB_URL)
    from trend_agent.config import Settings

    monkeypatch.setattr(cli, "SETTINGS_FACTORY", lambda: Settings(_env_file=None))
    for cmd in (["system", "scheduler", "--once"], ["system", "worker", "--once"]):
        out = runner.invoke(cli.app, cmd)
        assert out.exit_code == 2, out.output
        assert "refusing to start in production" in out.output


# 28
def test_s28_secrets_never_reach_logs_alerts_or_run_records(
    container: AppContainer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = logging.getLogger("trend_agent")
    saved = (list(root.handlers), root.level, root.propagate)
    key = "AIzaSyLEAKLEAKLEAKLEAKLEAKLEAKLEAK123"
    try:
        configure_logging("INFO", "json")
        log_event(
            logging.getLogger("trend_agent.e2e"),
            "provider.error",
            url=f"https://www.googleapis.com/youtube/v3/search?key={key}",
            api_key=key,
            detail=f"Bearer {TOKEN}",
        )
        err = capsys.readouterr().err
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        root.propagate = saved[2]
    assert key not in err
    assert TOKEN not in err
    from trend_agent.ops.pipeline import Stage

    def boom(ctx: Any) -> dict[str, Any]:
        raise RuntimeError(f"failed calling ?key={key}")

    asyncio.run(
        PipelineScheduler(
            container, OperationsConfig(), owner="s", stages=(Stage("forecasting", boom),)
        ).tick()
    )
    with transaction(container.session_factory) as s:
        run = ScheduledRunRepository(s).last("forecasting")
        assert run is not None
        assert key not in (run.error or "")
        dump = json.dumps(
            [str(r.detail) for r in s.execute(sa.text("SELECT detail FROM ops_alerts"))]
        )
        assert key not in dump


# 29
def test_s29_operational_detail_is_private_when_a_token_is_configured(
    container: AppContainer,
) -> None:
    c = _client(container)
    assert c.get("/health/live").status_code == 200
    assert c.get("/health/ready").status_code == 200
    for path in ("/metrics", "/health/workers", "/health/providers", "/api/system/mode"):
        assert c.get(path).status_code == 401, path


# 30
def test_s30_oversized_bodies_are_rejected(container: AppContainer) -> None:
    r = _client(container).post(
        "/api/profile",
        content=b"x" * (70 * 1024),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 413


# 31
def test_s31_rate_limits_return_retry_after(container: AppContainer) -> None:
    class Clock:
        t = 1000.0

        def __call__(self) -> float:
            return self.t

    c = _client(container, clock=Clock())
    codes = [c.get("/api/overview", headers=AUTH).status_code for _ in range(260)]
    assert 429 in codes
    r = c.get("/api/overview", headers=AUTH)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert c.get("/health/live").status_code == 200  # probes are never rate limited


# 32
def test_s32_cors_only_for_the_configured_origin(container: AppContainer) -> None:
    c = _client(container)
    evil = c.options(
        "/api/overview",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in {k.lower() for k in evil.headers}
    ok = c.options(
        "/api/overview",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"


# 33
def test_s33_synthetic_data_never_appears_as_live_intelligence(world: World) -> None:
    world.svc.sync_targets()
    world.svc.plan()
    asyncio.run(world.svc.executor("w").drain())
    c = world.container
    c.settings = c.settings.model_copy(update={"api_token": None})
    client = TestClient(create_app(c.dashboard_backend(), c.settings, ops=c))
    ov = client.get("/api/overview").json()
    assert ov["synthetic"] is False
    trends = client.get("/api/trends").json()
    assert all(not t.get("synthetic") for t in trends.get("items", []))
    with transaction(world.factory) as s:
        n = s.execute(
            sa.text("SELECT count(*) FROM ts_observations WHERE is_synthetic")
        ).scalar_one()
    assert n > 0  # synthetic data exists - it is just never served as live


# 34
def test_s34_contamination_is_blocked_by_the_schema_or_detected(world: World) -> None:
    assert integrity.check(world.factory)["status"] == "PASS"
    world.svc.sync_targets()
    world.svc.plan()
    asyncio.run(world.svc.executor("w").drain())
    # (a) relabelling synthetic observations as real is impossible: a check constraint ties
    #     is_synthetic to the synthetic measurement category
    with pytest.raises(sa.exc.IntegrityError), transaction(world.factory) as s:
        s.execute(sa.text("UPDATE ts_observations SET is_synthetic = false"))
    # (b) what the schema cannot express is caught by the integrity check: a synthetic
    #     opportunity derived from a real analysis run
    run_id = uuid.uuid4()
    with transaction(world.factory) as s:
        s.execute(
            sa.text(
                "INSERT INTO content_analysis_runs (id, trigger, canonical_query, status, "
                "started_at, model_version, analysis_version, configuration_version, "
                "inputs_fingerprint, "
                "demand_basis, is_synthetic) VALUES (:id, 'manual', 'q', 'completed', now(), 'm', "
                "'a', 'c', 'f', 'manual', false)"
            ),
            {"id": run_id},
        )
        s.execute(
            sa.text(
                "INSERT INTO content_opportunities (opportunity_key, canonical_query, "
                "score_version, status, verification_state, demand_basis, report, "
                "analysis_run_id, is_synthetic) "
                "VALUES (:k, 'q', 's', 'no_opportunity', 'unverified', 'manual', '{}', :r, true)"
            ),
            {"k": uuid.uuid4(), "r": run_id},
        )
    rep = integrity.check(world.factory)
    assert rep["status"] == "FAIL"
    assert rep["failed"] == {"real_opportunity_from_synthetic_analysis": 1}


# 35
def test_s35_labels_keep_provenance_and_never_come_from_the_future(
    container: AppContainer, tmp_path: Path
) -> None:
    lines = [
        {
            "kind": "surge_event",
            "subject_key": "target:1",
            "event_time": "2026-10-01T00:00:00Z",
            "known_at": "2026-10-05T00:00:00Z",
            "source": "Studio review",
            "labeler": "a",
        },
        {
            "kind": "surge_event",
            "subject_key": "target:2",
            "event_time": "2026-10-01T00:00:00Z",
            "known_at": "2026-09-01T00:00:00Z",
            "source": "x",
            "labeler": "a",
        },
        {
            "kind": "surge_event",
            "subject_key": "synthetic:demo",
            "event_time": "2026-10-01T00:00:00Z",
            "known_at": "2026-10-05T00:00:00Z",
            "source": "x",
            "labeler": "a",
        },
        {
            "kind": "surge_event",
            "subject_key": "target:3",
            "event_time": "2026-10-01T00:00:00Z",
            "known_at": "2026-10-05T00:00:00Z",
            "labeler": "a",
        },
    ]
    f = tmp_path / "labels.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    rep = import_file(container.session_factory, f, origin="real")
    assert rep.accepted == 1
    reasons = " ".join(r["reason"] for r in rep.rejected)
    assert "future" in reasons
    assert "synthetic subject" in reasons
    assert "source is required" in reasons
    assert len(rep.rejected) == 4
    again = import_file(container.session_factory, f, origin="real")
    assert again.accepted == 0
    assert again.duplicates == 1


# 36
def test_s36_backfilled_history_is_not_a_valid_backtest(world: World) -> None:
    world.svc.sync_targets()
    world.svc.plan()
    asyncio.run(world.svc.executor("w").drain())
    live = replay.validity(world.factory, include_synthetic=True)
    assert live["series"] > 0
    with transaction(world.factory) as s:
        s.execute(
            sa.text(
                "UPDATE ts_observations SET collected_at = period_end + interval '30 days', "
                "ingested_at = period_end + interval '30 days', provider_published_at = NULL"
            )
        )
    after = replay.validity(world.factory, include_synthetic=True)
    assert after["by_class"]["INVALID"] == after["series"]
    assert after["reportable"] == 0
    assert replay.validity(world.factory)["series"] == 0  # real data only: none here


# 37
def test_s37_readiness_never_counts_blocked_as_pass(
    container: AppContainer, tmp_path: Path
) -> None:
    snap = SystemService(container).snapshot(with_discovery=False)
    card = build_scorecard(container, snap, root=tmp_path)  # no recorded verification here
    assert card.state is ReleaseState.NOT_READY  # build evidence missing -> FAIL, not PASS
    items = {i.key: i for i in card.items}
    assert items["direct_search_provider"].status.value == "BLOCKED"
    assert items["detection_real_accuracy"].status.value == "BLOCKED"
    assert items["production_deployment"].status.value == "BLOCKED"
    assert card.counts()["PASS"] < len(card.items)


# 38
@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="PostgreSQL client tools missing")
def test_s38_backup_restore_drill_is_recorded_for_readiness(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: MutableClock
) -> None:
    monkeypatch.setattr(cli, "CONTAINER_FACTORY", lambda settings=None, **kw: make_container(clock))
    out = runner.invoke(cli.app, ["system", "backup", "--out", str(tmp_path), "--json"])
    assert out.exit_code == 0, out.output
    dump = json.loads(out.stdout)["path"]
    out = runner.invoke(cli.app, ["system", "restore-drill", dump])
    assert out.exit_code == 0, out.output
    assert "PASSED" in out.output
    with transaction(container.session_factory) as s:
        drill = SystemStateRepository(s).get(RESTORE_DRILL_KEY)
    assert drill is not None
    assert drill["ok"] is True
    snap = SystemService(container).snapshot(with_discovery=False)
    items = {i.key: i for i in build_scorecard(container, snap, root=tmp_path).items}
    assert items["backup_restore_drill"].status.value == "PASS"


# 39
async def test_s39_synthetic_pipeline_runs_end_to_end_through_scheduler_and_worker(
    world: World,
) -> None:
    c = world.container
    sched = PipelineScheduler(c, OperationsConfig(), owner="e2e", synthetic=True)
    worker = PipelineWorker(c, OperationsConfig(), worker_id="worker:e2e", synthetic=True)
    first = await sched.tick()
    assert first.leader
    assert first.ran["monitor_plan"] == "succeeded"
    out = await worker.tick()
    assert out["jobs"] > 0, out
    assert out["series"] > 0, out  # detection consumed the outbox the executor filled
    with transaction(world.factory) as s:
        obs = s.execute(
            sa.text("SELECT count(*) FROM ts_observations WHERE is_synthetic")
        ).scalar_one()
        real = s.execute(
            sa.text("SELECT count(*) FROM ts_observations WHERE NOT is_synthetic")
        ).scalar_one()
        runs = {r.task: r.status for r in ScheduledRunRepository(s).recent()}
    assert obs > 0
    assert real == 0  # nothing synthetic was stored as real
    assert runs["monitor_plan"] == "succeeded"
    assert {"ops_checks", "forecasting", "recommendations", "retention"} <= set(runs)
    assert integrity.check(world.factory)["status"] == "PASS"


# 40
def test_s40_evaluation_cli_reports_synthetic_offline_and_unknown_real_world(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: MutableClock
) -> None:
    monkeypatch.setattr(cli, "CONTAINER_FACTORY", lambda settings=None, **kw: make_container(clock))
    out = runner.invoke(cli.app, ["evaluate", "content-gaps", "--json"])
    assert out.exit_code == 0, out.output
    gaps = json.loads(out.stdout)
    assert gaps["origin"] == "synthetic"
    assert gaps["splits"]["heldout"]["false_verified_gaps"] == 0
    out = runner.invoke(cli.app, ["evaluate", "replay", "--json"])
    assert out.exit_code == 0, out.output
    rep = json.loads(out.stdout)
    assert rep["real_detection"]["status"] == "UNKNOWN"
    out = runner.invoke(cli.app, ["evaluate", "labels", "stats", "--json"])
    assert out.exit_code == 0
    assert json.loads(out.stdout) == {"real": {}, "synthetic": {}}
