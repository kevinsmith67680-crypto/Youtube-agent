"""Search-intent and category classification (deterministic baseline).

Scores are heuristic rule weights normalised to sum to 1. They are **not** calibrated
probabilities: no labelled evaluation set exists yet. An alternative classifier (e.g. an
LLM) can be plugged in via :class:`IntentClassifier`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from trend_agent.discovery.models import Category, SearchIntent
from trend_agent.discovery.text import tokens

# Tie-break order when scores are equal: more specific / more actionable first.
_PRECEDENCE = [
    SearchIntent.ITEM_LOCATION,
    SearchIntent.TROUBLESHOOTING,
    SearchIntent.CHALLENGE_GUIDE,
    SearchIntent.COMPARISON,
    SearchIntent.HOW_TO,
    SearchIntent.TUTORIAL,
    SearchIntent.GAME_GUIDE,
    SearchIntent.REVIEW,
    SearchIntent.DEMONSTRATION,
    SearchIntent.PRODUCT_DISCOVERY,
    SearchIntent.EXPLANATION,
    SearchIntent.NEWS,
    SearchIntent.OTHER,
]


@dataclass(frozen=True)
class _Rule:
    name: str
    intent: SearchIntent
    pattern: re.Pattern[str]
    weight: float
    categories: frozenset[Category] | None = None  # None = any category


def _r(
    name: str,
    intent: SearchIntent,
    regex: str,
    weight: float = 1.0,
    categories: Iterable[Category] | None = None,
) -> _Rule:
    return _Rule(
        name,
        intent,
        re.compile(regex, re.IGNORECASE),
        weight,
        frozenset(categories) if categories is not None else None,
    )


_G = [Category.GAMING]
_NOT_G = [Category.AI, Category.TECHNOLOGY, Category.OTHER]

RULES: tuple[_Rule, ...] = (
    _r("how_to", SearchIntent.HOW_TO, r"\bhow (?:to|do|can|does)\b", 1.2),
    _r(
        "tutorial",
        SearchIntent.TUTORIAL,
        r"\b(tutorial|step by step|for beginners|course|walkthrough)\b",
        1.2,
        _NOT_G,
    ),
    _r("guide_tech", SearchIntent.TUTORIAL, r"\bguide\b", 1.0, _NOT_G),
    _r("install", SearchIntent.TUTORIAL, r"\b(install\w*|set ?up|setup|configure|deploy)\b", 1.0),
    _r("run_local", SearchIntent.HOW_TO, r"\brun\b.*\blocal(?:ly)?\b", 1.0),
    _r("build", SearchIntent.TUTORIAL, r"\b(build|create|make)\b.*\b(with|using)\b", 0.8),
    _r("upgrade", SearchIntent.TUTORIAL, r"\b(upgrade|migrat\w+)\b", 1.0),
    _r(
        "explain",
        SearchIntent.EXPLANATION,
        r"\b(what (?:is|are|can)|explained|explain|meaning|why)\b",
        1.2,
    ),
    _r(
        "features",
        SearchIntent.EXPLANATION,
        r"\b(new features|what'?s new|changes|breaking changes)\b",
        0.9,
    ),
    _r(
        "news",
        SearchIntent.NEWS,
        r"\b(news|announced|announcement|release date|leaks?|launch(?:ed)?|patch notes"
        r"|changelog)\b",
        1.0,
    ),
    _r("when", SearchIntent.NEWS, r"\bwhen (?:does|is|will)\b", 1.0),
    _r(
        "versus",
        SearchIntent.COMPARISON,
        r"(\bvs\.?\b|\bversus\b|\bcompared?\b|\bcomparison\b|\bbetter than\b|\bor\b.*\bwhich\b)",
        2.0,
    ),
    _r("benchmark_cmp", SearchIntent.COMPARISON, r"\bbenchmarks?\b", 1.0),
    _r("alternatives", SearchIntent.PRODUCT_DISCOVERY, r"\balternatives?\b", 1.2),
    _r(
        "review",
        SearchIntent.REVIEW,
        r"\b(review|worth it|hands[- ]on|impressions|tested|is it good)\b",
        1.5,
    ),
    _r("benchmark_rev", SearchIntent.REVIEW, r"\bbenchmarks?\b", 1.0),
    _r(
        "demo",
        SearchIntent.DEMONSTRATION,
        r"\b(demo|showcase|in action|first look|unboxing|test drive)\b",
        1.5,
    ),
    _r("gameplay", SearchIntent.DEMONSTRATION, r"\bgameplay\b", 1.2, _G),
    _r(
        "troubleshoot",
        SearchIntent.TROUBLESHOOTING,
        r"\b(fix|fixed|error|errors|crash\w*|not working|won'?t (?:start|load|launch)|issue"
        r"|issues|bug|bugs|problem|lag|stutter\w*|fps drops?|black screen)\b",
        2.0,
    ),
    _r(
        "game_guide",
        SearchIntent.GAME_GUIDE,
        r"\b(guide|tips|tricks|strategy|strategies|best (?:loadout|build|settings|class)"
        r"|tier list|meta|beginner'?s)\b",
        1.2,
        _G,
    ),
    _r("settings", SearchIntent.GAME_GUIDE, r"\bbest settings\b", 0.8, _G),
    _r(
        "item_where",
        SearchIntent.ITEM_LOCATION,
        r"\b(where (?:to|do|can|is|are)|locations?|spawns?|how to find|find)\b",
        2.0,
        _G,
    ),
    _r("item_get", SearchIntent.ITEM_LOCATION, r"\bhow to (?:get|unlock|obtain)\b", 1.5, _G),
    _r(
        "challenge",
        SearchIntent.CHALLENGE_GUIDE,
        r"\b(complete|challenges?|quests?|missions?|achievements?|trophy|trophies)\b",
        1.6,
        _G,
    ),
    _r(
        "discovery",
        SearchIntent.PRODUCT_DISCOVERY,
        r"\b(best|top \d+|top|free|pricing|price|cost|tools|apps)\b",
        0.8,
    ),
)


@dataclass(frozen=True)
class IntentResult:
    primary: SearchIntent
    scores: dict[SearchIntent, float]
    matched_rules: tuple[str, ...]

    def top(self, n: int = 3) -> list[tuple[SearchIntent, float]]:
        return sorted(self.scores.items(), key=lambda kv: (-kv[1], _PRECEDENCE.index(kv[0])))[:n]


class IntentClassifier(Protocol):
    name: str

    def classify(self, query: str, category: Category | None = None) -> IntentResult: ...


class RuleBasedIntentClassifier:
    name = "rules-v1"

    def classify(self, query: str, category: Category | None = None) -> IntentResult:
        raw: dict[SearchIntent, float] = {}
        matched: list[str] = []
        for rule in RULES:
            if rule.categories is not None and (category or Category.OTHER) not in rule.categories:
                continue
            if rule.pattern.search(query):
                raw[rule.intent] = raw.get(rule.intent, 0.0) + rule.weight
                matched.append(rule.name)
        if not raw:
            return IntentResult(SearchIntent.OTHER, {SearchIntent.OTHER: 1.0}, ())
        total = sum(raw.values())
        scores = {intent: round(value / total, 4) for intent, value in raw.items()}
        primary = min(scores, key=lambda i: (-scores[i], _PRECEDENCE.index(i)))
        return IntentResult(primary, scores, tuple(matched))


# ------------------------------------------------------------------------- category

BASE_VOCABULARY: dict[Category, frozenset[str]] = {
    Category.AI: frozenset(
        """
        ai llm llms gpt chatbot agent agents agentic model models genai prompt prompts
        transformer transformers neural inference fine-tune finetune finetuning embedding
        embeddings rag diffusion multimodal copilot assistant ml machine learning openai
        deepfake text-to-video text-to-image tts voice-clone weights gguf
        """.split()
    ),
    Category.TECHNOLOGY: frozenset(
        """
        gpu gpus cpu cpus laptop phone smartphone tablet linux windows macos android ios
        programming code coding developer developers framework library sdk api apis python
        javascript typescript rust golang java kubernetes docker cloud server database
        cybersecurity security vulnerability exploit malware hardware chip chips headset vr
        ar glasses wearable smartwatch robot robotics quantum software update upgrade
        install release version browser
        """.split()
    ),
    Category.GAMING: frozenset(
        """
        game games gaming gamer gameplay multiplayer season event events quest quests
        challenge challenges loot skin skins weapon weapons map maps boss raid level
        speedrun mod mods modding glitch patch esports battle royale fps rpg mmo indie
        console controller unlock reward rewards item items character characters
        """.split()
    ),
}


@dataclass(frozen=True)
class CategoryResult:
    primary: Category
    scores: dict[Category, float]
    reason: str


class CategoryClassifier:
    """Category from a linked entity when known, else from a vocabulary built from seeds."""

    def __init__(self, extra_vocabulary: dict[Category, set[str]] | None = None) -> None:
        self._vocab = {c: set(words) for c, words in BASE_VOCABULARY.items()}
        for category, words in (extra_vocabulary or {}).items():
            self._vocab.setdefault(category, set()).update(w.lower() for w in words)

    def classify(self, query: str, entity_category: Category | None = None) -> CategoryResult:
        words = tokens(query)
        bigrams = {f"{a} {b}" for a, b in itertools.pairwise(words)}
        terms = set(words) | bigrams
        raw = {c: float(len(terms & vocab)) for c, vocab in self._vocab.items()}
        if entity_category in raw and entity_category is not None:
            raw[entity_category] += 3.0  # entity category dominates; cross-category kept
        total = sum(raw.values())
        if total == 0:
            return CategoryResult(Category.OTHER, {Category.OTHER: 1.0}, "no category terms")
        scores = {c: round(v / total, 4) for c, v in raw.items() if v > 0}
        # Gaming context wins ties with AI ("gaming AI" is a gaming topic), then AI.
        order = [Category.GAMING, Category.AI, Category.TECHNOLOGY]
        primary = min(scores, key=lambda c: (-scores[c], order.index(c) if c in order else 9))
        reason = "entity" if entity_category is not None else "vocabulary"
        return CategoryResult(primary, scores, reason)
