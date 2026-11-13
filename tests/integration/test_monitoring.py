"""Phase 3 monitoring against the real test database (network faked / synthetic only)."""

from __future__ import annotations

import random
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from tests.helpers import FakeYouTube, MutableClock, make_settings
from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent import cli
from trend_agent.container import AppContainer, build_container
from trend_agent.db.discovery_models import DiscoveredKeywordRow, KeywordStatusHistoryRow
from trend_agent.db.monitoring_models import (
    MonitoringJobRow,
    MonitoringTargetRow,
    ObservationRow,
)
from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.demo import run_demo as run_discovery_demo
from trend_agent.discovery.models import CandidateStatus, Category
from trend_agent.ingestion.retention import RetentionService
from trend_agent.monitoring.backfill import BackfillError, BackfillService
from trend_agent.monitoring.collectors import TargetSnapshot
from trend_agent.monitoring.demo import StageClock, _service, run_demo
from trend_agent.monitoring.service import MonitoringService
from trend_agent.monitoring.synthetic import load_monitoring_scenario, seed_keywords
from trend_agent.providers.base import RequestContext
from trend_agent.schemas.enums import DataNature, SampleQuality, ScaleType, SearchProperty
from trend_agent.schemas.enums import TimeGranularity as G
from trend_agent.schemas.monitoring import (
    JobStatus,
    JobType,
    MeasurementCategory,
    MonitoringStatus,
    RevisionKind,
    WriteOutcome,
)
from trend_agent.schemas.observation import NormalizationContext
from trend_agent.schemas.scheduling import RevisionBehaviour
from trend_agent.schemas.time_series import ObservationDraft

SCENARIO = load_monitoring_scenario()
ANCHOR = SCENARIO.anchor
DAY = timedelta(days=1)
runner = CliRunner()


def _deny(request: httpx.Request) -> httpx.Response:
    raise httpx.ProxyError("egress denied")


def _container(clock: Any, fake_youtube: FakeYouTube) -> AppContainer:
    return build_container(
        make_settings(discovery_config_dir=str(CONFIG_DIR)),
        clock=clock,
        http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
        discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
    )


class World:
    def __init__(self, container: AppContainer, clock: StageClock) -> None:
        self.container = container
        self.clock = clock
        self.svc: MonitoringService = _service(container, SCENARIO, clock)
        with transaction(self.svc.session_factory) as session:
            self.keywords = seed_keywords(session, SCENARIO, clock(), "en")

    @property
    def factory(self) -> SessionFactory:
        return self.svc.session_factory

    def target(self, query: str, measurement: MeasurementCategory | None = None) -> Any:
        m = measurement or MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST
        with transaction(self.factory) as session:
            rows = MonitoringRepository(session).targets(query_text=query, limit=10)
            session.expunge_all()
        return next(t for t in rows if t.measurement_category is m)

    def at(self, hours: float) -> None:
        self.clock.now = ANCHOR + timedelta(hours=hours)


@pytest.fixture
def world(session_factory: SessionFactory, fake_youtube: FakeYouTube) -> Iterator[World]:
    clock = StageClock(ANCHOR)
    container = _container(clock, fake_youtube)
    w = World(container, clock)
    yield w
    container.engine.dispose()


