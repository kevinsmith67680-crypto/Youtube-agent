"""Verification layer against the real test database (synthetic/fictional data, no network)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, InternalError
from typer.testing import CliRunner

from tests.helpers import FakeYouTube, make_settings
from tests.integration.test_detection import DAY, World, _container, _deny, count
from tests.unit.discovery.helpers import CONFIG_DIR
from trend_agent import cli
from trend_agent.container import build_container
from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveryItemRow,
    DiscoverySourceRow,
    EntityMentionRow,
    TopicClusterRow,
)
from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.db.verification_models import (
    EvidenceBundleRow,
    EvidenceRecordRow,
    VerificationAttemptRow,
    VerificationClaimRow,
    VerificationRunRow,
)
from trend_agent.detection import demo as ddemo
from trend_agent.discovery.models import (
    Category,
    EntityOrigin,
    EntityStatus,
    EntityType,
    EvidenceType,
    SourceHealth,
    SourcePool,
    SourceType,
)
from trend_agent.ingestion.retention import RetentionService
from trend_agent.schemas.detection import RunType
from trend_agent.schemas.verification import (
    AbstentionState,
    EvidenceStatus,
    PublicationDecision,
    ViolationCode,
)
from trend_agent.verification.generation import ScriptedGenerator, TemplateGenerator
from trend_agent.verification.permissions import read_only

runner = CliRunner()


def _seed_sources(factory: SessionFactory, target_id: int, now: Any) -> dict[str, int]:
    """A verified official feed item and an entity linked to the target's topic."""
    with transaction(factory) as s:
        source = DiscoverySourceRow(
            key="test:aurora-feed",
            provider_name="rss_feed",
            name="Aurora Labs blog (fictional)",
            source_type=SourceType.FEED,
            source_reference="https://aurora-labs.example/feed.xml",
            category=Category.AI,
            pool=SourcePool.EXPLOITATION,
            enabled=True,
            reliability=0.9,
            min_poll_interval_minutes=60,
            health_status=SourceHealth.HEALTHY,
            config={"verified": True},
        )
        s.add(source)
        s.flush()
        entity = DiscoveredEntityRow(
            entity_key="ai_product:nimbus-coder-3",
            canonical_name="Nimbus Coder 3",
            entity_type=EntityType.AI_PRODUCT,
            category=Category.AI,
            status=EntityStatus.VERIFIED,
            origin=EntityOrigin.EXTRACTED,
            confidence=0.9,
            first_seen_at=now - DAY * 3,
            last_seen_at=now,
        )
        s.add(entity)
        s.flush()
        item = DiscoveryItemRow(
            source_id=source.id,
            provider_name="rss_feed",
            source_reference="https://aurora-labs.example/blog/nimbus-coder-3",
            evidence_type=EvidenceType.OFFICIAL_ANNOUNCEMENT,
            is_synthetic=False,
            title="Aurora Labs releases Nimbus Coder 3",
            excerpt="Nimbus Coder 3 is available today and supports a 256K token context window.",
            url="https://aurora-labs.example/blog/nimbus-coder-3",
            publisher="Aurora Labs",
            published_at=now - DAY * 2,
            collected_at=now - DAY * 2,
            content_fingerprint="f" * 64,
            quality=80.0,
        )
        s.add(item)
        s.flush()
        s.add(
            EntityMentionRow(
                entity_id=entity.id,
                item_id=item.id,
                matched_text="Nimbus Coder 3",
                confidence=0.9,
                extractor="test",
                status=EntityStatus.VERIFIED,
            )
        )
        topic = TopicClusterRow(
            topic_key="ai:nimbus-coder-3",
            canonical_topic="Nimbus Coder 3",
            category=Category.AI,
            representative_entity_id=entity.id,
        )
        s.add(topic)
        s.flush()
        target = s.get(MonitoringTargetRow, target_id)
        assert target is not None
        target.topic_cluster_id = topic.id
        return {"item": item.id, "entity": entity.id, "topic": topic.id}


@pytest.fixture
def surge_world(session_factory: SessionFactory, fake_youtube: FakeYouTube) -> Iterator[World]:
    container = _container(lambda: ddemo.START, fake_youtube)
    w = World(container)
    for day in range(70, 76):
        w.advance(day)
        w.service.run(run_type=RunType.LIVE, from_queue=True)
    yield w
    container.engine.dispose()


def _service(w: World) -> Any:
    return w.container.verification(clock=w.clock)


def _draft(ids: dict[str, str], query_id: int, topic_id: int | None, **over: Any) -> str:
    item = next(v for k, v in ids.items() if k.startswith("discovery_item:"))
    draft: dict[str, Any] = {
        "proposed_video_title": "Nimbus Coder 3 Tutorial for Beginners",
        "topic_id": topic_id,
        "search_query_id": query_id,
        "audience_search_intent": "tutorial",
        "supporting_evidence_ids": [item],
        "factual_claims": [
            {
                "text": "Nimbus Coder 3 supports a 256K token context window.",
                "subject": "Nimbus Coder 3",
                "evidence_ids": [item],
            }
        ],
        "recommendation_rationale": "Beginners need a setup walkthrough.",
    }
    draft.update(over)
    return json.dumps(draft)


