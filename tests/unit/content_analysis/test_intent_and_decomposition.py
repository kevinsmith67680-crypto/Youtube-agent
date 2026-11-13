from __future__ import annotations

import pytest

from tests.unit.content_analysis.helpers import LOADED
from trend_agent.content_analysis.entity_verification import QueryEntities
from trend_agent.content_analysis.intent_classifier import (
    classify,
    merge_llm_suggestions,
    policy_flags,
)
from trend_agent.content_analysis.query_decomposition import _generic_requirement, decompose
from trend_agent.schemas.search_intent import (
    ConfidenceLabel,
    PrimaryIntent,
    RequirementPriority,
    SubIntent,
)

CFG = LOADED.config
NONE = QueryEntities()


@pytest.mark.parametrize(
    ("query", "category", "primary", "sub"),
    [
        ("how to install the new coding model locally", "ai", PrimaryIntent.TUTORIAL, None),
        ("best settings for the arena shooter", "gaming", PrimaryIntent.GAMING_GUIDE,
         SubIntent.GAME_SETTINGS),
        ("app crashes on startup fix", "technology", PrimaryIntent.PROBLEM_SOLVING, None),
        ("model x vs model y", "ai", PrimaryIntent.COMPARISON, None),
        ("phone review", "technology", PrimaryIntent.REVIEW, None),
        ("glimmer shards farming", "gaming", PrimaryIntent.GAMING_GUIDE,
         SubIntent.STRATEGY_GUIDE),
    ],
)  # fmt: skip
def test_rule_based_primary_and_sub_intent(
    query: str, category: str, primary: PrimaryIntent, sub: SubIntent | None
) -> None:
    intent = classify(query, NONE, CFG, category=category)
    assert intent.primary_intent is primary
    if sub is not None:
        assert intent.sub_intent is sub
    assert intent.method.value == "deterministic_rules"
    assert intent.confidence in tuple(ConfidenceLabel)


def test_no_cue_defaults_to_informational_with_low_confidence_and_ambiguity_note() -> None:
    intent = classify("zylophone", NONE, CFG, category=None)
    assert intent.primary_intent is PrimaryIntent.INFORMATIONAL
    assert intent.confidence is ConfidenceLabel.LOW
    assert intent.ambiguity


@pytest.mark.parametrize(
    "query",
    [
        "skyforge arena aimbot undetected",
        "free skyforge coins generator",
        "how to bypass anti-cheat",
        "hack someone's account password",
        "dupe glitch exploit",
    ],
)
def test_policy_flags_cheats_and_account_abuse(query: str) -> None:
    assert policy_flags(query, CFG.policy.prohibited_patterns)


def test_legitimate_gaming_queries_are_not_flagged() -> None:
    for q in ("aim training routine", "best loadout season 8", "how to earn coins legitimately"):
        assert not policy_flags(q, CFG.policy.prohibited_patterns), q


def test_llm_suggestions_only_add_secondary_intents_when_rules_are_confident() -> None:
    base = classify("how to install the editor", NONE, CFG, category="technology")
    merged = merge_llm_suggestions(base, [(PrimaryIntent.NEWS, None)])
    assert merged.primary_intent is base.primary_intent  # never overridden
    assert PrimaryIntent.NEWS in merged.secondary_intents


def test_item_location_template_has_blocking_identification_requirements() -> None:
    intent = classify("where to find the lantern season 8", NONE, CFG, category="gaming")
    dec = decompose(intent, NONE)
    keys = {r.key: r for r in dec.requirements}
    assert keys["item_exists"].blocking
    assert keys["item_exists"].priority is RequirementPriority.ESSENTIAL
    # Nothing in the requirements invents an item location or product detail.
    assert not any(ch.isdigit() for r in dec.requirements for ch in r.requirement)


def test_llm_requirement_suggestions_must_be_generic() -> None:
    assert _generic_requirement("Common setup errors and their fixes", NONE)
    assert _generic_requirement("Version 3.2 adds 40 new commands", NONE) is None
    assert _generic_requirement("How Zentrix Cloud handles billing", NONE) is None


def test_attribute_question_gets_an_attribute_requirement() -> None:
    from tests.unit.content_analysis.helpers import analyse

    out = analyse("nova", [], query="nova lens battery life")
    reqs = {r.key: r for r in out.decomposition.requirements}
    assert reqs["focus"].priority is RequirementPriority.ESSENTIAL
    assert "battery life" in reqs["focus"].requirement