def count(factory: SessionFactory, model: Any, *where: Any) -> int:
    with factory() as s:
        stmt = sa.select(sa.func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return int(s.execute(stmt).scalar_one())


# ---------------------------------------------------------------------------- targets


def test_target_selection_and_discovery_lifecycle(world: World) -> None:
    report = world.svc.sync_targets()
    assert report.activated == 6  # the priority-48 keyword is not monitored
    with transaction(world.factory) as session:
        repo = MonitoringRepository(session)
        targets = repo.targets(limit=100)
        assert {t.status for t in targets} == {MonitoringStatus.ACTIVE}
        texts = repo.query_text([t.query_id for t in targets])
        assert "ai wallpaper generator" not in {texts[t.query_id][0] for t in targets}
        t = next(t for t in targets if texts[t.query_id][0] == "how to run nimbus coder 2 locally")
        assert t.category is Category.AI
        assert t.region == "US"
        assert t.language == "en"
        assert t.provider_in_use == "synthetic_trends"
        assert t.topic_cluster_id is not None
        assert t.intent_cluster_id is not None
        assert t.priority_score == 86
        assert t.config_version == world.svc.config.config_version
        assert t.tier.value == "critical"
        events = repo.events(t.id)
        assert [e.to_value for e in reversed(events) if e.kind == "status"] == ["pending", "active"]
        kw = session.get(DiscoveredKeywordRow, t.keyword_id)
        assert kw is not None
        assert kw.candidate_status is CandidateStatus.MONITORING
        history = (
            session.execute(
                sa.select(KeywordStatusHistoryRow).where(
                    KeywordStatusHistoryRow.keyword_id == kw.id
                )
            )
            .scalars()
            .all()
        )
        assert history[-1].actor == "monitoring"
    again = world.svc.sync_targets()
    assert again.targets_created == 0
    assert again.activated == 0


def test_capacity_limits_and_exploration_reserve(world: World) -> None:
    cfg = world.svc.config
    small = cfg.model_copy(
        update={"capacity": cfg.capacity.model_copy(update={"max_active_targets": 3})}
    )
    world.svc.targets._config = small
    report = world.svc.sync_targets()
    assert report.activated + report.pending_capacity == 6
    assert 1 <= report.activated <= 3
    with transaction(world.factory) as session:
        waiting = MonitoringRepository(session).targets(statuses=[MonitoringStatus.PENDING])
        assert waiting
        assert all("capacity" in t.status_reason for t in waiting)


def test_real_keyword_without_search_provider_is_not_presented_as_monitored(
    world: World,
) -> None:
    with transaction(world.factory) as session:
        kw = session.get(DiscoveredKeywordRow, world.keywords["lattice 5 migration guide"])
        assert kw is not None
        kw.is_synthetic = False  # pretend it is a real keyword
    svc = world.container.monitoring(clock=world.clock, include_synthetic=False)
    svc.sync_targets()
    with transaction(world.factory) as session:
        rows = MonitoringRepository(session).targets(query_text="lattice 5 migration guide")
        search = [t for t in rows if not t.is_proxy]
        proxy = [t for t in rows if t.is_proxy]
        assert search
        assert all(t.status is MonitoringStatus.PROVIDER_UNAVAILABLE for t in search)
        assert "google_trends" in search[0].status_reason  # says why
        assert proxy
        assert proxy[0].status is MonitoringStatus.ACTIVE
        assert proxy[0].measurement_category is MeasurementCategory.OFFICIAL_EVENT_SIGNAL
        keyword = session.get(DiscoveredKeywordRow, world.keywords["lattice 5 migration guide"])
        assert keyword is not None
        assert keyword.candidate_status is not CandidateStatus.MONITORING


# ---------------------------------------------------------------------------- planning


def test_planning_respects_category_shares_and_redistributes(world: World) -> None:
    world.svc.sync_targets()
    cfg = world.svc.config
    world.svc.planner._config = cfg.model_copy(
        update={"capacity": cfg.capacity.model_copy(update={"max_jobs_per_cycle": 4})}
    )
    report = world.svc.plan()
    assert report.jobs_created == 4
    usage = report.categories
    assert usage["ai"].reserved == 2 and usage["gaming"].reserved == 1  # noqa: PT018
    assert usage["technology"].reserved == 1
    assert sum(u.used for u in usage.values()) == 4
    assert all(u.used >= 1 for u in usage.values())  # no category starved
    again = world.svc.plan()  # the two that did not fit; queued targets are not re-planned
    assert again.jobs_created == 2
    with transaction(world.factory) as session:
        targets = session.execute(sa.select(MonitoringJobRow.target_id)).scalars().all()
    assert len(targets) == len(set(targets)) == 6  # one open job per target, no duplicates


def test_dry_run_plan_writes_nothing(world: World) -> None:
    world.svc.sync_targets()
    before = count(world.factory, MonitoringJobRow)
    report = world.svc.plan(dry_run=True)
    assert report.dry_run
    assert len(report.planned) == 6
    assert report.estimated_quota == {"synthetic_trends": {"requests": 6}}
    assert count(world.factory, MonitoringJobRow) == before == 0


async def test_quota_exhaustion_defers_until_provider_reset(world: World) -> None:
    world.svc.sync_targets()
    world.svc.plan()
    reports = await world.svc.executor("w").drain()
    assert sum(r.claimed for r in reports) == 6
    BackfillService(world.factory, world.svc.config, world.svc.collectors, clock=world.clock).plan(
        query="python tutorial for beginners", provider="synthetic_trends", days=365
    )
    reports = await world.svc.executor("w").drain()
    statuses = [o.status for r in reports for o in r.outcomes]
    assert JobStatus.DEFERRED in statuses  # 8/day cap: 6 used, backfill needs 3
    with transaction(world.factory) as session:
        deferred = JobRepository(session).jobs(statuses=[JobStatus.DEFERRED])
        reset = deferred[0].retry_after
        assert reset == datetime(2026, 3, 2, 8, tzinfo=UTC)  # midnight Los Angeles (PST)
        assert deferred[0].attempt_count == 0  # waiting is not a failed attempt
    world.clock.now = reset - timedelta(minutes=1)
    assert sum(r.claimed for r in await world.svc.executor("w").drain()) == 0
    world.clock.now = reset + timedelta(minutes=1)
    resumed = await world.svc.executor("w").drain()
    assert {o.status for r in resumed for o in r.outcomes} <= {
        JobStatus.SUCCEEDED,
        JobStatus.NO_NEW_DATA,
    }


# ---------------------------------------------------------------------------- jobs


def _enqueue(world: World, n: int) -> list[uuid.UUID]:
    world.svc.sync_targets()
    target = world.target("ai coding agents")
    ids = []
    with transaction(world.factory) as session:
        jobs = JobRepository(session)
        for i in range(n):
            job_id = jobs.enqueue(
                {
                    "idempotency_key": f"test:{i}",
                    "target_id": target.id,
                    "provider_name": "synthetic_trends",
                    "job_type": JobType.MANUAL_COLLECTION,
                    "category": Category.AI,
                    "scheduled_at": world.clock(),
                    "priority": i,
                    "max_attempts": 3,
                    "created_at": world.clock(),
                    "updated_at": world.clock(),
                }
            )
            assert job_id is not None
            ids.append(job_id)
        assert (
            jobs.enqueue(
                {
                    "idempotency_key": "test:0",
                    "target_id": target.id,
                    "provider_name": "synthetic_trends",
                    "job_type": JobType.MANUAL_COLLECTION,
                    "category": Category.AI,
                    "scheduled_at": world.clock(),
                    "priority": 0,
                    "max_attempts": 3,
                    "created_at": world.clock(),
                    "updated_at": world.clock(),
                }
            )
            is None
        )  # idempotency key
    return ids


def test_concurrent_workers_never_claim_the_same_job(world: World) -> None:
    ids = _enqueue(world, 60)
    barrier = threading.Barrier(4)
    claimed: dict[str, list[uuid.UUID]] = {}
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            barrier.wait()
            got: list[uuid.UUID] = []
            while True:
                with transaction(world.factory) as session:
                    rows = JobRepository(session).claim(
                        worker=name, now=world.clock(), lease=timedelta(minutes=5), limit=3
                    )
                    batch = [r.id for r in rows]
                if not batch:
                    break
                got.extend(batch)
            claimed[name] = got
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    flat = [j for got in claimed.values() for j in got]
    assert len(flat) == len(set(flat)) == len(ids)  # every job exactly once
    assert sum(1 for got in claimed.values() if got) >= 2  # real contention happened


def test_lease_expiry_recovery_and_fencing(world: World) -> None:
    _enqueue(world, 1)
    with transaction(world.factory) as session:
        (job,) = JobRepository(session).claim(
            worker="a", now=world.clock(), lease=timedelta(minutes=5), limit=5
        )
        token_a = job.lease_token
        job_id = job.id
    assert token_a is not None
    with transaction(world.factory) as session:  # not expired yet: nothing to claim
        assert (
            JobRepository(session).claim(
                worker="b", now=world.clock(), lease=timedelta(minutes=5), limit=5
            )
            == []
        )
    world.clock.now += timedelta(minutes=6)
    with transaction(world.factory) as session:
        jobs = JobRepository(session)
        recovered = jobs.recover_expired(world.clock())
        assert [r.id for r in recovered] == [job_id]
        (again,) = jobs.claim(worker="b", now=world.clock(), lease=timedelta(minutes=5), limit=5)
        assert again.attempt_count == 2
        token_b = again.lease_token
    assert token_b is not None
    with transaction(world.factory) as session:
        jobs = JobRepository(session)
        # The vanished worker comes back: its fenced writes are refused.
        assert not jobs.finish(job_id, token_a, status=JobStatus.SUCCEEDED, now=world.clock())
        assert jobs.finish(job_id, token_b, status=JobStatus.SUCCEEDED, now=world.clock())
        row = jobs.get(job_id)
        assert row is not None
        assert row.status is JobStatus.SUCCEEDED
        assert row.error_category is None


async def test_reexecution_after_crash_is_idempotent(world: World) -> None:
    world.at(36)
    world.svc.sync_targets()
    world.svc.plan()
    first = await world.svc.executor("a").drain()
    assert {o.status for r in first for o in r.outcomes} == {JobStatus.SUCCEEDED}
    obs_before = count(world.factory, ObservationRow)
    # Simulate "wrote observations, crashed before recording completion": re-queue two jobs
    # (two, so the synthetic provider's 8-requests/day quota is not the limiting factor).
    with transaction(world.factory) as session:
        two = session.execute(sa.select(MonitoringJobRow.id).limit(2)).scalars().all()
        session.execute(
            sa.update(MonitoringJobRow)
            .where(MonitoringJobRow.id.in_(two))
            .values(status=JobStatus.PENDING, completed_at=None)
        )
    again = await world.svc.executor("b").drain()
    outcomes = [o for r in again for o in r.outcomes]
    assert {o.status for o in outcomes} == {JobStatus.NO_NEW_DATA}
    assert all(o.counts.get("new", 0) == 0 for o in outcomes)
    assert count(world.factory, ObservationRow) == obs_before


async def test_outage_retries_then_permanent_errors_fail(world: World) -> None:
    world.svc.sync_targets()
    world.svc.plan()
    world.at(5 * 24 + 1)  # inside the synthetic outage (2026-03-07)
    world.clock.now = datetime(2026, 3, 7, 1, tzinfo=UTC)
    reports = await world.svc.executor("w", rng=random.Random(3)).drain()
    outcomes = [o for r in reports for o in r.outcomes]
    assert outcomes
    assert {o.status for o in outcomes} == {JobStatus.RETRY_WAIT}
    assert count(world.factory, ObservationRow) == 0  # nothing fabricated
    with transaction(world.factory) as session:
        health = MonitoringRepository(session).health("synthetic_trends")
        assert health is not None
        assert health.status.value == "unhealthy"
        job = JobRepository(session).jobs(statuses=[JobStatus.RETRY_WAIT])[0]
        assert job.retry_after is not None
        assert job.retry_after > world.clock()
    # A permanent error (capability) fails at once and marks the target.
    target = world.target("ai coding agents")
    with transaction(world.factory) as session:
        t = session.get(MonitoringTargetRow, target.id)
        assert t is not None
        t.provider_in_use = "google_trends"
        session.execute(
            sa.update(MonitoringJobRow)
            .values(status=JobStatus.PENDING, retry_after=None, provider_name="google_trends")
            .where(MonitoringJobRow.target_id == target.id)
        )
    reports = await world.svc.executor("w").drain()
    failed = [o for r in reports for o in r.outcomes if o.status is JobStatus.FAILED]
    assert failed
    assert failed[0].error_category is not None
    assert failed[0].error_category.value == "capability"
    assert world.target("ai coding agents").status is MonitoringStatus.PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------- storage


def _draft(
    world: World,
    value: float | None,
    *,
    day: int = 0,
    quality: SampleQuality = (SampleQuality.COMPLETE),
    collected: int = 3,
) -> ObservationDraft:
    query_id = world.target("ai coding agents").query_id
    start = ANCHOR + DAY * day
    return ObservationDraft(
        query_id=query_id,
        target_id=None,
        provider_name="unit",
        provider_version="1",
        measurement_category=MeasurementCategory.EXTERNAL_SEARCH_INTEREST,
        unit="index",
        period_start=start,
        period_end=start + DAY,
        granularity=G.DAY,
        provider_published_at=min(start + DAY * 2, ANCHOR + DAY * collected),
        collected_at=ANCHOR + DAY * collected,
        value=value,
        scale_type=ScaleType.CONSISTENT_INDEX,
        normalization_context=NormalizationContext(
            scale_type=ScaleType.CONSISTENT_INDEX, method="unit"
        ),
        region="US",
        language="en",
        search_property=SearchProperty.GOOGLE_WEB_SEARCH,
        data_nature=DataNature.ACTUAL,
        sample_quality=quality,
        source_reference="unit://x",
        data_policy_id="synthetic-test-data@1",
    )


def test_observation_write_semantics(world: World) -> None:
    world.svc.sync_targets()
    with transaction(world.factory) as session:
        ts = TimeSeriesRepository(session)

        def write(d: ObservationDraft, behaviour: RevisionBehaviour) -> Any:
            return ts.write(d, revision_behaviour=behaviour, ingested_at=ANCHOR + DAY * 10)

        assert write(_draft(world, 40.0), RevisionBehaviour.REVISABLE).outcome is WriteOutcome.NEW
        assert write(_draft(world, 40.0, collected=4), RevisionBehaviour.REVISABLE).outcome is (
            WriteOutcome.UNCHANGED
        )
        revised = write(_draft(world, 47.5, collected=5), RevisionBehaviour.REVISABLE)
        assert revised.outcome is WriteOutcome.REVISED
        assert revised.revision == 2
        lineage = ts.lineage(revised.observation_id)
        assert [(p.revision, p.value, p.is_current) for p in lineage] == [
            (1, 40.0, False),
            (2, 47.5, True),
        ]
        assert lineage[1].supersedes_id == lineage[0].id
        # Immutable source contradicting itself: kept as a conflict, original stays current.
        assert write(_draft(world, 10.0, day=1), RevisionBehaviour.IMMUTABLE).outcome is (
            WriteOutcome.NEW
        )
        conflict = write(_draft(world, 99.0, day=1, collected=5), RevisionBehaviour.IMMUTABLE)
        assert conflict.outcome is WriteOutcome.CONFLICTING
        assert (
            write(_draft(world, 99.0, day=1, collected=6), RevisionBehaviour.IMMUTABLE).outcome
            is WriteOutcome.UNCHANGED
        )  # same conflict not duplicated
        current = [
            p
            for p in ts.points(
                query_id=revised.series_id * 0 + _draft(world, 1).query_id, provider_name="unit"
            )
            if p.period_start == ANCHOR + DAY
        ]
        assert [p.value for p in current] == [10.0]
        # Missing data is stored as NULL with its status, never as zero.
        miss = write(
            _draft(world, None, day=2, quality=SampleQuality.MISSING), RevisionBehaviour.REVISABLE
        )
        assert miss.outcome is WriteOutcome.NEW
        row = session.get(ObservationRow, miss.observation_id)
        assert row is not None
        assert row.value is None
        assert row.sample_quality is SampleQuality.MISSING
    with pytest.raises(sa.exc.IntegrityError), transaction(world.factory) as session:
        session.execute(
            sa.update(ObservationRow)
            .where(ObservationRow.id == miss.observation_id)
            .values(value=0.0)
        )
    assert (
        count(world.factory, ObservationRow, ObservationRow.revision_kind == RevisionKind.CONFLICT)
        == 1
    )


# ---------------------------------------------------------------------------- backfill / retention


async def test_backfill_plan_resume_limits_and_provenance(world: World) -> None:
    service = BackfillService(
        world.factory, world.svc.config, world.svc.collectors, clock=world.clock
    )
    plan = service.plan(
        query="ai coding agents", provider="synthetic_trends", days=900, dry_run=True
    )
    assert any("at most 730 days" in note for note in plan.limitations)
    assert len(plan.windows) == 5  # 730 days / 180-day windows
    assert count(world.factory, MonitoringJobRow) == 0  # dry run
    plan = service.plan(query="ai coding agents", provider="synthetic_trends", days=200)
    assert plan.jobs_created == 2
    await world.svc.executor("w").run_once(job_ids=plan.job_ids, limit=10)
    status = service.status(plan.backfill_id)  # type: ignore[arg-type]
    assert status["state"] == "completed"
    with transaction(world.factory) as session:
        rows = session.execute(sa.select(ObservationRow.job_id).distinct()).scalars().all()
        jobs = [JobRepository(session).get(j) for j in rows if j is not None]
        assert jobs
        assert all(j is not None and j.backfill_id == plan.backfill_id for j in jobs)
    resumed = service.plan(query="ai coding agents", provider="synthetic_trends", days=200)
    assert resumed.resumed
    assert resumed.windows == []  # already covered; nothing re-requested
    with pytest.raises(BackfillError, match="does not provide historical data"):
        service.plan(query="ai coding agents", provider="youtube_data_api", days=10)
    with pytest.raises(BackfillError, match="not available"):
        service.plan(query="ai coding agents", provider="google_trends", days=10)


def test_retention_purges_expired_observations(world: World) -> None:
    world.svc.sync_targets()
    with transaction(world.factory) as session:
        ts = TimeSeriesRepository(session)
        draft = _draft(world, 5.0).model_copy(update={"expires_at": ANCHOR + DAY * 4})
        ts.write(
            draft, revision_behaviour=RevisionBehaviour.REVISABLE, ingested_at=ANCHOR + DAY * 3
        )
        ts.write(
            _draft(world, 6.0, day=1),
            revision_behaviour=RevisionBehaviour.REVISABLE,
            ingested_at=ANCHOR + DAY * 3,
        )
    retention = RetentionService(world.factory, lambda: ANCHOR + DAY * 5)
    assert retention.report().expired["ts_observations"] == 1
    assert retention.purge_expired()["ts_observations"] == 1
    assert count(world.factory, ObservationRow) == 1


# ---------------------------------------------------------------------------- proxy signals


async def test_event_signal_collector_derives_counts_without_zero_fill(
    session_factory: SessionFactory, fake_youtube: FakeYouTube
) -> None:
    clock = MutableClock(datetime(2026, 3, 1, 12, tzinfo=UTC))
    container = _container(clock, fake_youtube)
    try:
        await run_discovery_demo(container)
        svc = container.monitoring(clock=clock, include_synthetic=True)
        collector = svc.collectors.get("discovery_evidence")
        with transaction(svc.session_factory) as session:
            kw = MonitoringRepository(session).keyword_for_query(
                "how to run nimbus coder 2 locally"
            )
            assert kw is not None
            snap = TargetSnapshot(
                target_id=0,  # not persisted by this test
                keyword_id=kw.id,
                query_id=kw.query_id,
                canonical_query="x",
                original_query="x",
                region="WORLD",
                language=None,
                is_proxy=True,
                is_synthetic=True,
                measurement_category=MeasurementCategory.OFFICIAL_EVENT_SIGNAL,
            )
        result = await collector.collect(
            snap, datetime(2025, 1, 1, tzinfo=UTC), clock(), RequestContext()
        )
    finally:
        await container.aclose()
    assert result.drafts
    sims = {d.simulated_category for d in result.drafts}
    assert MeasurementCategory.OFFICIAL_EVENT_SIGNAL in sims
    assert MeasurementCategory.VIDEO_ACTIVITY not in sims  # YouTube-derived: not permitted
    assert result.withheld > 0
    assert all(d.is_proxy and d.value and d.value >= 1 for d in result.drafts)  # no zero days
    assert all(
        d.provider_published_at and d.provider_published_at <= d.collected_at for d in result.drafts
    )


# ---------------------------------------------------------------------------- end to end


async def test_synthetic_demo_all_scenarios_pass(
    session_factory: SessionFactory, fake_youtube: FakeYouTube
) -> None:
    container = _container(lambda: ANCHOR, fake_youtube)
    try:
        report = await run_demo(container)
    finally:
        await container.aclose()
    failed = [c.as_dict() for c in report.checks if not c.passed]
    assert not failed
    assert {c.scenario for c in report.checks} >= set("ABCDEFGHIJ")


# ---------------------------------------------------------------------------- CLI


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
            clock=lambda: ANCHOR + timedelta(days=12),
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
def test_cli_monitor_commands() -> None:
    import json

    assert "SYNTHETIC MONITORING DEMO" in invoke("monitor", "demo")
    targets = json.loads(invoke("monitor", "targets", "--include-synthetic", "--json"))
    assert len(targets) >= 6
    assert {"category", "priority", "provider", "status", "next_collection_at"} <= set(targets[0])
    assert "No monitoring targets" in invoke("monitor", "targets")  # synthetic hidden by default
    status = json.loads(invoke("monitor", "status", "--json"))
    assert status["targets"]["active_total"] >= 6
    plan = json.loads(invoke("monitor", "plan", "--dry-run", "--json"))
    assert plan["dry_run"] is True
    hist = json.loads(invoke("monitor", "history", "--query", "AI coding agents", "--json"))
    assert any(p["revision"] == 2 for s in hist["series"] for p in s["points"])
    assert "Target freshness" in invoke("monitor", "freshness", "--include-synthetic")
    ready = json.loads(
        invoke(
            "monitor",
            "readiness",
            "--query",
            "python tutorial for beginners",
            "--profile",
            "weekly_seasonal",
            "--json",
        )
    )
    assert ready[0]["state"] in ("ready", "stale_history", "source_unavailable")
    invoke("monitor", "readiness", "--profile", "nope", code=2)
    quality = json.loads(invoke("monitor", "quality", "--include-synthetic", "--json"))
    assert quality["providers"]
    assert "Monitoring quotas" in invoke("monitor", "quota")
    assert "Monitoring providers" in invoke("monitor", "providers")
    backfill = json.loads(
        invoke(
            "monitor",
            "backfill",
            "--query",
            "AI coding agents",
            "--provider",
            "mock",
            "--days",
            "30",
            "--json",
        )
    )
    assert backfill["jobs"] in ({"succeeded": 1}, {})
    invoke(
        "monitor",
        "backfill",
        "--query",
        "AI coding agents",
        "--provider",
        "youtube_data_api",
        "--days",
        "5",
        code=1,
    )
    replayed = json.loads(
        invoke("monitor", "replay", "--query", "AI coding agents", "--provider", "mock", "--json")
    )
    assert replayed["events"] > 0
    invoke("monitor", "replay", "--query", "nothing stored here", code=1)
    assert "paused" in invoke("monitor", "pause", "--query", "lattice 5 migration guide")
    assert "active" in invoke("monitor", "resume", "--query", "lattice 5 migration guide")
    assert "override applied" in invoke(
        "monitor", "override", "--query", "lattice 5 migration guide", "--tier", "critical"
    )
    invoke("monitor", "override", "--query", "lattice 5 migration guide", code=2)
    assert "disabled" in invoke("monitor", "disable", "--query", "lattice 5 migration guide")
    out = invoke("monitor", "activate", "--query", "lattice 5 migration guide", code=1)
    assert "no available provider" in out  # synthetic providers live only in the demo
    invoke("monitor", "activate", "--query", "never discovered", code=1)
    invoke("monitor", "sync")
    invoke("monitor", "run", "--once")
    invoke("monitor", "worker", "--once")
    invoke("monitor", "scheduler", "--once")
    invoke("monitor", "scheduler", "--interval", "5", "--max-ticks", "1")


def test_cli_monitor_requires_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, session_factory: SessionFactory
) -> None:
    monkeypatch.setattr(
        cli,
        "SETTINGS_FACTORY",
        lambda **kw: make_settings(discovery_config_dir=str(tmp_path / "x")),
    )
    result = runner.invoke(cli.app, ["monitor", "targets"])
    assert result.exit_code == 2, result.output
    assert "monitoring configuration" in result.output
