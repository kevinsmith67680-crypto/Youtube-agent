"""Intent/category classification, keyword expansion, normalisation, deduplication, clustering."""

from __future__ import annotations

import pytest

from trend_agent.discovery.clustering import ClusterDecision, Clusterer, facet_tokens
from trend_agent.discovery.config import ExpansionConfig
from trend_agent.discovery.expansion import (
    EntityView,
    cap_candidates,
    expand_entity,
    make_candidate,
)
from trend_agent.discovery.intent import CategoryClassifier, RuleBasedIntentClassifier
from trend_agent.discovery.models import (
    Category,
    DiscoveryMethod,
    EntityStatus,
    EntityType,
    KeywordOrigin,
    SearchIntent,
)
from trend_agent.utils.text import canonicalize_query

SI = SearchIntent
C = Category
classify = RuleBasedIntentClassifier().classify

# ---------------------------------------------------------------------------- 7. intents


@pytest.mark.parametrize(
    ("query", "category", "expected"),
    [
        # Examples from the Phase 2 brief.
        ("how to run an LLM locally", C.AI, {SI.HOW_TO, SI.TUTORIAL}),
        ("new GPU benchmark", C.TECHNOLOGY, {SI.REVIEW, SI.COMPARISON}),
        ("where to find Fortnite event item", C.GAMING, {SI.ITEM_LOCATION}),
        ("AI coding assistant vs competitor", C.AI, {SI.COMPARISON}),
        ("how to fix game crashing", C.GAMING, {SI.TROUBLESHOOTING}),
        # AI
        ("ollama tutorial for beginners", C.AI, {SI.TUTORIAL}),
        ("what is model context protocol", C.AI, {SI.EXPLANATION}),
        ("best free ai video generator", C.AI, {SI.PRODUCT_DISCOVERY}),
        ("nimbus coder 2 review", C.AI, {SI.REVIEW}),
        # technology
        ("smart glasses unboxing", C.TECHNOLOGY, {SI.DEMONSTRATION}),
        ("lattice 5.0 migration guide", C.TECHNOLOGY, {SI.TUTORIAL}),
        ("docker error permission denied fix", C.TECHNOLOGY, {SI.TROUBLESHOOTING}),
        ("rtx 5090 release date", C.TECHNOLOGY, {SI.NEWS}),
        # gaming
        ("how to complete frostfall festival challenges", C.GAMING, {SI.CHALLENGE_GUIDE}),
        ("glacier lance location", C.GAMING, {SI.ITEM_LOCATION}),
        ("how to unlock glacier lance", C.GAMING, {SI.ITEM_LOCATION}),
        ("best settings for fortnite", C.GAMING, {SI.GAME_GUIDE}),
        ("minecraft update gameplay", C.GAMING, {SI.DEMONSTRATION}),
        ("asdf qwerty", C.AI, {SI.OTHER}),
    ],
)
def test_intent_examples(query: str, category: Category, expected: set[SearchIntent]) -> None:
    result = classify(query, category)
    assert result.primary in expected, result
    assert sum(result.scores.values()) == pytest.approx(1.0, abs=1e-3)


def test_ambiguous_queries_keep_multiple_intents() -> None:
    result = classify("how to fix game crashing", C.GAMING)
    top = dict(result.top(3))
    assert SI.TROUBLESHOOTING in top
    assert SI.HOW_TO in top
    assert len(result.scores) >= 2
    assert classify("frostfall guide", C.GAMING).primary is SI.GAME_GUIDE
    assert classify("frostfall guide", C.AI).primary is SI.TUTORIAL  # category-aware rule


