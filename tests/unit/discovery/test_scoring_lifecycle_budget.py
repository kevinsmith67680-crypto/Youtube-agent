"""Evidence quality/corroboration, novelty, priority scoring, lifecycle and budget planning."""

from __future__ import annotations

import dataclasses
import itertools
from datetime import date, timedelta

import pytest

from tests.unit.discovery.helpers import LOADED, NOW
from trend_agent.discovery.budget import SourceState, allocate_slots, build_plan, poll_interval
from trend_agent.discovery.config import DiscoveryConfig, ScoringConfig
from trend_agent.discovery.evidence import (
    EvidenceView,
    claimed_release_dates,
    corroboration_score,
    evidence_quality,
    independent_origins,
    quality_flags,
    resolve_release_date,
)
from trend_agent.discovery.lifecycle import (
    DISCOVERY_ACTOR,
    MONITORING_ACTOR,
    InvalidTransitionError,
    check_origin_change,
    status_after_scoring,
    transition,
    validate_keyword,
)
from trend_agent.discovery.models import (
    CandidateStatus,
    Category,
    DiscoverySource,
    EntityStatus,
    EntityType,
    EvidenceType,
    KeywordOrigin,
    NoveltyKind,
    SearchIntent,
    SourceHealth,
    SourcePool,
    SourceType,
)
from trend_agent.discovery.scoring import (
    ACTIONABILITY,
    NoveltyInputs,
    PriorityComponents,
    actionability,
    decay,
    freshness,
    novelty,
    priority,
    relevance,
)

S = CandidateStatus
ARTICLE = (
    "Corvid Labs today released Nimbus Coder 2, an open-weight coding model that runs locally "
    "and through the API. The company says the model handles repository-scale refactoring and "
    "ships with a 256k context window. Nimbus Coder 2 is available now under an open licence."
)


