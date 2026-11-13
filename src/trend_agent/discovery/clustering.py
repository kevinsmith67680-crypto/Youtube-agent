"""Two-level clustering: topic (subject/entity) and intent (information need).

* A **topic cluster** groups keywords about one subject. With a linked entity the topic is
  the entity itself, so different versions, seasons or events (distinct entities) never
  share a topic. Without an entity, topics are matched lexically and never across
  differing version numbers.
* An **intent cluster** groups queries in one topic that satisfy the same need:
  "how to install X", "X installation tutorial" and "install X guide" merge; "X benchmark"
  and "X pricing" stay separate.

Every assignment returns a :class:`ClusterDecision` recording method, score, threshold and
the member it matched, so each merge is auditable. The default backend is lexical (no
model downloads); an embedding backend can be supplied via :class:`SimilarityBackend`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from trend_agent.discovery.models import SearchIntent
from trend_agent.discovery.text import STOPWORDS, jaccard, numbers_in, tokens

INTENT_FAMILY: dict[SearchIntent, str] = {
    SearchIntent.HOW_TO: "instructional",
    SearchIntent.TUTORIAL: "instructional",
    SearchIntent.GAME_GUIDE: "guide",
    SearchIntent.CHALLENGE_GUIDE: "guide",
    SearchIntent.ITEM_LOCATION: "location",
    SearchIntent.COMPARISON: "comparison",
    SearchIntent.REVIEW: "review",
    SearchIntent.DEMONSTRATION: "demonstration",
    SearchIntent.TROUBLESHOOTING: "troubleshooting",
    SearchIntent.EXPLANATION: "explanation",
    SearchIntent.NEWS: "news",
    SearchIntent.PRODUCT_DISCOVERY: "product_discovery",
    SearchIntent.OTHER: "other",
}

# Words that carry the intent family but not the specific need.
_FILLER = (
    frozenset(
        """
    how what why when where tutorial tutorials guide guides step steps full complete
    beginner beginners easy quick ultimate explained way ways best video
    """.split()
    )
    | STOPWORDS
)

# Normalise surface variants of the same need.
_SYNONYMS: dict[str, str] = {
    "installation": "install",
    "installing": "install",
    "installer": "install",
    "setup": "install",
    "set": "install",
    "download": "install",
    "benchmarks": "benchmark",
    "benchmarking": "benchmark",
    "price": "pricing",
    "prices": "pricing",
    "cost": "pricing",
    "costs": "pricing",
    "locations": "location",
    "find": "location",
    "where": "location",
    "versus": "vs",
    "compared": "vs",
    "comparison": "vs",
    "reviews": "review",
    "challenges": "challenge",
    "quests": "quest",
    "rewards": "reward",
    "errors": "error",
    "crashing": "crash",
    "crashes": "crash",
    "upgrading": "upgrade",
    "migrate": "migration",
    "migrating": "migration",
    "locally": "local",
    "features": "feature",
    "changes": "change",
}


class SimilarityBackend(Protocol):
    name: str

    def similarity(
        self, a: frozenset[str], b: frozenset[str], text_a: str, text_b: str
    ) -> float: ...


class LexicalSimilarity:
    name = "lexical-jaccard-v1"

    def similarity(self, a: frozenset[str], b: frozenset[str], text_a: str, text_b: str) -> float:
        return jaccard(a, b)


@dataclass(frozen=True)
class ClusterDecision:
    level: str  # "topic" | "intent"
    cluster_key: str
    method: str  # entity | exact | lexical | new
    score: float
    threshold: float | None
    matched: str | None
    backend: str
    facet: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_NOT_PLURAL = frozenset(
    "status bonus campus corpus virus focus plus news series species chaos atlas canvas "
    "alias analysis basis linux macos ios windows always".split()
)


def _norm_word(word: str) -> str:
    word = _SYNONYMS.get(word, word)
    # Minimal plural folding ("gpus" -> "gpu"); deliberately conservative.
    if (
        len(word) > 3
        and word.endswith("s")
        and not word.endswith(("ss", "ous", "ius", "sis"))
        and word not in _NOT_PLURAL
    ):
        word = _SYNONYMS.get(word[:-1], word[:-1])
    return word


def facet_tokens(query: str, entity_name: str | None) -> frozenset[str]:
    """Tokens describing the need, with entity words and filler removed."""
    entity_words = set(tokens(entity_name)) if entity_name else set()
    out: set[str] = set()
    for word in tokens(query):
        if word in entity_words or word in _FILLER:  # raw form first ("does" is filler)
            continue
        word = _norm_word(word)
        if word in _FILLER or word in entity_words:
            continue
        out.add(word)
    return frozenset(out)


def topic_core(query: str) -> frozenset[str]:
    return frozenset(_norm_word(w) for w in tokens(query) if w not in _FILLER)


@dataclass
class _IntentMember:
    facet: frozenset[str]
    family: str
    query: str
    cluster_key: str


@dataclass
class _Topic:
    key: str
    canonical: str
    core: frozenset[str]
    intents: list[_IntentMember] = field(default_factory=list)


class Clusterer:
    """Stateful assigner; seed it with existing clusters, then assign new keywords."""

    def __init__(
        self,
        *,
        intent_threshold: float = 0.5,
        topic_threshold: float = 0.6,
        backend: SimilarityBackend | None = None,
    ) -> None:
        self.intent_threshold = intent_threshold
        self.topic_threshold = topic_threshold
        self.backend: SimilarityBackend = backend or LexicalSimilarity()
        self._topics: dict[str, _Topic] = {}

    # -- seeding from the database
    def add_topic(self, key: str, canonical: str) -> None:
        self._topics.setdefault(key, _Topic(key, canonical, topic_core(canonical)))

    def add_intent_member(
        self,
        topic_key: str,
        cluster_key: str,
        query: str,
        intent: SearchIntent,
        entity_name: str | None,
    ) -> None:
        topic = self._topics.get(topic_key)
        if topic is None:
            return
        topic.intents.append(
            _IntentMember(
                facet_tokens(query, entity_name), INTENT_FAMILY[intent], query, cluster_key
            )
        )

    # -- assignment
    def assign_topic(
        self, query: str, entity_key: str | None, entity_name: str | None
    ) -> tuple[str, str, ClusterDecision]:
        if entity_key is not None and entity_name is not None:
            key = f"entity:{entity_key}"
            existed = key in self._topics
            self.add_topic(key, entity_name)
            return (
                key,
                entity_name,
                ClusterDecision(
                    "topic",
                    key,
                    "entity",
                    1.0,
                    None,
                    entity_name if existed else None,
                    "entity-identity",
                ),
            )
        core = topic_core(query)
        numbers = numbers_in(query)
        best: tuple[float, _Topic] | None = None
        for topic in self._topics.values():
            if topic.key.startswith("entity:") or numbers_in(topic.canonical) != numbers:
                continue
            score = self.backend.similarity(core, topic.core, query, topic.canonical)
            if score >= self.topic_threshold and (best is None or score > best[0]):
                best = (score, topic)
        if best is not None:
            score, topic = best
            method = "exact" if score >= 1.0 else "lexical"
            return (
                topic.key,
                topic.canonical,
                ClusterDecision(
                    "topic",
                    topic.key,
                    method,
                    round(score, 4),
                    self.topic_threshold,
                    topic.canonical,
                    self.backend.name,
                    tuple(sorted(core)),
                ),
            )
        key = "lex:" + "-".join(sorted(core))[:150] if core else f"lex:{query}"
        canonical = " ".join(w for w in tokens(query) if w in core) or query
        self._topics[key] = _Topic(key, canonical, core)
        return (
            key,
            canonical,
            ClusterDecision(
                "topic",
                key,
                "new",
                0.0,
                self.topic_threshold,
                None,
                self.backend.name,
                tuple(sorted(core)),
            ),
        )

    def assign_intent(
        self, topic_key: str, query: str, intent: SearchIntent, entity_name: str | None
    ) -> tuple[str, ClusterDecision]:
        topic = self._topics[topic_key]
        facet = facet_tokens(query, entity_name)
        family = INTENT_FAMILY[intent]
        numbers = numbers_in(" ".join(facet))
        best: tuple[float, _IntentMember] | None = None
        for member in topic.intents:
            if member.family != family or numbers_in(" ".join(member.facet)) != numbers:
                continue
            score = self.backend.similarity(facet, member.facet, query, member.query)
            if score >= self.intent_threshold and (best is None or score > best[0]):
                best = (score, member)
        if best is not None:
            score, member = best
            decision = ClusterDecision(
                "intent",
                member.cluster_key,
                "exact" if score >= 1.0 else "lexical",
                round(score, 4),
                self.intent_threshold,
                member.query,
                self.backend.name,
                tuple(sorted(facet)),
            )
            cluster_key = member.cluster_key
        else:
            cluster_key = f"{family}:{'-'.join(sorted(facet)) or '_'}"
            # A facet key already taken in this topic by another family cannot collide
            # because the family prefixes the key.
            decision = ClusterDecision(
                "intent",
                cluster_key,
                "new",
                0.0,
                self.intent_threshold,
                None,
                self.backend.name,
                tuple(sorted(facet)),
            )
        topic.intents.append(_IntentMember(facet, family, query, cluster_key))
        return cluster_key, decision

    def topics(self) -> Iterable[tuple[str, str]]:
        return ((t.key, t.canonical) for t in self._topics.values())
