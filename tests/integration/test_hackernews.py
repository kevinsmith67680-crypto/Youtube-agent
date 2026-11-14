"""Hacker News integration against the real test database (all HN traffic is faked).

Covers: discovery -> items -> HN tables -> entities -> keyword hypotheses, idempotency,
repeated polling and momentum, the incremental refresher (updates feed, deleted/dead/
missing stories, failures, stop-after), reposts, outages, retention, prompt-injection and
HTML handling, the dashboard API (live and demo), the CLI and the scheduler stage.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.hackernews_fixtures import NOW, FakeHN, ts
from tests.helpers import ROOT, MutableClock, make_settings
from trend_agent import cli
from trend_agent.api.app import create_app
from trend_agent.container import AppContainer, build_container
from trend_agent.db.discovery_models import (
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
    DiscoverySourceRow,
)
from trend_agent.db.hackernews_models import HNObservationRow, HNStoryRow
from trend_agent.db.models import SearchInterestObservationRow
from trend_agent.db.monitoring_models import SeriesRow
from trend_agent.db.session import SessionFactory
from trend_agent.discovery.models import DiscoverySource, KeywordOrigin
from trend_agent.discovery.service import DiscoveryRunSummary
from trend_agent.hackernews.service import HackerNewsReadService, run_list_discovery
from trend_agent.providers.quota import InMemoryQuotaStore

pytestmark = pytest.mark.db
runner = CliRunner()
RELEVANT = {1001, 1002, 1003, 1004, 1005, 1006, 1009, 1014}
NEVER_STORED = {1007, 1008, 1010, 1011, 1012, 1013}


def make_container(clock: MutableClock, fake: FakeHN, **settings: Any) -> AppContainer:
    return build_container(
        make_settings(discovery_config_dir=str(ROOT / "config"), retry_attempts=1, **settings),
        clock=clock,
        quota_store=InMemoryQuotaStore(),
        discovery_http_client=httpx.AsyncClient(transport=fake.transport()),
    )


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(NOW)


@pytest.fixture
def hn(
    session_factory: SessionFactory, clock: MutableClock
) -> Iterator[tuple[AppContainer, FakeHN]]:
    fake = FakeHN()
    c = make_container(clock, fake)
    yield c, fake
    c.engine.dispose()


def hn_sources(c: AppContainer, *lists: str) -> list[DiscoverySource]:
    out = [s for s in c.discovery_config().sources if s.provider == "hackernews_firebase"]
    return [s for s in out if not lists or s.reference in lists]


async def run(c: AppContainer, *lists: str, force: bool = True) -> list[DiscoveryRunSummary]:
    svc = c.discovery()
    return await run_list_discovery(
        svc, hn_sources(c, *lists), config=c.discovery_config().config, force=force
    )


def story_rows(factory: SessionFactory) -> dict[int, HNStoryRow]:
    with factory() as s:
        rows = s.execute(sa.select(HNStoryRow)).scalars().all()
        for r in rows:
            s.expunge(r)
        return {r.story_id: r for r in rows}


def count(factory: SessionFactory, model: Any, *where: Any) -> int:
    with factory() as s:
        return int(
            s.execute(sa.select(sa.func.count()).select_from(model).where(*where)).scalar_one()
        )


# ------------------------------------------------------------------------------ pipeline
async def test_pipeline_persists_stories_observations_and_entities(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory
) -> None:
    c, _fake = hn
    summaries = await run(c)
    assert len(summaries) == 2  # one planned run per category, every list fetched
    fetched = {o.key: o for x in summaries for o in x.sources}
    assert set(fetched) == {s.key for s in hn_sources(c)}
    assert all(o.status == "succeeded" for o in fetched.values())
    rows = story_rows(session_factory)
    assert set(rows) == RELEVANT
    assert not set(rows) & NEVER_STORED  # off-topic, injection, dead, deleted, job, comment
    launch = rows[1001]
    assert launch.kind == "show_hn"
    assert launch.subject == "product_launch"
    assert launch.primary_category == "ai"
    assert launch.discussion_url == "https://news.ycombinator.com/item?id=1001"
    assert launch.linked_url == "https://github.com/agentry/agentry"
    assert launch.domain == "github.com"
    assert launch.author == "author"
    assert launch.published_at == NOW - timedelta(hours=1)
    assert set(launch.lists) == {"newstories", "topstories", "showstories"}
    assert (launch.latest_score, launch.latest_comments) == (40, 12)
    assert launch.first_seen_at == NOW
    assert launch.is_synthetic is False
    assert launch.item_id is not None
    assert launch.provider_id is not None
    assert 0 < launch.priority_score <= 100
    assert set(launch.priority_components) >= {"relevance", "novelty", "discussion_activity"}
    assert launch.refresh_tier in ("high", "normal", "low")
    assert launch.next_refresh_at is not None
    assert launch.next_refresh_at > NOW
    assert launch.momentum_state == "unknown"  # one observation is never growth
    assert launch.momentum["status"] == "insufficient_observations"
    # one observation per story per collection time, whatever the number of lists
    assert count(session_factory, HNObservationRow) == len(RELEVANT)
    # the Show HN project is the story's entity, unverified (community evidence only)
    with session_factory() as s:
        name, status = s.execute(
            sa.text(
                "select e.canonical_name, e.status from hn_stories h "
                "join discovered_entities e on e.id = h.entity_id where h.story_id = 1001"
            )
        ).one()
        assert name == "Agentry"
        assert status == "needs_verification"
        evidence = {
            r
            for (r,) in s.execute(
                sa.select(DiscoveryItemRow.evidence_type).where(
                    DiscoveryItemRow.provider_name == "hackernews_firebase"
                )
            )
        }
        assert {e.value for e in evidence} == {"community_discussion"}
    # nothing here is search demand: no search-interest series or observations exist
    assert count(session_factory, SeriesRow) == 0
    assert count(session_factory, SearchInterestObservationRow) == 0
    # the chronological lists stored a cursor
    with session_factory() as s:
        cursors = dict(
            s.execute(sa.select(DiscoverySourceRow.source_reference, DiscoverySourceRow.etag)).all()
        )
    assert cursors["newstories"] == "hn:last=1013"
    assert cursors["topstories"] is None


async def test_reposts_of_one_article_are_not_independent(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory
) -> None:
    c, _ = hn
    await run(c)
    rows = story_rows(session_factory)
    first, repost = rows[1002], rows[1003]
    assert first.canonical_link == repost.canonical_link  # utm and trailing slash ignored
    assert repost.priority_components["novelty"]["value"] == 0.2
    assert repost.priority_components["_meta"]["repost_of"] == 1002
    assert first.priority_components["novelty"]["value"] == 1.0
    detail = HackerNewsReadService(session_factory, clock=c.clock).story_detail(1002)
    assert detail is not None
    assert [r["story_id"] for r in detail["reposts"]] == [1003]


async def test_keyword_candidates_are_hypotheses_never_observed_queries(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory
) -> None:
    c, _ = hn
    await run(c)
    with session_factory() as s:
        origins = set(
            s.execute(
                sa.select(DiscoveredKeywordRow.origin)
                .join(
                    DiscoveryEvidenceRow, DiscoveryEvidenceRow.keyword_id == DiscoveredKeywordRow.id
                )
                .join(DiscoveryItemRow, DiscoveryItemRow.id == DiscoveryEvidenceRow.item_id)
                .where(DiscoveryItemRow.provider_name == "hackernews_firebase")
            ).scalars()
        )
    assert KeywordOrigin.OBSERVED_QUERY not in origins
    detail = HackerNewsReadService(session_factory, clock=c.clock).story_detail(1002)
    assert detail is not None
    assert all(k["evidence_class"] == "KEYWORD_HYPOTHESIS" for k in detail["keyword_candidates"])
    assert all(k["origin"] != "observed_query" for k in detail["keyword_candidates"])


async def test_prompt_injection_and_html_are_neutralised(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory
) -> None:
    c, _ = hn
    await run(c)
    with session_factory() as s:
        titles = [t for (t,) in s.execute(sa.select(DiscoveryItemRow.title))]
        texts = [t for (t,) in s.execute(sa.select(DiscoveryItemRow.excerpt)) if t]
        stored = [t for (t,) in s.execute(sa.select(HNStoryRow.title))]
    joined = " ".join(titles + texts + stored).lower()
    assert "ignore previous instructions" not in joined
    assert "5 million" not in joined
    assert "<script" not in joined
    assert "javascript:" not in joined
    assert "<b>" not in joined
    assert "Ask HN: Is MCP worth it for AI agents?" in stored


# ------------------------------------------------------------------------------ momentum
async def test_repeated_polling_is_idempotent_and_builds_momentum(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory, clock: MutableClock
) -> None:
    c, fake = hn
    await run(c, "topstories")
    before = (count(session_factory, HNStoryRow), count(session_factory, HNObservationRow))
    await run(c, "topstories")  # same collection time: nothing new
    assert (count(session_factory, HNStoryRow), count(session_factory, HNObservationRow)) == before
    clock.advance(timedelta(minutes=30))
    fake.set_activity(1002, score=360, comments=170)
    await run(c, "topstories")
    row = story_rows(session_factory)[1002]
    assert (row.latest_score, row.latest_comments) == (360, 170)
    m = row.momentum
    assert m["status"] == "ok"
    assert m["interval_hours"] == 0.5
    assert (m["score_delta"], m["comments_delta"]) == (60, 20)
    assert (m["score_per_hour"], m["comments_per_hour"]) == (120.0, 40.0)
    assert row.momentum_state == "rising"
    assert row.refresh_tier == "high"
    assert m["signal_class"] == "COMMUNITY_ACTIVITY"
    unchanged = story_rows(session_factory)[1004]
    assert unchanged.momentum["score_delta"] == 0
    assert unchanged.momentum_state == "steady"
    trending = HackerNewsReadService(session_factory, clock=c.clock).trending(hours=48)
    assert trending["items"][0]["story_id"] == 1002
    assert "not YouTube search volume" in trending["caveat"]


async def test_a_story_on_several_lists_in_one_poll_is_observed_once(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory, clock: MutableClock
) -> None:
    c, _ = hn
    await run(c, "newstories")
    clock.advance(timedelta(seconds=5))  # the next list of the same poll (cached item)
    await run(c, "topstories")
    with session_factory() as s:
        n = s.execute(
            sa.select(sa.func.count())
            .select_from(HNObservationRow)
            .where(HNObservationRow.story_id == 1001)
        ).scalar_one()
    assert n == 1
    assert set(story_rows(session_factory)[1001].lists) == {"newstories", "topstories"}
    clock.advance(timedelta(minutes=20))  # a later poll is a new observation
    await run(c, "topstories")
    assert count(session_factory, HNObservationRow, HNObservationRow.story_id == 1001) == 2


async def test_refresher_pulls_forward_changed_stories_and_stops_dead_ones(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory, clock: MutableClock
) -> None:
    c, fake = hn
    await run(c)
    refresher = c.hackernews_refresher()
    # nothing due, nothing changed: one updates request, no item requests
    nothing = await refresher.run()
    assert nothing["requests"] == 1
    assert nothing["selected"] == 0
    # 12 min later: 1004 changed (in /updates.json) -> pulled forward before its tier is due
    clock.advance(timedelta(minutes=12))
    fake.set_activity(1004, score=560, comments=230)
    before = len(fake.requests)
    report = await refresher.run()
    assert report["selected"] == 1
    assert report["observed"] == 1
    assert report["pulled_forward_by_updates"] == 1
    assert len(fake.requests) - before == 2  # updates + one item
    row = story_rows(session_factory)[1004]
    assert row.latest_score == 560
    assert row.momentum["status"] == "ok"
    with session_factory() as s:
        origins = [
            o
            for (o,) in s.execute(
                sa.select(HNObservationRow.origin).where(HNObservationRow.story_id == 1004)
            )
        ]
    assert sorted(origins) == ["list", "refresh"]
    # 5 h later every story is due: deleted, dead, missing stop; a failure is deferred
    clock.advance(timedelta(hours=5))
    fake.items[1005] = {"id": 1005, "deleted": True, "type": "story", "time": ts(NOW)}
    fake.items[1006] = {**fake.items[1006], "dead": True}  # type: ignore[dict-item]
    fake.items[1009] = None
    fake.override["/item/1001.json"] = httpx.Response(500)
    report = await refresher.run(max_items=50)
    assert report["deactivated"] == {"deleted": 1, "dead": 1, "missing": 1} | (
        {"older_than_stop_after": report["deactivated"]["older_than_stop_after"]}
        if "older_than_stop_after" in report["deactivated"]
        else {}
    )
    assert report["failed"] == {"temporarily_unavailable": 1}
    rows = story_rows(session_factory)
    assert {rows[i].monitoring_state for i in (1005, 1006, 1009)} == {"inactive"}
    assert rows[1005].classification["monitoring_stopped"] == "deleted"
    assert rows[1001].monitoring_state == "active"
    assert rows[1001].next_refresh_at == clock() + timedelta(minutes=60)
    # stop after 72 h: no requests for old stories
    clock.advance(timedelta(hours=80))
    del fake.override["/item/1001.json"]
    final = await refresher.run()
    assert final["selected"] == 0
    assert count(session_factory, HNStoryRow, HNStoryRow.monitoring_state == "active") == 0
    await c.aclose()


async def test_refresh_respects_its_per_run_bound(
    hn: tuple[AppContainer, FakeHN], clock: MutableClock
) -> None:
    c, fake = hn
    await run(c)
    clock.advance(timedelta(hours=5))
    before = len(fake.requests)
    report = await c.hackernews_refresher().run(max_items=3, use_updates=False)
    assert report["selected"] == 3
    assert len(fake.requests) - before == 3


# ------------------------------------------------------------------------------ failures
async def test_outage_of_one_list_is_contained_and_recorded(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    fake = FakeHN()
    fake.override["/newstories.json"] = httpx.ConnectError("connection refused")
    c = make_container(clock, fake)
    summaries = await run(c)
    by_key = {o.key: o for x in summaries for o in x.sources}
    assert by_key["hn:list:newstories"].status == "failed"
    assert by_key["hn:list:newstories"].error_type == "temporarily_unavailable"
    assert by_key["hn:list:topstories"].status == "succeeded"
    assert count(session_factory, HNStoryRow) > 0
    summary = HackerNewsReadService(session_factory, clock=c.clock).summary()
    health = {h["list"]: h for h in summary["sources"]}
    assert health["newstories"]["consecutive_failures"] == 1
    assert health["newstories"]["last_error"]
    c.engine.dispose()


async def test_retention_purges_hn_rows(
    hn: tuple[AppContainer, FakeHN], session_factory: SessionFactory, clock: MutableClock
) -> None:
    c, _ = hn
    await run(c)
    clock.advance(timedelta(days=91))
    deleted = c.retention().purge_expired()
    assert deleted["hn_stories"] == len(RELEVANT)
    assert count(session_factory, HNStoryRow) == 0
    assert count(session_factory, HNObservationRow) == 0


# ------------------------------------------------------------------------------ API
async def test_dashboard_api_labels_community_activity(
    hn: tuple[AppContainer, FakeHN],
) -> None:
    c, _ = hn
    await run(c)
    client = TestClient(create_app(c.dashboard_backend(), c.settings, ops=c))
    summary = client.get("/api/hackernews/summary").json()
    assert summary["demo"] is False
    assert summary["stories"] == len(RELEVANT)
    assert summary["provider"]["status"] == "available"
    assert set(summary["evidence_classes"]) == {
        "COMMUNITY_ACTIVITY",
        "KEYWORD_HYPOTHESIS",
        "EXTERNAL_ATTENTION",
        "DIRECT_YOUTUBE_SEARCH_INTEREST",
    }
    assert {h["list"] for h in summary["sources"]} == {
        "newstories", "topstories", "beststories", "showstories", "askstories",
    }  # fmt: skip
    show = client.get(
        "/api/hackernews/stories", params={"list": "show", "order": "priority"}
    ).json()
    assert {i["story_id"] for i in show["items"]} == {1001, 1005}
    assert all(i["evidence_class"] == "COMMUNITY_ACTIVITY" for i in show["items"])
    ai = client.get("/api/hackernews/stories", params={"category": "ai"}).json()
    assert 1002 in {i["story_id"] for i in ai["items"]}
    ask = client.get("/api/hackernews/stories", params={"kind": "ask_hn"}).json()
    assert {i["story_id"] for i in ask["items"]} == {1006, 1009}
    detail = client.get("/api/hackernews/stories/1001").json()
    assert detail["discussion_url"] == "https://news.ycombinator.com/item?id=1001"
    assert detail["external_attention"]["evidence_class"] == "EXTERNAL_ATTENTION"
    assert detail["direct_youtube_search_interest"]["available"] is False
    assert detail["momentum"]["status"] == "insufficient_observations"
    assert len(detail["observations"]) == 1
    trending = client.get("/api/hackernews/trending").json()
    assert trending["items"] == []
    assert trending["awaiting_repeat_observation"] == len(RELEVANT) - 1
    assert client.get("/api/hackernews/stories/42").status_code == 404
    assert client.get("/api/hackernews/stories", params={"list": "jobs"}).status_code == 422
    assert client.get("/api/hackernews/stories", params={"order": "volume"}).status_code == 422
    body = json.dumps([summary, show, ai, detail, trending]).lower()
    assert "search_volume" not in body
    assert "searches_per" not in body


def test_dashboard_api_in_demo_mode_is_empty_and_labelled() -> None:
    settings = make_settings(demo_mode=True)
    c = build_container(settings, quota_store=InMemoryQuotaStore())
    from trend_agent.recommendations.backend import DashboardBackend

    class Empty:  # the Hacker News endpoints never read the demo backend
        pass

    backend: DashboardBackend = Empty()  # type: ignore[assignment]
    client = TestClient(create_app(backend, settings, ops=None))
    data = client.get("/api/hackernews/stories").json()
    assert data["demo"] is True
    assert data["synthetic"] is True
    assert data["items"] == []
    assert client.get("/api/hackernews/summary").json()["provider"]["status"] == "demo"
    assert client.get("/api/hackernews/trending").json()["items"] == []
    assert client.get("/api/hackernews/stories/1").status_code == 404
    c.engine.dispose()


# ------------------------------------------------------------------------------ scheduler
async def test_scheduler_stage_is_gated_and_runs_lists_then_refresh(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    from trend_agent.ops.pipeline import _hackernews

    fake = FakeHN()
    off = make_container(clock, fake)
    ctx = SimpleNamespace(container=off, synthetic=False, max_per_run=5)
    assert "TREND_AGENT_SCHEDULED_DISCOVERY" in (await _hackernews(ctx))["skipped"]  # type: ignore[arg-type]
    disabled = make_container(clock, fake, scheduled_discovery=True, hackernews_api_enabled=False)
    ctx = SimpleNamespace(container=disabled, synthetic=False, max_per_run=5)
    assert "HACKERNEWS_API_ENABLED" in (await _hackernews(ctx))["skipped"]  # type: ignore[arg-type]
    assert fake.requests == []
    on = make_container(clock, fake, scheduled_discovery=True)
    ctx = SimpleNamespace(container=on, synthetic=False, max_per_run=5)
    out = await _hackernews(ctx)  # type: ignore[arg-type]
    assert out["discovery"]["fetched"] == 5
    assert out["discovery"]["failed"] == 0
    assert out["refresh"]["requests"] == 1  # updates feed only: nothing due yet
    # 5 minutes later nothing is due: per-source minimum intervals hold
    clock.advance(timedelta(minutes=5))
    again = await _hackernews(ctx)  # type: ignore[arg-type]
    assert again["discovery"]["fetched"] == 0
    # 16 minutes after the first run new/show are due again, top/ask/best are not
    clock.advance(timedelta(minutes=11))
    later = await _hackernews(ctx)  # type: ignore[arg-type]
    assert later["discovery"]["fetched"] == 2
    synthetic = SimpleNamespace(container=on, synthetic=True, max_per_run=5)
    assert "synthetic" in (await _hackernews(synthetic))["skipped"]  # type: ignore[arg-type]
    await on.aclose()
    for x in (off, disabled, on):
        x.engine.dispose()


def test_scheduler_config_has_the_hackernews_task() -> None:
    from trend_agent.ops.config import load_operations_config
    from trend_agent.ops.pipeline import DEFAULT_STAGES

    cfg = load_operations_config(str(ROOT / "config"))
    task = cfg.scheduler.tasks["hackernews"]
    assert task.interval_seconds == 900
    assert task.max_per_run == 5
    assert "hackernews" in {s.name for s in DEFAULT_STAGES}


# ------------------------------------------------------------------------------ CLI
@pytest.fixture
def wired_cli(
    session_factory: SessionFactory, clock: MutableClock, monkeypatch: pytest.MonkeyPatch
) -> FakeHN:
    fake = FakeHN()
    settings = make_settings(discovery_config_dir=str(ROOT / "config"), retry_attempts=1)
    store = InMemoryQuotaStore()  # shared across commands, like the database store
    monkeypatch.setattr(cli, "SETTINGS_FACTORY", lambda: settings)
    monkeypatch.setattr(
        cli,
        "CONTAINER_FACTORY",
        lambda s=None, **kw: build_container(
            settings,
            clock=clock,
            quota_store=store,
            discovery_http_client=httpx.AsyncClient(transport=fake.transport()),
        ),
    )
    return fake


def _json(args: list[str]) -> Any:
    out = runner.invoke(cli.app, args)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def test_cli_commands(wired_cli: FakeHN, clock: MutableClock) -> None:
    status = _json(["hackernews", "status", "--json"])
    assert status["status"] == "available"
    assert status["credentials"] == "none required"
    assert {s["list"] for s in status["sources"]} == {"new", "top", "best", "show", "ask"}
    assert "show_hn_discovery" in status["capabilities"]
    assert status["signal_class"] == "COMMUNITY_ACTIVITY"
    test = _json(["hackernews", "test", "--json"])
    assert test["ok"] is True
    assert set(test["endpoints"].values()) == {"ok"}
    assert test["lists"]["newstories"] == len(wired_cli.lists["newstories"])
    assert test["requests_sent"] <= 10
    found = _json(["hackernews", "discover", "--category", "ai", "--json"])
    assert all(r["status"] == "succeeded" for r in found["runs"])
    seen = {i["story_id"] for i in found["stories_seen_this_run"]["items"]}
    assert {1001, 1002} <= seen
    assert 1004 not in seen
    show = _json(["hackernews", "stories", "--type", "show", "--json"])
    assert {i["story_id"] for i in show["show"]["items"]} == {1001, 1005}
    one = _json(["hackernews", "stories", "--story", "1002", "--json"])
    assert one["story_id"] == 1002
    assert one["evidence_class"] == "COMMUNITY_ACTIVITY"
    live = _json(["hackernews", "stories", "--type", "new", "--live", "--limit", "5", "--json"])
    assert live["live"] is True
    assert "new" in live["lists"]
    trending = _json(["hackernews", "trending", "--json"])
    assert trending["measured"] == 0
    stats = _json(["hackernews", "stats", "--json"])
    assert stats["stories"] == len(RELEVANT)
    assert stats["by_list"]["showstories"] == 2
    clock.advance(timedelta(hours=5))
    refresh = _json(["hackernews", "refresh", "--max-items", "4", "--json"])
    assert refresh["selected"] == 4
    assert refresh["observed"] == 4
    plain = runner.invoke(cli.app, ["hackernews", "stories"])
    assert plain.exit_code == 0
    assert "not growth" in plain.output
    assert "search interest" in plain.output
    bad = runner.invoke(cli.app, ["hackernews", "stories", "--type", "jobs"])
    assert bad.exit_code == 2