def ev(item_id: int, **kw: object) -> EvidenceView:
    base: dict[str, object] = {
        "item_id": item_id,
        "provider": "feeds",
        "evidence_type": EvidenceType.OFFICIAL_ANNOUNCEMENT,
        "source_reference": f"https://example.test/{item_id}",
        "title": f"title {item_id}",
        "collected_at": NOW,
    }
    base.update(kw)
    return EvidenceView(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- 12. evidence


def test_syndicated_copies_are_one_origin() -> None:
    original = ev(1, title="Nimbus Coder 2 released", excerpt=ARTICLE, publisher="Corvid Labs")
    repost = ev(
        2,
        title="Corvid Labs launches Nimbus Coder 2 coding model",  # rewritten headline
        excerpt=ARTICLE + " Read more on TechWire.",
        publisher="TechWire",
        url="https://techwire.test/nimbus",
    )
    second_repost = ev(
        3,
        title="DailyByte: Nimbus Coder 2",
        excerpt="Via the wire: " + ARTICLE,
        publisher="DailyByte",
        url="https://dailybyte.test/a",
    )
    independent = ev(
        4,
        provider="youtube",
        evidence_type=EvidenceType.VIDEO_TITLE,
        title="I tested Nimbus Coder 2 on a real project",
        author="Dev Channel",
    )
    groups = independent_origins([original, repost, second_repost, independent])
    assert len(groups) == 2
    assert sorted(len(g) for g in groups) == [1, 3]


def test_same_url_publisher_or_author_is_one_origin() -> None:
    same_url = [
        ev(1, url="https://a.test/post?utm_source=x", title="one"),
        ev(2, url="https://a.test/post", title="completely different"),
    ]
    assert len(independent_origins(same_url)) == 1
    same_publisher = [ev(1, publisher="Corvid Labs"), ev(2, publisher="corvid labs", title="x y")]
    assert len(independent_origins(same_publisher)) == 1
    same_author = [
        ev(1, provider="youtube", author="Dev Channel", title="video one about stuff"),
        ev(2, provider="youtube", author="dev channel", title="another unrelated video"),
    ]
    assert len(independent_origins(same_author)) == 1


def test_short_unrelated_titles_are_not_merged() -> None:
    a = ev(1, title="Nimbus Coder 2 install guide", url="https://a.test/1")
    b = ev(2, title="Nimbus Coder 2 benchmark results", url="https://b.test/1")
    assert len(independent_origins([a, b])) == 2


def test_corroboration_has_diminishing_returns() -> None:
    assert corroboration_score(0) == 0.0
    scores = [corroboration_score(n) for n in range(1, 6)]
    assert scores == sorted(scores)
    assert scores[1] - scores[0] > scores[4] - scores[3]
    assert scores[-1] < 100.0


def test_quality_flags_and_penalties() -> None:
    flags = quality_flags(
        evidence_type=EvidenceType.COMMUNITY_DISCUSSION,
        title="rumor: Void Crown",
        text="Leaked: a Void Crown item might be coming",
        url=None,
        published_at=None,
        collected_at=NOW,
        max_age_days=365,
    )
    assert {"missing_published_at", "speculative", "low_quality_claim"} <= flags
    outdated = quality_flags(
        evidence_type=EvidenceType.CODE_RELEASE,
        title="Lattice 2.0",
        text=None,
        url="https://x.test",
        published_at=NOW - timedelta(days=800),
        collected_at=NOW,
        max_age_days=365,
    )
    assert outdated == frozenset({"outdated"})
    future = quality_flags(
        evidence_type=EvidenceType.CODE_RELEASE,
        title="x",
        text=None,
        url="https://x.test",
        published_at=NOW + timedelta(days=5),
        collected_at=NOW,
        max_age_days=365,
    )
    assert "future_dated" in future
    clean = ev(1, reliability=0.9)
    flagged = dataclasses.replace(clean, flags=frozenset({"speculative", "missing_published_at"}))
    assert evidence_quality(flagged) < evidence_quality(clean)
    worst = ev(2, flags=frozenset({"outdated", "speculative", "missing_published_at"}))
    assert 0.0 <= evidence_quality(worst) < evidence_quality(flagged)


def test_release_dates_are_sentence_scoped_and_conflicts_detected() -> None:
    text = (
        "Corvid Labs released Nimbus Coder 2 on March 1, 2026. "
        "Haldane's Nimbus 2 thermostat launched 2025-10-02."
    )
    assert claimed_release_dates(text, ["Nimbus Coder 2"]) == [date(2026, 3, 1)]
    assert claimed_release_dates(text, ["Nimbus 2"]) == [date(2025, 10, 2)]
    assert claimed_release_dates("Planned for 2026-04-01 maybe", []) == []  # no release cue
    chosen, conflict = resolve_release_date(
        [
            (date(2026, 3, 4), EvidenceType.COMMUNITY_DISCUSSION),
            (date(2026, 3, 1), EvidenceType.OFFICIAL_ANNOUNCEMENT),
        ]
    )
    assert chosen == date(2026, 3, 1)  # official source wins
    assert conflict is True
    assert resolve_release_date([(date(2026, 3, 1), EvidenceType.CODE_RELEASE)]) == (
        date(2026, 3, 1),
        False,
    )


# ---------------------------------------------------------------------------- 13. novelty


def nov(**kw: object) -> NoveltyInputs:
    base: dict[str, object] = {
        "now": NOW,
        "has_entity": True,
        "entity_new_this_run": True,
        "entity_first_seen": NOW,
        "entity_type": EntityType.AI_MODEL,
        "parent_known_before": False,
        "evergreen": False,
        "query_new_this_run": True,
        "query_first_seen": NOW,
        "intent_cluster_new": True,
        "topic_existed_before": False,
        "release_date": NOW,
    }
    base.update(kw)
    return NoveltyInputs(**base)  # type: ignore[arg-type]


def test_novelty_kinds() -> None:
    assert novelty(nov(), 7).kind is NoveltyKind.NEW_ENTITY
    assert novelty(nov(parent_known_before=True), 7).kind is NoveltyKind.NEW_VERSION
    event = nov(parent_known_before=True, entity_type=EntityType.GAME_EVENT)
    assert novelty(event, 7).kind is NoveltyKind.NEW_EVENT
    new_intent = nov(entity_new_this_run=False, topic_existed_before=True)
    assert novelty(new_intent, 7).kind is NoveltyKind.NEW_INTENT
    recurring = nov(entity_new_this_run=False, intent_cluster_new=False)
    assert novelty(recurring, 7).kind is NoveltyKind.RECURRING
    assert novelty(nov(evergreen=True), 7).kind is NoveltyKind.EVERGREEN


def test_novelty_decays_with_age_and_unknown_dates_are_neutral() -> None:
    fresh = novelty(nov(), 7).score
    month_old = novelty(
        nov(
            entity_new_this_run=False,
            entity_first_seen=NOW - timedelta(days=30),
            query_new_this_run=False,
            query_first_seen=NOW - timedelta(days=30),
            intent_cluster_new=False,
            release_date=NOW - timedelta(days=30),
        ),
        7,
    ).score
    assert fresh > month_old
    unknown = novelty(nov(release_date=None), 7)
    assert unknown.components["recency"] == 30.0
    assert novelty(nov(evergreen=True), 7).score < fresh


# ---------------------------------------------------------------------------- 14. priority


def components(**kw: float) -> PriorityComponents:
    base = {
        "novelty": 80.0,
        "freshness": 90.0,
        "corroboration": 51.0,
        "relevance": 70.0,
        "intent_actionability": 100.0,
        "evidence_quality": 75.0,
    }
    base.update(kw)
    return PriorityComponents(**base)


def test_priority_is_weighted_sum_with_stored_components() -> None:
    config = ScoringConfig()
    result = priority(components(), config)
    expected = 0.25 * 80 + 0.20 * 90 + 0.20 * 51 + 0.15 * 70 + 0.10 * 100 + 0.10 * 75
    assert result.score == pytest.approx(expected, abs=0.01)
    assert result.version == "priority-v1"
    assert sum(result.contributions.values()) == pytest.approx(result.score, abs=0.01)
    as_dict = result.as_dict()
    assert "not search volume" in as_dict["note"]
    assert set(as_dict["components"]) == {
        "novelty",
        "freshness",
        "corroboration",
        "relevance",
        "intent_actionability",
        "evidence_quality",
    }
    assert not any("volume" in key or "demand" in key for key in as_dict["components"])
    # Deterministic.
    assert priority(components(), config) == result


def test_priority_components_are_bounded_and_monotonic() -> None:
    with pytest.raises(ValueError, match="outside 0-100"):
        components(novelty=120.0)
    with pytest.raises(ValueError, match="outside 0-100"):
        components(freshness=float("nan"))
    config = ScoringConfig()
    low = priority(components(corroboration=30.0), config).score
    high = priority(components(corroboration=66.0), config).score
    assert high > low


def test_scoring_helpers() -> None:
    assert decay(0, 7) == 100.0
    assert decay(7, 7) == 50.0
    assert decay(-3, 7) == 100.0  # future timestamps are not "fresher than fresh"
    assert freshness(None, NOW, 7) == 0.0
    assert freshness(NOW - timedelta(days=14), NOW, 7) == 25.0
    assert relevance({Category.AI: 1.0}, Category.AI, 100) == 100.0
    assert relevance({Category.OTHER: 1.0}, Category.OTHER, 100) == 0.0
    assert set(ACTIONABILITY) == set(SearchIntent)
    assert actionability(SearchIntent.HOW_TO) > actionability(SearchIntent.NEWS)


# ---------------------------------------------------------------------------- 15. lifecycle


def test_lifecycle_transitions() -> None:
    t = transition(None, S.DISCOVERED, reason="new", actor=DISCOVERY_ACTOR, at=NOW)
    assert t.from_status is None
    path = [S.DISCOVERED, S.VALIDATED, S.PRIORITIZED, S.DORMANT, S.PRIORITIZED]
    for current, target in itertools.pairwise(path):
        transition(current, target, reason="step", actor=DISCOVERY_ACTOR, at=NOW)
    for bad_from, bad_to in [
        (S.DISCOVERED, S.PRIORITIZED),
        (S.REJECTED, S.VALIDATED),
        (S.DISCOVERED, S.MONITORING),
    ]:
        with pytest.raises(InvalidTransitionError):
            transition(bad_from, bad_to, reason="x", actor=DISCOVERY_ACTOR, at=NOW)
    with pytest.raises(InvalidTransitionError, match="start as discovered"):
        transition(None, S.VALIDATED, reason="x", actor=DISCOVERY_ACTOR, at=NOW)
    with pytest.raises(InvalidTransitionError, match="needs a reason"):
        transition(S.DISCOVERED, S.VALIDATED, reason="", actor=DISCOVERY_ACTOR, at=NOW)


def test_only_monitoring_actor_can_start_monitoring() -> None:
    with pytest.raises(InvalidTransitionError, match="only the monitoring system"):
        transition(S.PRIORITIZED, S.MONITORING, reason="go", actor=DISCOVERY_ACTOR, at=NOW)
    ok = transition(S.PRIORITIZED, S.MONITORING, reason="go", actor=MONITORING_ACTOR, at=NOW)
    assert ok.to_status is S.MONITORING


def test_origin_guard() -> None:
    with pytest.raises(InvalidTransitionError, match="verified interest"):
        check_origin_change(
            KeywordOrigin.OBSERVED_QUERY, KeywordOrigin.VERIFIED_INTEREST, DISCOVERY_ACTOR
        )
    with pytest.raises(InvalidTransitionError, match="downgraded"):
        check_origin_change(
            KeywordOrigin.OBSERVED_QUERY, KeywordOrigin.GENERATED_HYPOTHESIS, DISCOVERY_ACTOR
        )
    check_origin_change(
        KeywordOrigin.GENERATED_HYPOTHESIS, KeywordOrigin.OBSERVED_QUERY, DISCOVERY_ACTOR
    )


@pytest.mark.parametrize(
    ("canonical", "origin", "category", "entity_status", "valid"),
    [
        ("nimbus coder 2 install", KeywordOrigin.OBSERVED_QUERY, Category.AI, None, True),
        (
            "how to install nimbus coder 2",
            KeywordOrigin.GENERATED_HYPOTHESIS,
            Category.AI,
            EntityStatus.VERIFIED,
            True,
        ),
        (
            "nimbus coder 3 release date",
            KeywordOrigin.GENERATED_HYPOTHESIS,
            Category.AI,
            EntityStatus.NEEDS_VERIFICATION,
            False,
        ),
        ("nimbus", KeywordOrigin.OBSERVED_QUERY, Category.AI, None, False),
        ("best pizza near me", KeywordOrigin.OBSERVED_QUERY, Category.OTHER, None, False),
        (
            "void crown location",
            KeywordOrigin.OBSERVED_QUERY,
            Category.GAMING,
            EntityStatus.REJECTED,
            False,
        ),
        (" ".join(["word"] * 12), KeywordOrigin.OBSERVED_QUERY, Category.AI, None, False),
    ],
)
def test_keyword_validation(
    canonical: str,
    origin: KeywordOrigin,
    category: Category,
    entity_status: EntityStatus | None,
    valid: bool,
) -> None:
    outcome = validate_keyword(
        canonical=canonical,
        origin=origin,
        category=category,
        entity_status=entity_status,
        max_words=10,
    )
    assert outcome.valid is valid, outcome.reason


def test_status_after_scoring() -> None:
    assert status_after_scoring(S.VALIDATED, 60, 45) is S.PRIORITIZED
    assert status_after_scoring(S.PRIORITIZED, 30, 45) is S.VALIDATED
    assert status_after_scoring(S.DORMANT, 60, 45) is S.PRIORITIZED
    assert status_after_scoring(S.DORMANT, 10, 45) is S.VALIDATED
    assert status_after_scoring(S.MONITORING, 10, 45) is S.MONITORING
    assert status_after_scoring(S.REJECTED, 99, 45) is S.REJECTED


# ---------------------------------------------------------------------------- 16. budget


def src(key: str, category: Category, **kw: object) -> DiscoverySource:
    base: dict[str, object] = {
        "key": key,
        "provider": "feeds",
        "name": key,
        "source_type": SourceType.FEED,
        "category": category,
        "reference": f"https://example.test/{key}",
    }
    base.update(kw)
    return DiscoverySource(**base)  # type: ignore[arg-type]


def always(_: DiscoverySource) -> tuple[bool, str]:
    return True, ""


def seen(key: str, hours_ago: float = 48, **kw: object) -> SourceState:
    return SourceState(key=key, last_checked_at=NOW - timedelta(hours=hours_ago), **kw)  # type: ignore[arg-type]


def test_slot_allocation_follows_category_shares() -> None:
    shares = {Category.AI: 0.45, Category.TECHNOLOGY: 0.25, Category.GAMING: 0.30}
    all_three = allocate_slots(20, shares, list(shares))
    assert all_three == {Category.AI: 9, Category.TECHNOLOGY: 5, Category.GAMING: 6}
    odd = allocate_slots(7, shares, list(shares))
    assert sum(odd.values()) == 7
    assert odd[Category.AI] >= odd[Category.GAMING] >= odd[Category.TECHNOLOGY]
    single = allocate_slots(20, shares, [Category.GAMING])
    assert single == {Category.GAMING: 6}  # own share, not the whole run
    assert allocate_slots(1, shares, [Category.TECHNOLOGY]) == {Category.TECHNOLOGY: 1}
    assert LOADED.config.category_budgets.share(Category.AI) == 0.45


def test_plan_reserves_exploration_and_records_skips() -> None:
    config = DiscoveryConfig()
    sources = [src(f"ai-{i}", Category.AI, reliability=0.5 + i / 20) for i in range(8)]
    sources.append(src("ai-new", Category.AI, pool=SourcePool.EXPLORATION, reliability=0.3))
    sources.append(src("ai-off", Category.AI, enabled=False))
    states = {f"ai-{i}": seen(f"ai-{i}") for i in range(8)}
    plan = build_plan(
        sources,
        states,
        categories=[Category.AI],
        config=config,
        now=NOW,
        provider_ready=always,
        quota_ok=always,
        max_fetches=10,
    )
    assert plan.slots == {Category.AI: 5}
    assert plan.exploration_slots[Category.AI] == 1
    pools = [f.pool for f in plan.fetches]
    assert pools.count(SourcePool.EXPLORATION) == 1
    assert "ai-new" in {f.source.key for f in plan.fetches}
    reasons = {s.source_key: s.reason for s in plan.skipped}
    assert reasons["ai-off"] == "disabled in configuration"
    assert sum(r == "category budget exhausted" for r in reasons.values()) == 4
    # Highest reliability exploitation sources are chosen first.
    exploit = [f.source.key for f in plan.fetches if f.pool is SourcePool.EXPLOITATION]
    assert exploit == ["ai-7", "ai-6", "ai-5", "ai-4"]


def test_plan_respects_intervals_backoff_readiness_and_quota() -> None:
    config = DiscoveryConfig()
    sources = [
        src("recent", Category.GAMING, min_poll_interval_minutes=360),
        src("failing", Category.GAMING, min_poll_interval_minutes=60),
        src("unready", Category.GAMING, provider="reddit"),
        src("broke", Category.GAMING, provider="youtube_discovery"),
        src("ok", Category.GAMING),
    ]
    states = {
        "recent": seen("recent", hours_ago=2),
        # 3 failures -> 8x the 60-minute interval: 5 hours ago is still too soon.
        "failing": seen(
            "failing", hours_ago=5, consecutive_failures=3, health=SourceHealth.FAILING
        ),
    }
    assert poll_interval(sources[1], states["failing"], 16) == timedelta(hours=8)
    assert poll_interval(sources[1], seen("x", consecutive_failures=10), 16) == timedelta(hours=16)

    def ready(source: DiscoverySource) -> tuple[bool, str]:
        return (False, "not_configured") if source.provider == "reddit" else (True, "")

    def quota(source: DiscoverySource) -> tuple[bool, str]:
        return (False, "quota exhausted") if source.provider == "youtube_discovery" else (True, "")

    plan = build_plan(
        sources,
        states,
        categories=[Category.GAMING],
        config=config,
        now=NOW,
        provider_ready=ready,
        quota_ok=quota,
        max_fetches=20,
    )
    reasons = {s.source_key: s.reason for s in plan.skipped}
    assert reasons["recent"].startswith("polled recently")
    assert reasons["failing"].startswith("polled recently")
    assert reasons["unready"] == "not_configured"
    assert reasons["broke"] == "quota exhausted"
    assert [f.source.key for f in plan.fetches] == ["ok"]
    # Unfillable slots stay unused; nothing is invented to meet the share.
    assert plan.unused_slots[Category.GAMING] == plan.slots[Category.GAMING] - 1

    forced = build_plan(
        sources,
        states,
        categories=[Category.GAMING],
        config=config,
        now=NOW,
        provider_ready=ready,
        quota_ok=quota,
        max_fetches=20,
        respect_intervals=False,
    )
    assert {f.source.key for f in forced.fetches} == {"ok", "recent", "failing"}


def test_unused_exploration_slots_flow_to_exploitation() -> None:
    sources = [src(f"t-{i}", Category.TECHNOLOGY) for i in range(6)]
    states = {f"t-{i}": seen(f"t-{i}") for i in range(6)}
    plan = build_plan(
        sources,
        states,
        categories=[Category.TECHNOLOGY],
        config=DiscoveryConfig(),
        now=NOW,
        provider_ready=always,
        quota_ok=always,
        max_fetches=16,
    )
    assert plan.slots[Category.TECHNOLOGY] == 4
    assert len(plan.fetches) == 4
    assert all(f.pool is SourcePool.EXPLOITATION for f in plan.fetches)
