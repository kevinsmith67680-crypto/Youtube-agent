"""Phase 4 detection against the real test database (synthetic data only, no network)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from tests.helpers import FakeYouTube, make_settings
from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent import cli
from trend_agent.container import AppContainer, build_container
from trend_agent.db.detection_models import (
    AnomalyResultRow,
    DetectionQueueRow,
    DetectionRunRow,
    DetectorStateRow,
    SurgeEventRow,
)
from trend_agent.db.monitoring_models import ObservationRow
from trend_agent.db.repositories.detection_runs import DetectionQueueRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.detection import demo as ddemo
from trend_agent.detection.service import DetectionService
from trend_agent.detection.synthetic import InjectedEvent, generate
from trend_agent.discovery.models import Category
from trend_agent.ingestion.retention import RetentionService
from trend_agent.monitoring.demo import StageClock, _service
from trend_agent.monitoring.service import MonitoringService
from trend_agent.monitoring.synthetic import load_monitoring_scenario, seed_keywords
from trend_agent.schemas.detection import RunType
from trend_agent.schemas.evidence import AlertStage
from trend_agent.schemas.scheduling import RevisionBehaviour
from trend_agent.schemas.surge import SurgeAlert

runner = CliRunner()
DAY = timedelta(days=1)


def _deny(request: httpx.Request) -> httpx.Response:
    raise httpx.ProxyError("egress denied")


def _container(clock: Any, fake_youtube: FakeYouTube) -> AppContainer:
    return build_container(
        make_settings(discovery_config_dir=str(CONFIG_DIR)),
        clock=clock,
        http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
        discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
    )


def count(factory: SessionFactory, model: Any, *where: Any) -> int:
    with factory() as s:
        stmt = sa.select(sa.func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return int(s.execute(stmt).scalar_one())


class World:
    """A few synthetic demo series written through the Phase 3 storage path."""

    def __init__(self, container: AppContainer) -> None:
        self.container = container
        self.clock = StageClock(ddemo.START + DAY * 70 + timedelta(hours=12))
        container.monitoring(clock=self.clock, include_synthetic=True).ensure_policies()
        spike = ddemo.DemoSeries(
            "A", "nimbus coder 3 tutorial", Category.AI,
            ddemo.M.DIRECT_YOUTUBE_SEARCH_INTEREST, ddemo.DIRECT,
            ddemo._spec("A", base=18, noise=0.08, events=(InjectedEvent("step", 68, 42.0),)),
        )  # fmt: skip
        self.items = [spike, *[s for s in ddemo.scenario_series() if s.scenario in ("E",)][:5]]
        self.targets = ddemo.ensure_targets(container, self.items, self.clock())
        self.values = {s.spec.key: generate(s.spec).values for s in self.items}
        with transaction(container.session_factory) as session:
            from trend_agent.db.monitoring_models import MonitoringTargetRow

            self.query_ids = {
                r.id: r.query_id for r in session.execute(sa.select(MonitoringTargetRow)).scalars()
            }
        self.previous: Any = None
        self.service = DetectionService(
            session_factory=container.session_factory,
            loaded=container.detection_config(),
            clock=self.clock,
            include_synthetic=True,
        )

    @property
    def factory(self) -> SessionFactory:
        return self.container.session_factory

    def advance(self, day: int) -> int:
        self.clock.now = ddemo.START + DAY * day + timedelta(hours=12)
        n = ddemo.write_available(
            self.container, self.items, self.values, self.targets, self.query_ids,
            self.previous, self.clock.now,
        )  # fmt: skip
        self.previous = self.clock.now
        return n


@pytest.fixture
def world(session_factory: SessionFactory, fake_youtube: FakeYouTube) -> Iterator[World]:
    container = _container(lambda: ddemo.START, fake_youtube)
    w = World(container)
    yield w
    container.engine.dispose()


# ---------------------------------------------------------------------------- live detection
def test_live_detection_from_outbox_is_idempotent_and_persists_state(world: World) -> None:
    assert world.advance(70) > 0
    assert count(world.factory, DetectionQueueRow) == len(world.items)
    first = world.service.run(run_type=RunType.LIVE, from_queue=True)
    assert first.series_evaluated == len(world.items)
    assert not first.errors
    assert count(world.factory, DetectionQueueRow) == 0
    results = count(world.factory, AnomalyResultRow)
    assert results >= first.observations_evaluated * 3
    states = count(world.factory, DetectorStateRow)
    assert states == len(world.items) * 5  # engine, anomaly cache, cusum, change point, cold start
    for day in range(71, 76):
        world.advance(day)
        world.service.run(run_type=RunType.LIVE, from_queue=True)
    episodes = count(world.factory, SurgeEventRow)
    assert episodes >= 1
    with transaction(world.factory) as session:
        ep = session.execute(sa.select(SurgeEventRow).order_by(SurgeEventRow.id)).scalars().first()
        assert ep is not None
        assert ep.is_synthetic
        alert = SurgeAlert.model_validate(ep.payload)
        assert alert.explanation.startswith("[SYNTHETIC]")
    totals = (count(world.factory, AnomalyResultRow), count(world.factory, SurgeEventRow))
    # unchanged data: nothing evaluated, nothing duplicated
    again = world.service.run(run_type=RunType.LIVE)
    assert again.observations_evaluated == 0
    assert again.skipped["no_new_observations"] >= 1
    # lost state: everything re-evaluated, still no duplicate result or episode
    with transaction(world.factory) as session:
        session.execute(sa.delete(DetectorStateRow))
    redo = world.service.run(run_type=RunType.LIVE)
    assert redo.observations_evaluated > 0
    assert (count(world.factory, AnomalyResultRow), count(world.factory, SurgeEventRow)) == totals
    # run records keep counts and multiple-testing information
    with transaction(world.factory) as session:
        run = session.get(DetectionRunRow, redo.run_id)
        assert run is not None
        assert run.status.value == "succeeded"
        assert run.observations_evaluated == redo.observations_evaluated
        assert "budget" in run.multiple_testing


def test_concurrent_detection_workers_never_duplicate(world: World) -> None:
    world.advance(72)
    errors: list[BaseException] = []
    reports: list[Any] = []

    def work(i: int) -> None:
        try:
            svc = DetectionService(
                session_factory=world.factory,
                loaded=world.container.detection_config(),
                clock=world.clock,
                include_synthetic=True,
            )
            reports.append(svc.run(run_type=RunType.WORKER, worker=f"w{i}"))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(not r.errors for r in reports)
    with world.factory() as s:
        dup = s.execute(
            sa.select(
                AnomalyResultRow.observation_id, AnomalyResultRow.detector_type, sa.func.count()
            )
            .group_by(AnomalyResultRow.observation_id, AnomalyResultRow.detector_type)
            .having(sa.func.count() > 1)
        ).all()
        open_dup = s.execute(
            sa.select(SurgeEventRow.episode_key, sa.func.count())
            .where(SurgeEventRow.status.in_(["watch", "candidate", "confirmed"]))
            .group_by(SurgeEventRow.episode_key)
            .having(sa.func.count() > 1)
        ).all()
    assert dup == []
    assert open_dup == []
    processed = sum(r.series_evaluated for r in reports)
    skipped = sum(r.skipped.get("concurrent_update", 0) for r in reports)
    assert processed - skipped >= len(world.items)  # every series handled exactly once


def test_revision_is_processed_with_recomputation(world: World) -> None:
    world.advance(70)
    world.service.run(run_type=RunType.LIVE, from_queue=True)
    s = world.items[0]
    tid = world.targets[s.spec.key]
    revised = ddemo._draft(s, tid, world.query_ids[tid], 60, 77.0, world.clock.now)
    with transaction(world.factory) as session:
        res = TimeSeriesRepository(session).write(
            revised, revision_behaviour=RevisionBehaviour.REVISABLE, ingested_at=world.clock.now
        )
        DetectionQueueRepository(session).mark(
            [(res.series_id, tid, revised.period_start)], world.clock.now
        )
    report = world.service.run(run_type=RunType.LIVE, from_queue=True)
    assert report.recomputations["revision"] == 1
    with world.factory() as session:
        rows = (
            session.execute(
                sa.select(AnomalyResultRow).where(AnomalyResultRow.observation_revision == 2)
            )
            .scalars()
            .all()
        )
    assert rows
    assert all(r.is_late for r in rows)


def test_derived_detection_records_follow_source_retention(world: World) -> None:
    world.advance(70)
    world.service.run(run_type=RunType.LIVE, from_queue=True)
    expiry = world.clock.now + DAY
    with transaction(world.factory) as session:
        session.execute(sa.update(ObservationRow).values(expires_at=expiry))
        session.execute(sa.update(AnomalyResultRow).values(expires_at=expiry))
    retention = RetentionService(world.factory, lambda: expiry + DAY)
    report = retention.report()
    assert report.expired["anomaly_results"] > 0
    deleted = retention.purge_expired()
    assert deleted["anomaly_results"] > 0
    assert count(world.factory, AnomalyResultRow) == 0


# ---------------------------------------------------------------------------- Phase 3 hook
async def test_phase3_collection_queues_series_and_triggers_detection(
    session_factory: SessionFactory, fake_youtube: FakeYouTube
) -> None:
    scenario = load_monitoring_scenario()
    clock = StageClock(scenario.anchor + timedelta(hours=30))
    container = _container(clock, fake_youtube)
    try:
        base = _service(container, scenario, clock)
        with transaction(base.session_factory) as session:
            seed_keywords(session, scenario, clock(), "en")
        detection = DetectionService(
            session_factory=container.session_factory,
            loaded=container.detection_config(),
            clock=clock,
            include_synthetic=True,
        )
        svc = MonitoringService(
            settings=container.settings,
            session_factory=container.session_factory,
            registry=container.registry,
            quota_store=container.quota_store,
            loaded=base.loaded,
            clock=clock,
            include_synthetic=True,
            after_collection=lambda: detection.run(
                run_type=RunType.HOOK, from_queue=True, worker="hook"
            ),
        )
        report = await svc.run_once()
    finally:
        await container.aclose()
    assert report.detection is not None
    assert report.detection.series_evaluated >= 1
    assert count(session_factory, AnomalyResultRow) >= report.detection.observations_evaluated
    assert count(session_factory, DetectionQueueRow) == 0  # outbox drained by the hook


# ---------------------------------------------------------------------------- demo + CLI
def test_synthetic_detection_demo_all_scenarios_pass(
    session_factory: SessionFactory, fake_youtube: FakeYouTube
) -> None:
    container = _container(lambda: ddemo.START, fake_youtube)
    try:
        report = ddemo.run_demo(container, null_series=120)
        # Phase 5 interface: typed, validated alerts and per-target views
        svc = container.detection(include_synthetic=True)
        alerts = svc.feed(active=False, include_synthetic=True)
        views = svc.target_views(include_synthetic=True)
        production = svc.feed(active=False, include_synthetic=False)
    finally:
        container.engine.dispose()
    assert len(alerts) == len(report.episodes)
    assert all(isinstance(a, SurgeAlert) for a in alerts)
    assert views
    assert all(v.synthetic for v in views)
    assert production == []
    failed = [c.as_dict() for c in report.checks if not c.passed]
    assert not failed
    assert {c.scenario for c in report.checks} >= set("ABCDEFGHIJKLMN")
    by = {e["scenario"]: e for e in report.episodes}
    assert by["A"]["classification"] == "direct_search_surge"
    assert by["H"]["classification"] == "external_search_surge"
    assert by["I"]["classification"] == "proxy_attention_signal"
    assert all(
        e["status"] != AlertStage.CONFIRMED.value for e in report.episodes if e["scenario"] in "HIF"
    )


@pytest.fixture
def wired_cli(
    monkeypatch: pytest.MonkeyPatch, session_factory: SessionFactory, fake_youtube: FakeYouTube
) -> None:
    monkeypatch.setattr(
        cli, "SETTINGS_FACTORY", lambda **kw: make_settings(discovery_config_dir=str(CONFIG_DIR))
    )
    monkeypatch.setattr(
        cli,
        "CONTAINER_FACTORY",
        lambda settings, **kw: build_container(
            settings,
            clock=lambda: ddemo.START + DAY * 120,
            http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
            discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
            **kw,
        ),
    )


def invoke(*args: str, code: int = 0) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == code, result.output
    return result.output


@pytest.mark.usefixtures("wired_cli")
def test_cli_detect_commands() -> None:
    assert "No surge episodes" in invoke("detect", "alerts")
    assert "No calibration stored" in invoke("detect", "thresholds")
    out = invoke("detect", "demo", "--null-series", "60")
    assert "SYNTHETIC DETECTION DEMO" in out
    assert "FAIL" not in out
    alerts = json.loads(invoke("detect", "alerts", "--include-synthetic", "--json"))
    assert alerts
    assert {"classification", "status", "confidence", "severity"} <= set(alerts[0])
    assert "No surge episodes" in invoke("detect", "alerts", "--active")  # synthetic hidden
    gaming = json.loads(
        invoke("detect", "alerts", "--include-synthetic", "--category", "gaming", "--json")
    )
    assert gaming
    assert all(a["category"] == "gaming" for a in gaming)
    event = next(a for a in alerts if a["classification"] == "direct_search_surge")
    explained = json.loads(
        invoke("detect", "explain", "--event-id", str(event["event_id"]), "--json")
    )
    assert explained["transitions"]
    assert explained["evidence"]
    SurgeAlert.model_validate(explained["alert"])
    assert "Baseline:" in invoke("detect", "explain", "--event-id", str(event["event_id"]))
    invoke("detect", "explain", "--event-id", "999999", code=1)
    q = json.loads(invoke("detect", "query", "--query", "nimbus coder 3 tutorial", "--json"))
    assert q["targets"][0]["series"][0]["results"]
    invoke("detect", "query", "--query", "no such query", code=1)
    ready = json.loads(invoke("detect", "readiness", "--include-synthetic", "--json"))
    levels = {r["query"]: r["baseline_readiness"] for r in ready}
    assert levels["nimbus coder 3 tutorial"] == "ready"
    assert levels["lumen 7 local install"] in ("provisional", "insufficient")  # cold start
    thresholds = json.loads(invoke("detect", "thresholds", "--json"))
    assert thresholds["cusum"]["k"] == 1.0
    assert thresholds["calibrations"]
    stats = json.loads(invoke("detect", "stats", "--include-synthetic", "--json"))
    assert stats["recent_runs"]
    assert stats["synthetic_episodes"]
    assert "surge-alert/1.0" in invoke("detect", "schema")
    replayed = json.loads(
        invoke("detect", "replay", "--query", "quantum sdk release notes", "--json")
    )
    assert replayed["evaluations"] > 0
    assert replayed["decision_digest"]
    again = json.loads(invoke("detect", "replay", "--query", "quantum sdk release notes", "--json"))
    assert again["decision_digest"] == replayed["decision_digest"]
    run = json.loads(invoke("detect", "run", "--all", "--include-synthetic", "--json"))
    assert run["observations_evaluated"] == 0  # nothing new
    invoke("detect", "run", "--category", "ai", "--json")
    cal = json.loads(invoke("detect", "calibrate", "--series", "80", "--json"))
    assert cal["strata"]
    assert cal["cusum_arl_table"]
    invoke("detect", "calibrate", "--dataset", "real", code=2)
    invoke("detect", "evaluate", "--dataset", "real", code=2)
    ev = json.loads(
        invoke("detect", "evaluate", "--test-series", "60", "--null-series", "60", "--json")
    )
    assert ev["label"].startswith("SYNTHETIC")
    assert ev["test_calibrated"]["recall"] is not None
    assert "evaluation" in invoke("detect", "stats")
    worker = invoke("detect", "worker", "--once", "--include-synthetic")
    assert "LISTEN trend_agent_detection" in worker


def test_cli_detect_requires_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "SETTINGS_FACTORY",
        lambda **kw: make_settings(
            database_url="postgresql+psycopg://nobody:x@127.0.0.1:1/none",
            discovery_config_dir=str(CONFIG_DIR),
        ),
    )
    result = runner.invoke(cli.app, ["detect", "alerts"])
    assert result.exit_code == 3
    assert "database unavailable" in result.output