def test_category_classification_and_cross_category() -> None:
    cc = CategoryClassifier({C.GAMING: {"fortnite", "minecraft"}})
    assert cc.classify("fortnite chapter update").primary is C.GAMING
    assert cc.classify("how to run llm locally").primary is C.AI
    assert cc.classify("new gpu benchmark").primary is C.TECHNOLOGY
    gaming_ai = cc.classify("gaming AI npc mod")
    assert gaming_ai.primary is C.GAMING
    assert C.AI in gaming_ai.scores  # secondary category retained
    assert cc.classify("best gpu for local llm").primary is C.AI
    assert cc.classify("asdf").primary is C.OTHER
    # A linked entity's category dominates vocabulary.
    assert cc.classify("glacier lance location", entity_category=C.GAMING).primary is C.GAMING


# ---------------------------------------------------------------------------- 6/8. expansion


def view(
    name: str, entity_type: EntityType, attrs: frozenset[str] | set[str] = frozenset(), **kw: object
) -> EntityView:
    base: dict[str, object] = {
        "key": f"x|{entity_type.value}|{name.lower()}",
        "canonical_name": name,
        "entity_type": entity_type,
        "category": C.AI,
        "status": EntityStatus.VERIFIED,
        "confidence": 0.9,
        "attributes": frozenset(attrs),
    }
    base.update(kw)
    return EntityView(**base)  # type: ignore[arg-type]


CFG = ExpansionConfig()


def queries(report: object) -> set[str]:
    return {c.query for c in report.candidates}  # type: ignore[attr-defined]


def test_ai_model_expansion_is_grounded() -> None:
    report = expand_entity(
        view(
            "Nimbus Coder 2",
            EntityType.AI_MODEL,
            {"local", "api"},
            competitors=("Nimbus Coder 1.5",),
        ),
        CFG,
    )
    q = queries(report)
    assert {
        "how to run Nimbus Coder 2 locally",
        "how to install Nimbus Coder 2",
        "Nimbus Coder 2 api tutorial",
        "Nimbus Coder 2 benchmark",
        "what can Nimbus Coder 2 do",
        "Nimbus Coder 2 vs Nimbus Coder 1.5",
    } <= q
    assert "Nimbus Coder 2 pricing" not in q  # pricing not grounded
    assert ("ai_model.pricing", "not grounded: needs pricing") in report.skipped
    for c in report.candidates:
        assert c.origin is KeywordOrigin.GENERATED_HYPOTHESIS  # never "observed"
        assert c.method in (DiscoveryMethod.ENTITY_TEMPLATE, DiscoveryMethod.ENTITY_COMPARISON)


def test_gaming_event_and_item_expansion() -> None:
    event = expand_entity(
        view(
            "Frostfall Festival",
            EntityType.GAME_EVENT,
            {"challenges", "rewards", "limited_time"},
            category=C.GAMING,
            parent_game="Skyforge Arena",
        ),
        CFG,
    )
    assert {
        "Skyforge Arena Frostfall Festival guide",
        "how to complete Frostfall Festival",
        "Frostfall Festival rewards",
        "when does Frostfall Festival end",
    } <= queries(event)
    item = expand_entity(
        view(
            "Glacier Lance", EntityType.GAME_ITEM, category=C.GAMING, parent_game="Skyforge Arena"
        ),
        CFG,
    )
    assert {
        "how to unlock Glacier Lance",
        "where to find Glacier Lance in Skyforge Arena",
    } <= queries(item)
    unverified = expand_entity(
        view(
            "Void Crown",
            EntityType.GAME_ITEM,
            category=C.GAMING,
            status=EntityStatus.NEEDS_VERIFICATION,
        ),
        CFG,
    )
    assert unverified.candidates == []
    assert "not expanded" in unverified.skipped[0][1]


