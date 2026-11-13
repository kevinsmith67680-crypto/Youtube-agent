"""Novelty and discovery-priority scoring.

The priority score ranks candidates for *future monitoring*. It is not a search-volume or
demand score and contains no search-volume input. Weights are initial heuristics
(``priority-v1``), not experimentally validated predictors. Every component is 0-100,
individually testable, stored with the score, and recomputable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from trend_agent.discovery.config import ScoringConfig
from trend_agent.discovery.models import (
    TARGET_CATEGORIES,
    Category,
    EntityType,
    NoveltyKind,
    SearchIntent,
)

ACTIONABILITY: dict[SearchIntent, float] = {
    SearchIntent.HOW_TO: 100.0,
    SearchIntent.TUTORIAL: 95.0,
    SearchIntent.ITEM_LOCATION: 95.0,
    SearchIntent.CHALLENGE_GUIDE: 90.0,
    SearchIntent.TROUBLESHOOTING: 90.0,
    SearchIntent.COMPARISON: 80.0,
    SearchIntent.GAME_GUIDE: 80.0,
    SearchIntent.REVIEW: 70.0,
    SearchIntent.DEMONSTRATION: 65.0,
    SearchIntent.PRODUCT_DISCOVERY: 60.0,
    SearchIntent.EXPLANATION: 55.0,
    SearchIntent.NEWS: 40.0,
    SearchIntent.OTHER: 20.0,
}

UNKNOWN_DATE_SCORE = 30.0  # unknown is neither fresh (100) nor stale (0)


def decay(age_days: float, half_life_days: float) -> float:
    """100 at age 0, 50 at one half-life; future timestamps count as age 0."""
    return round(100.0 * math.pow(0.5, max(0.0, age_days) / half_life_days), 2)


def age_days(then: datetime, now: datetime) -> float:
    return (now - then).total_seconds() / 86400.0


# ------------------------------------------------------------------------- novelty


@dataclass(frozen=True)
class NoveltyInputs:
    """Timestamps only, so re-processing the same evidence at the same time scores the same.

    "New" means first seen within the novelty window (default: one half-life) - not "first
    seen in this particular run", which would make scores depend on how runs were batched.
    """

    now: datetime
    has_entity: bool
    entity_first_seen: datetime | None
    entity_type: EntityType | None
    ancestor_known_before: bool  # a parent/grandparent was known before this entity appeared
    evergreen: bool
    query_first_seen: datetime | None
    intent_cluster_first_seen: datetime | None  # earliest member of the keyword's intent cluster
    topic_first_seen: datetime | None  # earliest member of the keyword's topic
    release_date: datetime | None


@dataclass(frozen=True)
class NoveltyResult:
    score: float
    kind: NoveltyKind
    components: dict[str, float]


def _is_new(first_seen: datetime | None, now: datetime, window_days: float) -> bool:
    return first_seen is None or age_days(first_seen, now) <= window_days


def novelty(
    inputs: NoveltyInputs, half_life_days: float, new_window_days: float | None = None
) -> NoveltyResult:
    now = inputs.now
    window = new_window_days if new_window_days is not None else half_life_days
    if not inputs.has_entity:
        entity = 30.0
    elif inputs.evergreen:
        entity = 10.0
    elif inputs.entity_first_seen is None:
        entity = 100.0
    else:
        entity = max(10.0, decay(age_days(inputs.entity_first_seen, now), half_life_days))
    if inputs.query_first_seen is None:
        query = 100.0
    else:
        query = max(5.0, decay(age_days(inputs.query_first_seen, now), half_life_days))
    intent_new = _is_new(inputs.intent_cluster_first_seen, now, window)
    topic_existed = (
        inputs.topic_first_seen is not None
        and inputs.intent_cluster_first_seen is not None
        and inputs.topic_first_seen < inputs.intent_cluster_first_seen
    )
    new_intent_score = 80.0 if topic_existed else 60.0
    intent = new_intent_score if intent_new else 20.0
    recency = (
        decay(age_days(inputs.release_date, now), half_life_days)
        if inputs.release_date is not None
        else UNKNOWN_DATE_SCORE
    )
    score = round(0.40 * entity + 0.25 * query + 0.15 * intent + 0.20 * recency, 2)

    entity_new = inputs.has_entity and _is_new(inputs.entity_first_seen, now, window)
    if inputs.has_entity and inputs.evergreen:
        kind = NoveltyKind.EVERGREEN
    elif entity_new:
        if inputs.ancestor_known_before and inputs.entity_type in (
            EntityType.GAME_EVENT,
            EntityType.GAME_UPDATE,
            EntityType.GAME_ITEM,
        ):
            kind = NoveltyKind.NEW_EVENT
        elif inputs.ancestor_known_before:
            kind = NoveltyKind.NEW_VERSION
        else:
            kind = NoveltyKind.NEW_ENTITY
    elif intent_new and topic_existed:
        kind = NoveltyKind.NEW_INTENT
    else:
        kind = NoveltyKind.RECURRING
    return NoveltyResult(
        score=score,
        kind=kind,
        components={"entity": entity, "query": query, "intent": intent, "recency": recency},
    )


# ------------------------------------------------------------------------- components


def freshness(newest_evidence: datetime | None, now: datetime, half_life_days: float) -> float:
    if newest_evidence is None:
        return 0.0
    return decay(age_days(newest_evidence, now), half_life_days)


def relevance(
    category_scores: dict[Category, float], primary: Category, seed_priority: float | None
) -> float:
    if primary not in TARGET_CATEGORIES:
        return 0.0
    confidence = category_scores.get(primary, 0.0)
    seed = seed_priority if seed_priority is not None else 50.0
    return round(0.6 * 100.0 * confidence + 0.4 * seed, 2)


def actionability(intent: SearchIntent) -> float:
    return ACTIONABILITY[intent]


# ------------------------------------------------------------------------- priority


@dataclass(frozen=True)
class PriorityComponents:
    novelty: float
    freshness: float
    corroboration: float
    relevance: float
    intent_actionability: float
    evidence_quality: float

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not 0.0 <= value <= 100.0 or math.isnan(value):
                raise ValueError(f"component {name}={value} outside 0-100")


@dataclass(frozen=True)
class PriorityScore:
    version: str
    score: float
    components: dict[str, float]
    contributions: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "score": self.score,
            "components": self.components,
            "contributions": self.contributions,
            "note": "discovery priority for monitoring; not search volume or demand",
        }


def priority(components: PriorityComponents, config: ScoringConfig) -> PriorityScore:
    weights = config.weights.model_dump()
    values = asdict(components)
    contributions = {k: round(weights[k] * values[k], 4) for k in weights}
    score = round(sum(contributions.values()), 2)
    return PriorityScore(config.version, score, values, contributions)