def test_recommend_registers_versioned_evidence_and_persists_the_decision(
    surge_world: World,
) -> None:
    w = surge_world
    target_id = w.targets["A"]
    seeded = _seed_sources(w.factory, target_id, w.clock.now)
    svc = _service(w)
    ctx = svc.build_context(target_id, include_synthetic=True)
    kinds = {r.evidence_key.split(":")[0] for r in ctx.records}
    assert {"discovery_item", "entity", "ts_obs", "surge_event"} <= kinds
    assert ctx.stats.available  # the Phase 4 episode became authoritative statistics
    assert ctx.stats.event_ids
    assert ctx.synthetic
    ids = {r.evidence_key: r.evidence_id for r in ctx.records}
    decision = svc.check(
        target_id,
        _draft(ids, ctx.query_id, seeded["topic"]),
        include_synthetic=True,
    )
    rec = decision.recommendation
    assert rec.publication is not PublicationDecision.WITHHOLD
    assert rec.synthetic
    assert rec.trend_statement.startswith("[SYNTHETIC]")
    assert rec.factual_claims[0].status.value == "verified"
    assert count(w.factory, VerificationRunRow) == 1
    assert count(w.factory, VerificationAttemptRow) == 1
    assert count(w.factory, VerificationClaimRow) >= 2
    assert count(w.factory, EvidenceBundleRow) == 2  # build_context + check
    with transaction(w.factory) as s:
        run = s.get(VerificationRunRow, __import__("uuid").UUID(rec.run_id))
        assert run is not None
        assert run.output["verification_status"] == rec.verification_status.value
        assert run.steps[-1]["name"] == "10_persist_decision"
    # Registration is idempotent: same content, same IDs, no new versions.
    before = count(w.factory, EvidenceRecordRow)
    again = svc.build_context(target_id, include_synthetic=True)
    assert {r.evidence_id for r in again.records} == set(ids.values())
    assert count(w.factory, EvidenceRecordRow) == before
    # A changed source registers a new version and supersedes the old one.
    with transaction(w.factory) as s:
        item = s.get(DiscoveryItemRow, seeded["item"])
        assert item is not None
        item.excerpt = "Nimbus Coder 3 is available today with a 512K token context window."
    changed = svc.build_context(target_id, include_synthetic=True)
    key = f"discovery_item:{seeded['item']}"
    new = next(r for r in changed.records if r.evidence_key == key)
    assert new.version == 2
    assert new.evidence_id != ids[key]
    with transaction(w.factory) as s:
        old = s.execute(
            sa.select(EvidenceRecordRow).where(EvidenceRecordRow.evidence_id == ids[key])
        ).scalar_one()
        assert old.status is EvidenceStatus.SUPERSEDED
        assert not old.is_current
    rec_check, problem = svc.verify_record(new.evidence_id)
    assert rec_check is not None
    assert problem is None


def test_evidence_content_is_immutable_in_the_database(surge_world: World) -> None:
    w = surge_world
    svc = _service(w)
    ctx = svc.build_context(w.targets["A"], include_synthetic=True)
    eid = ctx.records[0].evidence_id
    with pytest.raises((DBAPIError, InternalError)), transaction(w.factory) as s:
        s.execute(
            sa.update(EvidenceRecordRow)
            .where(EvidenceRecordRow.evidence_id == eid)
            .values(excerpt="Nimbus Coder 3 has 99 million searches.")
        )
    with pytest.raises((DBAPIError, InternalError)), transaction(w.factory) as s:
        s.execute(
            sa.update(EvidenceRecordRow)
            .where(EvidenceRecordRow.evidence_id == eid)
            .values(trust_level="official", is_synthetic=False)
        )
    # A forged row (written around the trigger by dropping it) fails signature checks.
    with transaction(w.factory) as s:
        s.execute(
            sa.text("ALTER TABLE evidence_records DISABLE TRIGGER evidence_records_immutable")
        )
        s.execute(
            sa.update(EvidenceRecordRow)
            .where(EvidenceRecordRow.evidence_id == eid)
            .values(publisher="Forged Publisher")
        )
        s.execute(sa.text("ALTER TABLE evidence_records ENABLE TRIGGER evidence_records_immutable"))
    _, problem = svc.verify_record(eid)
    assert problem == "content hash mismatch"


def test_assembly_is_read_only_and_synthetic_targets_need_opt_in(surge_world: World) -> None:
    w = surge_world

    def write_in_read_only_transaction() -> None:
        with transaction(w.factory) as s:
            read_only(s)
            s.execute(sa.text("DELETE FROM verification_runs"))

    with pytest.raises(DBAPIError):
        write_in_read_only_transaction()
    svc = _service(w)
    with pytest.raises(PermissionError, match="synthetic"):
        svc.build_context(w.targets["A"])
    with pytest.raises(LookupError):
        svc.build_context(10_000_000)