def test_software_release_gating() -> None:
    major = expand_entity(
        view(
            "Lattice 5.0",
            EntityType.FRAMEWORK,
            {"breaking", "bugs"},
            release_level="major",
            competitors=("Lattice 4.0",),
        ),
        CFG,
    )
    assert {
        "how to upgrade to Lattice 5.0",
        "Lattice 5.0 breaking changes",
        "Lattice 5.0 migration guide",
        "how to fix Lattice 5.0 errors",
        "Lattice 5.0 vs Lattice 4.0",
        "how to install Lattice 5.0",
    } <= queries(major)
    minor = expand_entity(
        view(
            "LangChain 1.5",
            EntityType.FRAMEWORK,
            {"breaking"},
            release_level="minor",
            competitors=("LangChain 1.4",),
        ),
        CFG,
    )
    assert queries(minor) == {"LangChain 1.5 new features"}
    assert (
        expand_entity(
            view("Lattice 5.0.1", EntityType.FRAMEWORK, release_level="patch"), CFG
        ).candidates
        == []
    )
    # AI model names are not semver: a "minor" number is still a real release.
    assert expand_entity(
        view("Gemini 2.5", EntityType.AI_MODEL, release_level="minor"), CFG
    ).candidates


def test_superseded_code_releases_are_not_expanded() -> None:
    old_minor = view(
        "SDK 3.42", EntityType.SOFTWARE, release_level="minor", superseded_by="SDK 3.50"
    )
    report = expand_entity(old_minor, CFG)
    assert report.candidates == []
    assert "superseded by SDK 3.50" in report.skipped[0][1]
    # AI model versions are not semver releases: an older model stays a topic.
    model = view("Nimbus Coder 1.5", EntityType.AI_MODEL, superseded_by="Nimbus Coder 2")
    assert expand_entity(model, CFG).candidates


def test_curated_and_low_confidence_entities_are_not_expanded() -> None:
    assert expand_entity(view("Fortnite", EntityType.GAME, evergreen=True), CFG).candidates == []
    assert expand_entity(view("X", EntityType.AI_MODEL, confidence=0.3), CFG).candidates == []
    assert (
        expand_entity(view("X 2", EntityType.AI_MODEL), ExpansionConfig(max_depth=0)).candidates
        == []
    )


def test_expansion_limits_prevent_explosion() -> None:
    small = ExpansionConfig(max_templates_per_entity=2, max_comparisons_per_entity=1)
    report = expand_entity(
        view("M 2", EntityType.AI_MODEL, {"local", "api", "pricing"}, competitors=("A", "B", "C")),
        small,
    )
    templates = [c for c in report.candidates if c.method is DiscoveryMethod.ENTITY_TEMPLATE]
    comparisons = [c for c in report.candidates if c.method is DiscoveryMethod.ENTITY_COMPARISON]
    assert len(templates) == 2
    assert len(comparisons) == 1
    assert any("limit reached" in reason for _, reason in report.skipped)
    many = list(report.candidates) * 3
    capped, dropped = cap_candidates(many, 4)
    assert len(capped) == 4
    assert dropped == len(many) - 4


def test_observed_queries_rank_first_when_capping() -> None:
    observed = make_candidate(
        "nimbus coder 2 install",
        origin=KeywordOrigin.OBSERVED_QUERY,
        method=DiscoveryMethod.OBSERVED_QUERY,
        category=C.AI,
    )
    generated = make_candidate(
        "aaa first alphabetically",
        origin=KeywordOrigin.GENERATED_HYPOTHESIS,
        method=DiscoveryMethod.ENTITY_TEMPLATE,
        category=C.AI,
    )
    assert observed
    assert generated
    capped, _ = cap_candidates([generated, observed], 1)
    assert capped == [observed]


# ---------------------------------------------------------------------------- 8. normalisation


def test_keyword_normalisation() -> None:
    assert (
        canonicalize_query("  How to Install   Nimbus Coder 2 ") == "how to install nimbus coder 2"
    )
    a = make_candidate(
        "Nimbus  Coder 2 TUTORIAL",
        origin=KeywordOrigin.GENERATED_HYPOTHESIS,
        method=DiscoveryMethod.ENTITY_TEMPLATE,
        category=C.AI,
    )
    assert a is not None
    assert a.canonical == "nimbus coder 2 tutorial"
    assert a.query == "Nimbus Coder 2 TUTORIAL"  # original preserved
    assert (
        make_candidate(
            "   ",
            origin=KeywordOrigin.GENERATED_HYPOTHESIS,
            method=DiscoveryMethod.ENTITY_TEMPLATE,
            category=C.AI,
        )
        is None
    )


