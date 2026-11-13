"""Keyword discovery against the real test database (all network access is faked or denied)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from tests.helpers import FakeYouTube, MutableClock, make_settings
from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent import cli
from trend_agent.container import AppContainer, build_container
from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
    DiscoveryRunRow,
    EntityMentionRow,
    IntentClusterRow,
    KeywordStatusHistoryRow,
)
from trend_agent.db.models import SearchInterestObservationRow, SearchQueryRow
from trend_agent.db.session import SessionFactory
from trend_agent.discovery.demo import run_demo
from trend_agent.discovery.lifecycle import DISCOVERY_ACTOR
from trend_agent.discovery.models import (
    TARGET_CATEGORIES,
    CandidateStatus,
    Category,
    DiscoveryMethod,
    EntityStatus,
    EntityType,
    EvidenceType,
    KeywordOrigin,
)
from trend_agent.discovery.providers.synthetic import load_scenario
from trend_agent.ingestion.retention import RetentionService
from trend_agent.schemas.enums import IngestionStatus

runner = CliRunner()


def _deny(request: httpx.Request) -> httpx.Response:
    raise httpx.ProxyError(f"egress denied for {request.url.host}")


def _container(clock: MutableClock, fake_youtube: FakeYouTube, **settings: Any) -> AppContainer:
    return build_container(
        make_settings(discovery_config_dir=str(CONFIG_DIR), **settings),
        clock=clock,
        http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
        discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
    )


@pytest.fixture
def container(
    session_factory: SessionFactory, clock: MutableClock, fake_youtube: FakeYouTube
) -> Iterator[AppContainer]:
    c = _container(clock, fake_youtube)
    yield c
    c.engine.dispose()


def keywords(factory: SessionFactory) -> dict[str, DiscoveredKeywordRow]:
    with factory() as s:
        rows = s.execute(
            sa.select(SearchQueryRow.canonical_query, DiscoveredKeywordRow).join(
                SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id
            )
        ).all()
        s.expunge_all()
    return {str(q): k for q, k in rows}


def entities(factory: SessionFactory) -> dict[str, DiscoveredEntityRow]:
    with factory() as s:
        rows = s.execute(sa.select(DiscoveredEntityRow)).scalars().all()
        s.expunge_all()
    return {r.canonical_name: r for r in rows}


def count(factory: SessionFactory, model: Any) -> int:
    with factory() as s:
        return int(s.execute(sa.select(sa.func.count()).select_from(model)).scalar_one())


# ---------------------------------------------------------------------------- end to end


async def test_demo_end_to_end(container: AppContainer, session_factory: SessionFactory) -> None:
    results = dict(await run_demo(container))
    history, current = results["history"], results["current"]
    assert history.status is IngestionStatus.SUCCEEDED
    assert current.status is IngestionStatus.PARTIAL  # synthetic outage + quota exhaustion
    assert current.is_synthetic
    failed = {o.key: o.error_type for o in current.sources if o.status == "failed"}
    assert failed == {
        "synthetic:gaming:outage-source": "temporarily_unavailable",
        "synthetic:gaming:quota-source": "quota_exhausted",
    }
    skipped = {s["source"]: s["reason"] for s in current.skipped}
    assert "unlicensed_scraper" in skipped["synthetic:gaming:scraped-site"]
    assert current.categories["ai"].items_duplicate == 1
    assert current.categories["technology"].items_outdated == 1
    assert current.categories["gaming"].status is IngestionStatus.PARTIAL
    assert current.categories["ai"].status is IngestionStatus.SUCCEEDED

    kw = keywords(session_factory)
    ent = entities(session_factory)

    # Scenario 1: AI coding model release.
    nc2 = ent["Nimbus Coder 2"]
    assert nc2.status is EntityStatus.VERIFIED
    assert nc2.entity_type is EntityType.AI_MODEL
    assert ent["Nimbus Coder"].is_family
    assert nc2.parent_entity_id == ent["Nimbus Coder"].id
    assert {"local", "api", "install"} <= set(nc2.attributes)
    local = kw["how to run nimbus coder 2 locally"]
    assert local.origin is KeywordOrigin.GENERATED_HYPOTHESIS
    assert local.candidate_status is CandidateStatus.PRIORITIZED
    assert local.entity_id == nc2.id
    assert local.novelty_kind.value == "new_version"
    observed = kw["nimbus coder 2 install"]
    assert observed.origin is KeywordOrigin.OBSERVED_QUERY
    assert observed.discovery_type is DiscoveryMethod.OBSERVED_QUERY
    assert "nimbus coder 2 vs nimbus coder 1.5" in kw
    assert "nimbus coder 1.5 vs nimbus coder 2" not in kw  # no reverse/self-family pairs
    # Conflicting syndicated date: the official date wins, the conflict is recorded.
    assert nc2.release_date is not None
    assert nc2.release_date.date().isoformat() == "2026-02-28"
    assert len(nc2.extra["conflicting_release_dates"]) == 2
    # Syndicated copies are not independent: more mentioning items than origins.
    with session_factory() as s:
        mentions = s.execute(
            sa.select(sa.func.count(sa.distinct(EntityMentionRow.item_id))).where(
                EntityMentionRow.entity_id == nc2.id
            )
        ).scalar_one()
    assert nc2.extra["independent_origins"] == 4
    assert mentions > nc2.extra["independent_origins"]
    # The rumoured successor is stored but never expanded.
    nc3 = ent["Nimbus Coder 3"]
    assert nc3.status is EntityStatus.NEEDS_VERIFICATION
    assert nc3.release_date is None
    assert not [k for k in kw.values() if k.entity_id == nc3.id]

    # Scenario 3: framework major version.
    assert ent["Lattice 5.0"].release_level == "major"
    assert ent["Lattice 5.0.1"].release_level == "patch"
    assert {"how to upgrade to lattice 5.0", "lattice 5.0 migration guide"} <= set(kw)
    assert "lattice 5.0 vs lattice 4.0" in kw
    assert not [q for q in kw if "5.0.1" in q]  # patch releases are not expanded
    assert not [q for q in kw if "lattice 2.0" in q]  # outdated item ignored

    # Similar names stay separate entities.
    assert ent["Nimbus 2"].entity_type is EntityType.HARDWARE
    assert ent["Nimbus 2"].category is Category.TECHNOLOGY
    assert kw["nimbus 2 review"].entity_id == ent["Nimbus 2"].id

    # Scenario 2: limited-time game event with items.
    event = ent["Frostfall Festival"]
    assert event.entity_type is EntityType.GAME_EVENT
    assert event.parent_entity_id == ent["Skyforge Arena"].id
    assert ent["Glacier Lance"].parent_entity_id == event.id
    assert {"frostfall festival challenges", "glacier lance location"} <= set(kw)
    assert kw["how to unlock rime glider"].candidate_status is CandidateStatus.PRIORITIZED
    assert "Void Crown" not in ent  # speculative item claim never becomes an entity
    # The previous event (90 days ago, not seen since) went dormant.
    ember = [k for q, k in kw.items() if "ember rush" in q or "cinder bow" in q]
    assert ember
    assert {k.candidate_status for k in ember} == {CandidateStatus.DORMANT}
    # Curated entities and families have no fabricated release date.
    assert ent["Skyforge Arena"].release_date is None
    assert ent["Nimbus Coder"].release_date is None

    # Everything stored from the fixture is labelled synthetic.
    assert all(k.is_synthetic for k in kw.values())
    assert all(e.is_synthetic for e in ent.values() if not e.evergreen)


async def test_demo_provenance_scores_clusters_and_history(
    container: AppContainer, session_factory: SessionFactory
) -> None:
    await run_demo(container)
    kw = keywords(session_factory)
    with session_factory() as s:
        evidence = s.execute(sa.select(DiscoveryEvidenceRow)).scalars().all()
        history = s.execute(sa.select(KeywordStatusHistoryRow)).scalars().all()
        items = s.execute(sa.select(DiscoveryItemRow)).scalars().all()
        clusters = {c.id: c for c in s.execute(sa.select(IntentClusterRow)).scalars()}

    by_keyword: dict[int, list[DiscoveryEvidenceRow]] = {}
    for e in evidence:
        by_keyword.setdefault(e.keyword_id, []).append(e)
    for query, k in kw.items():
        # Provenance: every candidate links to stored source material.
        assert by_keyword.get(k.id), query
        # Priority is stored with its version and components, and contains no volume.
        assert k.scoring_version == "priority-v1"
        assert set(k.score_components["components"]) == {
            "novelty",
            "freshness",
            "corroboration",
            "relevance",
            "intent_actionability",
            "evidence_quality",
        }
        assert "not search volume" in k.score_components["note"]
        assert 0 <= k.priority_score <= 100
        # Generated keywords are never labelled observed (and vice versa).
        relations = {e.relation.value for e in by_keyword[k.id]}
        if k.origin is KeywordOrigin.OBSERVED_QUERY:
            assert "query_observed" in relations, query
        else:
            assert "query_observed" not in relations, query
    # Synthetic items record what they simulate; real evidence types are never faked.
    assert {i.evidence_type for i in items} == {EvidenceType.SYNTHETIC_TEST_DATA}
    assert all(i.simulated_evidence_type is not None for i in items)

    # Lifecycle: each keyword starts at DISCOVERED; transitions are logged by discovery only.
    first: dict[int, KeywordStatusHistoryRow] = {}
    for h in sorted(history, key=lambda h: (h.changed_at, h.id)):
        first.setdefault(h.keyword_id, h)
        assert h.actor == DISCOVERY_ACTOR
        assert h.reason
        assert h.to_status is not CandidateStatus.MONITORING
    assert {h.from_status for h in first.values()} == {None}
    assert {h.to_status for h in first.values()} == {CandidateStatus.DISCOVERED}
    assert set(first) == {k.id for k in kw.values()}

    # Equivalent install queries share an intent cluster; distinct intents do not.
    install = {
        kw[q].intent_cluster_id for q in ("how to install nimbus coder 2", "nimbus coder 2 install")
    }
    assert len(install) == 1
    (install_id,) = install
    assert install_id is not None
    assert clusters[install_id].cluster_key.endswith(":install")
    assert kw["nimbus coder 2 benchmark"].intent_cluster_id != install_id
    assert kw["nimbus coder 2 pricing"].intent_cluster_id not in (
        install_id,
        kw["nimbus coder 2 benchmark"].intent_cluster_id,
    )
    # Different versions never share a topic.
    assert (
        kw["nimbus coder 2 benchmark"].topic_cluster_id
        != kw["nimbus coder 1.5 benchmark"].topic_cluster_id
    )
    assert kw["how to install nimbus coder 2"].intent_assignment["method"] in (
        "new",
        "exact",
        "lexical",
    )

    # Suggestions/related queries are discovery evidence, not search-volume observations.
    assert count(session_factory, SearchInterestObservationRow) == 0


async def test_rerun_is_idempotent(
    container: AppContainer, session_factory: SessionFactory
) -> None:
    await run_demo(container)
    before = {
        m.__tablename__: count(session_factory, m)
        for m in (DiscoveryItemRow, DiscoveredKeywordRow, DiscoveryEvidenceRow, DiscoveredEntityRow)
    }
    statuses = {q: k.candidate_status for q, k in keywords(session_factory).items()}
    results = dict(await run_demo(container))
    after = {
        m.__tablename__: count(session_factory, m)
        for m in (DiscoveryItemRow, DiscoveredKeywordRow, DiscoveryEvidenceRow, DiscoveredEntityRow)
    }
    assert after == before
    assert all(c.items_new == 0 for c in results["current"].categories.values())
    assert all(c.candidates_new == 0 for c in results["current"].categories.values())
    assert {q: k.candidate_status for q, k in keywords(session_factory).items()} == statuses
    assert count(session_factory, DiscoveryRunRow) == 12  # 2 demo runs x 2 stages x 3 categories


async def test_single_category_and_dry_run(
    container: AppContainer, session_factory: SessionFactory, clock: MutableClock
) -> None:
    scenario = load_scenario()
    from trend_agent.discovery.demo import StageClock
    from trend_agent.discovery.providers.synthetic import SyntheticDiscoveryProvider

    stage_clock = StageClock(scenario.stage_time("current"))
    container.registry.register(SyntheticDiscoveryProvider(scenario, clock=stage_clock))
    service = container.discovery(clock=stage_clock)
    plan_only = await service.run(
        [Category.GAMING], sources=scenario.sources_for("current"), synthetic=True, dry_run=True
    )
    assert plan_only.dry_run
    assert count(session_factory, DiscoveredKeywordRow) == 0
    assert count(session_factory, DiscoveryItemRow) == 0

    summary = await service.run(
        [Category.GAMING], sources=scenario.sources_for("current"), synthetic=True, force=True
    )
    assert set(summary.categories) == {"gaming"}
    assert {o.category for o in summary.sources} == {Category.GAMING}
    cats = {k.primary_category for k in keywords(session_factory).values()}
    assert cats == {Category.GAMING}


async def test_live_run_without_access_never_fabricates(
    container: AppContainer, session_factory: SessionFactory
) -> None:
    """No API keys and an egress policy that denies every host: nothing is invented."""
    summary = await container.discovery().run(TARGET_CATEGORIES, max_fetches=12)
    by_status: dict[str, set[str]] = {}
    for outcome in summary.sources:
        by_status.setdefault(outcome.status, set()).add(outcome.error_type or "")
    assert "access_restricted" in by_status.get("failed", set())
    reasons = " ".join(s["reason"] for s in summary.skipped)
    assert "not_configured" in reasons  # YouTube / Reddit / Trends without credentials
    with session_factory() as s:
        evidence_types = set(s.execute(sa.select(DiscoveryItemRow.evidence_type)).scalars())
    assert evidence_types <= {EvidenceType.CURATED_SEED}
    kw = keywords(session_factory)
    assert not [k for k in kw.values() if k.origin is not KeywordOrigin.GENERATED_HYPOTHESIS]
    assert not [k for k in kw.values() if k.discovery_type is not DiscoveryMethod.SEED_TERM]
    assert count(session_factory, SearchInterestObservationRow) == 0


async def test_retention_purge_cascades_to_evidence(
    container: AppContainer, session_factory: SessionFactory, clock: MutableClock
) -> None:
    await run_demo(container)
    evidence_before = count(session_factory, DiscoveryEvidenceRow)
    with session_factory.begin() as s:
        target = s.execute(
            sa.select(DiscoveryItemRow.id)
            .join(DiscoveryEvidenceRow, DiscoveryEvidenceRow.item_id == DiscoveryItemRow.id)
            .limit(1)
        ).scalar_one()
        s.execute(
            sa.update(DiscoveryItemRow)
            .where(DiscoveryItemRow.id == target)
            .values(expires_at=clock() - timedelta(minutes=1))
        )
    service = RetentionService(session_factory, clock)
    assert service.report().expired["discovery_items"] == 1
    assert service.purge_expired()["discovery_items"] == 1
    assert count(session_factory, DiscoveryEvidenceRow) < evidence_before
    with session_factory() as s:
        assert s.get(DiscoveryItemRow, target) is None
        assert not s.execute(
            sa.select(EntityMentionRow).where(EntityMentionRow.item_id == target)
        ).first()


# ---------------------------------------------------------------------------- CLI


@pytest.fixture
def wired_cli(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: SessionFactory,
    fake_youtube: FakeYouTube,
    clock: MutableClock,
) -> None:
    monkeypatch.setattr(
        cli, "SETTINGS_FACTORY", lambda **kw: make_settings(discovery_config_dir=str(CONFIG_DIR))
    )
    monkeypatch.setattr(
        cli,
        "CONTAINER_FACTORY",
        lambda settings, **kw: build_container(
            settings,
            clock=clock,
            http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
            discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
            **kw,
        ),
    )


def invoke(*args: str, code: int = 0) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == code, result.output
    return result.output


def as_json(output: str) -> Any:
    start = min(i for i in (output.find("{"), output.find("[")) if i >= 0)
    return json.loads(output[start:])


@pytest.mark.usefixtures("wired_cli")
def test_cli_discover_commands() -> None:
    demo = invoke("discover", "demo")
    assert "SYNTHETIC DEMO" in demo
    assert "not search volume" in demo

    # Synthetic data is hidden unless asked for.
    assert "No candidates found" in invoke("discover", "keywords", "--category", "gaming")
    rows = as_json(
        invoke(
            "discover",
            "keywords",
            "--category",
            "gaming",
            "--include-synthetic",
            "--limit",
            "5",
            "--json",
        )
    )
    assert 0 < len(rows) <= 5
    assert all(r["synthetic"] for r in rows)
    assert rows == sorted(rows, key=lambda r: -r["priority"])

    shown = as_json(invoke("discover", "show", "--query", "glacier lance location", "--json"))
    assert shown["origin"] == "generated_hypothesis"
    assert shown["evidence"]
    assert shown["history"][0]["to"] == "discovered"
    assert shown["priority"]["version"] == "priority-v1"
    assert shown["intent_scores_uncalibrated"]  # not presented as calibrated probabilities

    ents = as_json(
        invoke(
            "discover", "entities", "--recent", "--days", "3650", "--include-synthetic", "--json"
        )
    )
    names = {e["name"] for e in ents}
    assert {"Nimbus Coder 2", "Frostfall Festival", "Lattice 5.0"} <= names

    expanded = as_json(invoke("discover", "expand", "--entity", "Glacier Lance", "--json"))
    assert expanded[0]["hypotheses"]
    assert all(h["grounding"] for h in expanded[0]["hypotheses"])

    sources = as_json(invoke("discover", "sources", "--json"))
    assert any(s["provider"] == "feeds" for s in sources["sources"])
    reddit = next(s for s in sources["sources"] if s["provider"] == "reddit")
    assert reddit["ready"] is False
    assert reddit["not_ready_reason"].startswith("not_configured")
    assert "Candidates" not in invoke("discover", "clusters", "--include-synthetic")
    stats = as_json(invoke("discover", "stats", "--json"))
    assert stats["total_keywords"] == sum(stats["by_category"].values())

    dry = as_json(invoke("discover", "run", "--category", "gaming", "--dry-run", "--json"))
    assert dry["dry_run"] is True
    invoke("discover", "run", code=2)  # a category (or --all) is required
    invoke("discover", "expand", "--entity", "No Such Thing", code=1)


@pytest.mark.usefixtures("session_factory")
def test_cli_missing_discovery_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(
        cli,
        "SETTINGS_FACTORY",
        lambda **kw: make_settings(discovery_config_dir=str(tmp_path / "x")),
    )
    result = runner.invoke(cli.app, ["discover", "run", "--all", "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "discovery configuration" in result.output