def test_substituted_and_fabricated_references_are_rejected_against_the_db(
    surge_world: World,
) -> None:
    w = surge_world
    target_a = w.targets["A"]
    other = next(t for k, t in w.targets.items() if k != "A")
    svc = _service(w)
    other_ctx = svc.build_context(other, include_synthetic=True)
    foreign = other_ctx.records[0].evidence_id  # real, but issued for another request
    ctx = svc.build_context(target_a, include_synthetic=True)
    ids = {r.evidence_key: r.evidence_id for r in ctx.records}
    if not any(k.startswith("discovery_item:") for k in ids):
        _seed_sources(w.factory, target_a, w.clock.now)
        ctx = svc.build_context(target_a, include_synthetic=True)
        ids = {r.evidence_key: r.evidence_id for r in ctx.records}
    raw = _draft(
        ids,
        ctx.query_id,
        None,
        factual_claims=[
            {"text": "Nimbus Coder 3 has 99 million users.", "evidence_ids": [foreign]},
            {"text": "Nimbus Coder 3 supports video.", "evidence_ids": ["ev_" + "q" * 26]},
        ],
    )
    decision = svc.check(target_a, raw, include_synthetic=True)
    codes = set(decision.recommendation.violations)
    assert ViolationCode.EVIDENCE_NOT_IN_BUNDLE.value in codes
    assert ViolationCode.UNKNOWN_EVIDENCE_ID.value in codes
    assert not any("99 million" in c.text for c in decision.recommendation.factual_claims)


def test_template_recommendation_and_retention(surge_world: World) -> None:
    w = surge_world
    target_id = w.targets["A"]
    _seed_sources(w.factory, target_id, w.clock.now)
    svc = _service(w)
    decision = svc.recommend(target_id, TemplateGenerator(), include_synthetic=True)
    assert decision.recommendation.verification_status in (
        AbstentionState.VERIFIED_RESULT,
        AbstentionState.PARTIALLY_VERIFIED,
    )
    refused = svc.recommend(target_id, ScriptedGenerator(["@not json"] * 3), include_synthetic=True)
    assert refused.recommendation.verification_status is AbstentionState.SOURCE_UNAVAILABLE
    runs = svc.runs(include_synthetic=True)
    assert len(runs) == 2
    explained = svc.explain(runs[0]["run_id"])
    assert explained is not None
    assert explained["steps"]
    # Records carry their source's expiry and are purged with it.
    with transaction(w.factory) as s:
        s.execute(
            sa.text("ALTER TABLE evidence_records DISABLE TRIGGER evidence_records_immutable")
        )
        s.execute(sa.update(EvidenceRecordRow).values(expires_at=w.clock.now - timedelta(days=1)))
        s.execute(sa.text("ALTER TABLE evidence_records ENABLE TRIGGER evidence_records_immutable"))
        s.execute(sa.update(VerificationRunRow).values(expires_at=w.clock.now - timedelta(days=1)))
        s.execute(sa.update(EvidenceBundleRow).values(expires_at=w.clock.now - timedelta(days=1)))
    deleted = RetentionService(w.factory, w.clock).purge_expired()
    assert deleted["verification_runs"] == 2
    assert deleted["evidence_records"] > 0
    assert count(w.factory, EvidenceRecordRow) == 0
    assert count(w.factory, VerificationClaimRow) == 0


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
            clock=lambda: ddemo.START + DAY * 76,
            http_client=httpx.AsyncClient(transport=fake_youtube.transport()),
            discovery_http_client=httpx.AsyncClient(transport=httpx.MockTransport(_deny)),
            **kw,
        ),
    )


def invoke(*args: str, code: int = 0) -> str:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == code, result.output
    return result.output


def test_cli_verify_commands(surge_world: World, wired_cli: None) -> None:
    target = str(surge_world.targets["A"])
    out = invoke("verify", "recommend", "--target", target, "--include-synthetic", "--json")
    data = json.loads(out)
    assert data["schema_version"] == "verified-recommendation/1.0"
    run_id = data["run_id"]
    assert "run" in invoke("verify", "runs", "--include-synthetic")
    assert "10_persist_decision" in invoke("verify", "explain", run_id)
    invoke("verify", "explain", "not-a-uuid", code=2)
    evidence = json.loads(invoke("verify", "evidence", "--json", "--limit", "5"))
    assert evidence
    one = json.loads(invoke("verify", "evidence", "--id", evidence[0]["evidence_id"], "--json"))
    assert one[0]["integrity"] == "ok"
    assert "runs_by_state" in invoke("verify", "stats")
    invoke("verify", "recommend", "--target", target, code=2)  # synthetic without opt-in
    invoke("verify", "recommend", "--target", target, "--generator", "nope", code=2)