# ---------------------------------------------------------------------------- 9-11. clustering


def assign(
    c: Clusterer, query: str, entity: str | None, intent: SearchIntent
) -> tuple[str, str, ClusterDecision]:
    key = f"ai|ai_model|{entity.lower()}" if entity else None
    topic, _canon, _ = c.assign_topic(query, key, entity)
    intent_key, decision = c.assign_intent(topic, query, intent, entity)
    return topic, intent_key, decision


def test_equivalent_install_queries_share_an_intent_cluster_with_trace() -> None:
    c = Clusterer()
    t1, i1, d1 = assign(c, "how to install nimbus coder 2", "Nimbus Coder 2", SI.HOW_TO)
    t2, i2, d2 = assign(c, "nimbus coder 2 installation tutorial", "Nimbus Coder 2", SI.TUTORIAL)
    t3, i3, _d3 = assign(c, "install nimbus coder 2 guide", "Nimbus Coder 2", SI.TUTORIAL)
    assert t1 == t2 == t3
    assert i1 == i2 == i3
    assert d1.method == "new"
    assert d2.method == "exact"
    assert d2.matched == "how to install nimbus coder 2"
    assert d2.as_dict()["backend"] == "lexical-jaccard-v1"


def test_distinct_intents_on_one_entity_stay_separate() -> None:
    c = Clusterer()
    _, install, _ = assign(c, "nimbus coder 2 installation", "Nimbus Coder 2", SI.TUTORIAL)
    _, bench, _ = assign(c, "nimbus coder 2 benchmark", "Nimbus Coder 2", SI.COMPARISON)
    _, price, _ = assign(c, "nimbus coder 2 pricing", "Nimbus Coder 2", SI.PRODUCT_DISCOVERY)
    assert len({install, bench, price}) == 3


def test_versions_and_events_get_separate_topics() -> None:
    c = Clusterer()
    t2, _, _ = assign(c, "how to install nimbus coder 2", "Nimbus Coder 2", SI.HOW_TO)
    t15, _, _ = assign(c, "how to install nimbus coder 1.5", "Nimbus Coder 1.5", SI.HOW_TO)
    assert t2 != t15
    # Entity-less topics never merge across differing numbers.
    a, _, _ = assign(c, "best gpu for llm", None, SI.PRODUCT_DISCOVERY)
    b, _, _ = assign(c, "best gpus for llms", None, SI.PRODUCT_DISCOVERY)
    d, _, _ = assign(c, "best gpu for llm 2026", None, SI.PRODUCT_DISCOVERY)
    assert a == b != d


def test_facets_strip_entity_context_and_filler() -> None:
    assert (
        facet_tokens("skyforge arena frostfall festival guide", "Frostfall Festival Skyforge Arena")
        == frozenset()
    )
    assert facet_tokens("when does frostfall festival end", "Frostfall Festival") == frozenset(
        {"end"}
    )
    assert facet_tokens("where to find glacier lance", "Glacier Lance") == frozenset({"location"})


class _AlwaysSimilar:
    name = "always"

    def similarity(self, a: frozenset[str], b: frozenset[str], text_a: str, text_b: str) -> float:
        return 1.0


def test_similarity_backend_is_pluggable() -> None:
    c = Clusterer(backend=_AlwaysSimilar())
    _, i1, _ = assign(c, "nimbus coder 2 tutorial", "Nimbus Coder 2", SI.TUTORIAL)
    _, i2, d = assign(c, "how to use nimbus coder 2", "Nimbus Coder 2", SI.HOW_TO)
    assert i1 == i2
    assert d.backend == "always"
