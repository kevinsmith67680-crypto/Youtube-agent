"""Keyword expansion: turn verified entities into *candidate* search phrases.

Every phrase produced here is a ``GENERATED_HYPOTHESIS``: a guess at what people might
search, never evidence that anyone did. Safeguards:

* Only ``VERIFIED`` entities are expanded (no keywords about unverified items/products).
* Templates that imply a capability ("run locally", "API", "pricing", "breaking changes",
  "challenges", ...) require that capability to be grounded in the entity's evidence.
* Comparisons only use competitors grounded in evidence (co-mentioned with a comparison
  cue) or a known earlier version of the same family. No competitor is invented.
* Hard limits per entity and per run prevent keyword explosion.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from trend_agent.discovery.config import ExpansionConfig
from trend_agent.discovery.models import (
    Category,
    DiscoveryMethod,
    EntityStatus,
    EntityType,
    KeywordOrigin,
    SearchIntent,
)
from trend_agent.utils.text import QueryValidationError, canonicalize_query, clean_original_query


@dataclass(frozen=True)
class IntentTemplate:
    id: str
    pattern: str  # placeholders: {e} entity, {game} parent game, {c} competitor
    intent: SearchIntent
    entity_types: frozenset[EntityType]
    requires: frozenset[str] = frozenset()  # grounded attributes required
    # None = any level. A None *member* means 'unversioned entity'.
    release_levels: frozenset[str | None] | None = None
    needs_parent_game: bool = False
    priority: int = 50  # higher = generated first when limits apply


def _t(
    id: str,
    pattern: str,
    intent: SearchIntent,
    types: Iterable[EntityType],
    *,
    requires: Iterable[str] = (),
    levels: Iterable[str | None] | None = None,
    game: bool = False,
    priority: int = 50,
) -> IntentTemplate:
    return IntentTemplate(
        id,
        pattern,
        intent,
        frozenset(types),
        frozenset(requires),
        frozenset(levels) if levels is not None else None,
        game,
        priority,
    )


_MODEL = [EntityType.AI_MODEL]
_AI_PRODUCT = [EntityType.AI_PRODUCT]
_SOFT = [EntityType.SOFTWARE, EntityType.FRAMEWORK, EntityType.RELEASE]
_HW = [EntityType.HARDWARE]
_MAJOR_MINOR: list[str | None] = ["major", "minor"]

TEMPLATES: tuple[IntentTemplate, ...] = (
    # --- AI models
    _t("ai_model.how_to_use", "how to use {e}", SearchIntent.HOW_TO, _MODEL, priority=90),
    _t("ai_model.tutorial", "{e} tutorial", SearchIntent.TUTORIAL, _MODEL, priority=85),
    _t(
        "ai_model.run_local",
        "how to run {e} locally",
        SearchIntent.HOW_TO,
        _MODEL,
        requires=["local"],
        priority=95,
    ),
    _t(
        "ai_model.install",
        "how to install {e}",
        SearchIntent.TUTORIAL,
        _MODEL,
        requires=["local"],
        priority=92,
    ),
    _t("ai_model.benchmark", "{e} benchmark", SearchIntent.COMPARISON, _MODEL, priority=80),
    _t(
        "ai_model.api",
        "{e} api tutorial",
        SearchIntent.TUTORIAL,
        _MODEL,
        requires=["api"],
        priority=75,
    ),
    _t(
        "ai_model.pricing",
        "{e} pricing",
        SearchIntent.PRODUCT_DISCOVERY,
        _MODEL,
        requires=["pricing"],
        priority=65,
    ),
    _t("ai_model.capabilities", "what can {e} do", SearchIntent.EXPLANATION, _MODEL, priority=60),
    _t("ai_model.review", "{e} review", SearchIntent.REVIEW, _MODEL, priority=55),
    # --- AI products / agents / tools
    _t("ai_product.how_to_use", "how to use {e}", SearchIntent.HOW_TO, _AI_PRODUCT, priority=90),
    _t("ai_product.tutorial", "{e} tutorial", SearchIntent.TUTORIAL, _AI_PRODUCT, priority=85),
    _t("ai_product.what_is", "what is {e}", SearchIntent.EXPLANATION, _AI_PRODUCT, priority=70),
    _t("ai_product.review", "{e} review", SearchIntent.REVIEW, _AI_PRODUCT, priority=65),
    _t(
        "ai_product.free",
        "is {e} free",
        SearchIntent.PRODUCT_DISCOVERY,
        _AI_PRODUCT,
        requires=["free"],
        priority=60,
    ),
    _t(
        "ai_product.pricing",
        "{e} pricing",
        SearchIntent.PRODUCT_DISCOVERY,
        _AI_PRODUCT,
        requires=["pricing"],
        priority=60,
    ),
    _t(
        "ai_product.api",
        "{e} api tutorial",
        SearchIntent.TUTORIAL,
        _AI_PRODUCT,
        requires=["api"],
        priority=70,
    ),
    _t(
        "ai_product.run_local",
        "how to run {e} locally",
        SearchIntent.HOW_TO,
        _AI_PRODUCT,
        requires=["local"],
        priority=80,
    ),
    # --- software / frameworks (releases)
    _t(
        "software.whats_new",
        "{e} new features",
        SearchIntent.EXPLANATION,
        _SOFT,
        levels=_MAJOR_MINOR,
        priority=85,
    ),
    _t(
        "software.install",
        "how to install {e}",
        SearchIntent.TUTORIAL,
        _SOFT,
        levels=["major", None],
        priority=80,
    ),
    _t(
        "software.upgrade",
        "how to upgrade to {e}",
        SearchIntent.TUTORIAL,
        _SOFT,
        levels=["major"],
        priority=90,
    ),
    _t(
        "software.breaking",
        "{e} breaking changes",
        SearchIntent.EXPLANATION,
        _SOFT,
        requires=["breaking"],
        levels=["major"],
        priority=88,
    ),
    _t(
        "software.migration",
        "{e} migration guide",
        SearchIntent.TUTORIAL,
        _SOFT,
        requires=["breaking"],
        levels=["major"],
        priority=86,
    ),
    _t(
        "software.errors",
        "how to fix {e} errors",
        SearchIntent.TROUBLESHOOTING,
        _SOFT,
        requires=["bugs"],
        levels=["major"],
        priority=70,
    ),
    _t(
        "software.tutorial",
        "{e} tutorial",
        SearchIntent.TUTORIAL,
        _SOFT,
        levels=["major", None],
        priority=75,
    ),
    # --- hardware
    _t("hardware.review", "{e} review", SearchIntent.REVIEW, _HW, priority=90),
    _t("hardware.benchmark", "{e} benchmark", SearchIntent.COMPARISON, _HW, priority=85),
    _t("hardware.unboxing", "{e} unboxing", SearchIntent.DEMONSTRATION, _HW, priority=60),
    _t("hardware.worth_it", "is {e} worth it", SearchIntent.REVIEW, _HW, priority=70),
    _t(
        "hardware.price",
        "{e} price",
        SearchIntent.PRODUCT_DISCOVERY,
        _HW,
        requires=["pricing"],
        priority=65,
    ),
    # --- games (new titles)
    _t("game.gameplay", "{e} gameplay", SearchIntent.DEMONSTRATION, [EntityType.GAME], priority=85),
    _t(
        "game.beginner",
        "{e} beginner guide",
        SearchIntent.GAME_GUIDE,
        [EntityType.GAME],
        priority=80,
    ),
    _t("game.how_to_play", "how to play {e}", SearchIntent.HOW_TO, [EntityType.GAME], priority=75),
    _t(
        "game.settings",
        "best settings for {e}",
        SearchIntent.GAME_GUIDE,
        [EntityType.GAME],
        requires=["performance"],
        priority=70,
    ),
    _t("game.review", "{e} review", SearchIntent.REVIEW, [EntityType.GAME], priority=60),
    # --- game events (limited-time)
    _t(
        "event.guide",
        "{game} {e} guide",
        SearchIntent.GAME_GUIDE,
        [EntityType.GAME_EVENT],
        game=True,
        priority=95,
    ),
    _t(
        "event.complete",
        "how to complete {e}",
        SearchIntent.CHALLENGE_GUIDE,
        [EntityType.GAME_EVENT],
        requires=["challenges"],
        priority=92,
    ),
    _t(
        "event.challenges",
        "{e} challenges",
        SearchIntent.CHALLENGE_GUIDE,
        [EntityType.GAME_EVENT],
        requires=["challenges"],
        priority=88,
    ),
    _t(
        "event.rewards",
        "{e} rewards",
        SearchIntent.GAME_GUIDE,
        [EntityType.GAME_EVENT],
        requires=["rewards"],
        priority=85,
    ),
    _t(
        "event.strategy",
        "{e} best strategy",
        SearchIntent.GAME_GUIDE,
        [EntityType.GAME_EVENT],
        priority=75,
    ),
    _t(
        "event.end",
        "when does {e} end",
        SearchIntent.NEWS,
        [EntityType.GAME_EVENT],
        requires=["limited_time"],
        priority=70,
    ),
    # --- game items (only verified, explicitly named items)
    _t(
        "item.unlock",
        "how to unlock {e}",
        SearchIntent.ITEM_LOCATION,
        [EntityType.GAME_ITEM],
        priority=95,
    ),
    _t(
        "item.where",
        "where to find {e} in {game}",
        SearchIntent.ITEM_LOCATION,
        [EntityType.GAME_ITEM],
        game=True,
        priority=93,
    ),
    _t(
        "item.location",
        "{e} location",
        SearchIntent.ITEM_LOCATION,
        [EntityType.GAME_ITEM],
        priority=85,
    ),
    # --- game updates / seasons
    _t(
        "update.changes",
        "{e} patch notes",
        SearchIntent.NEWS,
        [EntityType.GAME_UPDATE],
        priority=85,
    ),
    _t(
        "update.whats_new",
        "{e} what's new",
        SearchIntent.EXPLANATION,
        [EntityType.GAME_UPDATE],
        priority=75,
    ),
    _t(
        "update.bugs",
        "{e} bugs",
        SearchIntent.TROUBLESHOOTING,
        [EntityType.GAME_UPDATE],
        requires=["bugs"],
        priority=70,
    ),
    _t(
        "update.crash",
        "how to fix {game} crashing",
        SearchIntent.TROUBLESHOOTING,
        [EntityType.GAME_UPDATE],
        requires=["bugs"],
        game=True,
        priority=65,
    ),
    # --- technologies / features
    _t(
        "tech.what_is",
        "what is {e}",
        SearchIntent.EXPLANATION,
        [EntityType.TECHNOLOGY, EntityType.FEATURE],
        priority=80,
    ),
    _t(
        "tech.tutorial",
        "{e} tutorial",
        SearchIntent.TUTORIAL,
        [EntityType.TECHNOLOGY, EntityType.FEATURE],
        priority=75,
    ),
)

# Semantic-versioned code releases: release level gates expansion. AI model names
# ("Gemini 2.5", "Nimbus Coder 1.5") are not semver, so they are not gated this way.
CODE_RELEASE_TYPES = frozenset({EntityType.SOFTWARE, EntityType.FRAMEWORK, EntityType.RELEASE})

COMPARISON_TYPES = frozenset(
    {
        EntityType.AI_MODEL,
        EntityType.AI_PRODUCT,
        EntityType.SOFTWARE,
        EntityType.FRAMEWORK,
        EntityType.HARDWARE,
        EntityType.GAME,
    }
)


@dataclass(frozen=True)
class EntityView:
    """What expansion needs to know about an entity (from extraction and/or the DB)."""

    key: str
    canonical_name: str
    entity_type: EntityType
    category: Category
    status: EntityStatus
    confidence: float
    attributes: frozenset[str] = frozenset()
    release_level: str | None = None
    parent_game: str | None = None
    competitors: tuple[str, ...] = ()  # grounded comparison targets
    evergreen: bool = False
    evidence_refs: tuple[str, ...] = ()
    superseded_by: str | None = None  # newer same-or-higher-level release of the same family


@dataclass(frozen=True)
class KeywordCandidate:
    query: str
    canonical: str
    origin: KeywordOrigin
    method: DiscoveryMethod
    category: Category
    entity_key: str | None
    entity_name: str | None
    template_id: str | None
    intent_hint: SearchIntent | None
    grounding: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass
class ExpansionReport:
    candidates: list[KeywordCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (template id, reason)


def make_candidate(
    query: str,
    *,
    origin: KeywordOrigin,
    method: DiscoveryMethod,
    category: Category,
    entity: EntityView | None = None,
    template_id: str | None = None,
    intent_hint: SearchIntent | None = None,
    grounding: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
) -> KeywordCandidate | None:
    text = clean_original_query(query)
    try:
        canonical = canonicalize_query(text)
    except QueryValidationError:
        return None
    return KeywordCandidate(
        query=text,
        canonical=canonical,
        origin=origin,
        method=method,
        category=category,
        entity_key=entity.key if entity else None,
        entity_name=entity.canonical_name if entity else None,
        template_id=template_id,
        intent_hint=intent_hint,
        grounding=tuple(grounding),
        evidence_refs=tuple(evidence_refs) or (entity.evidence_refs if entity else ()),
    )


def expand_entity(entity: EntityView, config: ExpansionConfig) -> ExpansionReport:
    """Generate grounded hypotheses for one entity, respecting per-entity limits."""
    report = ExpansionReport()
    if config.max_depth < 1:
        report.skipped.append(("*", "expansion disabled (max_depth=0)"))
        return report
    if entity.status is not EntityStatus.VERIFIED:
        report.skipped.append(("*", f"entity status is {entity.status.value}; not expanded"))
        return report
    if entity.confidence < config.min_entity_confidence:
        report.skipped.append(("*", f"confidence {entity.confidence} below threshold"))
        return report
    if entity.evergreen:
        report.skipped.append(
            ("*", "evergreen curated entity; only its new releases/events expand")
        )
        return report
    code_release = entity.entity_type in CODE_RELEASE_TYPES
    if entity.release_level == "prerelease" or (code_release and entity.release_level == "patch"):
        report.skipped.append(("*", f"{entity.release_level} release; recorded as evidence only"))
        return report
    if code_release and entity.superseded_by:
        # Libraries/SDKs ship minor releases every few days; only the newest is a topic.
        report.skipped.append(
            ("*", f"superseded by {entity.superseded_by}; recorded as evidence only")
        )
        return report

    # Every hypothesis records why it exists: the verified entity plus what the template needed.
    basis = f"entity:{entity.entity_type.value}:{entity.canonical_name}"
    templates = sorted(
        (t for t in TEMPLATES if entity.entity_type in t.entity_types),
        key=lambda t: (-t.priority, t.id),
    )
    for template in templates:
        if len(report.candidates) >= config.max_templates_per_entity:
            report.skipped.append((template.id, "per-entity template limit reached"))
            continue
        missing = template.requires - entity.attributes
        if missing:
            report.skipped.append(
                (template.id, f"not grounded: needs {', '.join(sorted(missing))}")
            )
            continue
        if (
            template.release_levels is not None
            and entity.release_level not in template.release_levels
        ):
            report.skipped.append(
                (template.id, f"release level {entity.release_level} not applicable")
            )
            continue
        if template.needs_parent_game and not entity.parent_game:
            report.skipped.append((template.id, "parent game unknown"))
            continue
        phrase = template.pattern.format(e=entity.canonical_name, game=entity.parent_game or "")
        if len(phrase.split()) > config.max_query_words:
            report.skipped.append((template.id, "phrase too long"))
            continue
        grounding = [basis, *(f"attribute:{a}" for a in sorted(template.requires))]
        if template.release_levels is not None:
            grounding.append(f"release_level:{entity.release_level or 'unversioned'}")
        if template.needs_parent_game:
            grounding.append(f"parent_game:{entity.parent_game}")
        candidate = make_candidate(
            phrase,
            origin=KeywordOrigin.GENERATED_HYPOTHESIS,
            method=DiscoveryMethod.ENTITY_TEMPLATE,
            category=entity.category,
            entity=entity,
            template_id=template.id,
            intent_hint=template.intent,
            grounding=grounding,
        )
        if candidate is not None:
            report.candidates.append(candidate)

    if code_release and entity.release_level == "minor":
        # Libraries ship minor releases constantly; "X 1.5 vs X 1.4" is noise.
        report.skipped.append(("comparison.vs", "minor code release; no comparisons"))
    elif config.max_depth >= 2 and entity.entity_type in COMPARISON_TYPES:
        for competitor in entity.competitors[: config.max_comparisons_per_entity]:
            candidate = make_candidate(
                f"{entity.canonical_name} vs {competitor}",
                origin=KeywordOrigin.GENERATED_HYPOTHESIS,
                method=DiscoveryMethod.ENTITY_COMPARISON,
                category=entity.category,
                entity=entity,
                template_id="comparison.vs",
                intent_hint=SearchIntent.COMPARISON,
                grounding=(basis, f"competitor:{competitor}"),
            )
            if candidate is not None:
                report.candidates.append(candidate)
        for competitor in entity.competitors[config.max_comparisons_per_entity :]:
            report.skipped.append(("comparison.vs", f"comparison limit reached ({competitor})"))
    return report


def cap_candidates(
    candidates: list[KeywordCandidate], limit: int
) -> tuple[list[KeywordCandidate], int]:
    """Enforce the per-run keyword limit, keeping observed queries first."""
    ordered = sorted(
        candidates,
        key=lambda c: (c.origin is not KeywordOrigin.OBSERVED_QUERY, c.method.value, c.canonical),
    )
    return ordered[:limit], max(0, len(ordered) - limit)
