"""Hierarchical search-intent classification (deterministic baseline).

Rules match cue phrases in the query. Primary intents get a rule score; category-specific
sub-intents add to the primary they belong to. The output keeps every plausible intent,
the resolved entities, query specificity, ambiguity indicators and the matched cues.

``ConfidenceLabel`` reflects rule agreement only; it has not been calibrated against
outcomes and is not a probability. An optional LLM may *suggest* intents
(``merge_llm_suggestions``); it can add secondary intents and choose between
already-plausible primaries when the rules are uncertain, never override them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from trend_agent.content_analysis.config import ContentAnalysisConfig
from trend_agent.content_analysis.entity_verification import QueryEntities
from trend_agent.schemas.search_intent import (
    SUB_INTENT_CATEGORY,
    ClassificationMethod,
    ConfidenceLabel,
    IntentClassification,
    IntentEvidence,
    PrimaryIntent,
    Specificity,
    SubIntent,
)
from trend_agent.verification.text import fold, tokens

ALGORITHM_VERSION = "intent-rules-1.0"
P = PrimaryIntent
S = SubIntent


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    weight: float
    primary: PrimaryIntent | None = None
    sub: SubIntent | None = None
    categories: frozenset[str] | None = None


def _r(
    name: str,
    regex: str,
    weight: float = 1.0,
    *,
    primary: PrimaryIntent | None = None,
    sub: SubIntent | None = None,
    categories: Iterable[str] | None = None,
) -> Rule:
    return Rule(
        name,
        re.compile(regex, re.IGNORECASE),
        weight,
        primary,
        sub,
        frozenset(categories) if categories is not None else None,
    )


AI = ("ai",)
TECH = ("technology",)
GAME = ("gaming",)
NOT_GAME = ("ai", "technology")

SUB_PRIMARY: dict[SubIntent, PrimaryIntent] = {
    S.MODEL_INSTALLATION: P.TUTORIAL,
    S.AI_AGENT_TUTORIAL: P.TUTORIAL,
    S.MODEL_COMPARISON: P.COMPARISON,
    S.BENCHMARK: P.COMPARISON,
    S.API_INTEGRATION: P.TUTORIAL,
    S.AI_AUTOMATION: P.TUTORIAL,
    S.LOCAL_MODEL_SETUP: P.TUTORIAL,
    S.TOOL_DEMONSTRATION: P.DEMONSTRATION,
    S.MODEL_PRICING: P.INFORMATIONAL,
    S.RELEASE_EXPLANATION: P.NEWS,
    S.SOFTWARE_INSTALLATION: P.TUTORIAL,
    S.PRODUCT_REVIEW: P.REVIEW,
    S.HARDWARE_BENCHMARK: P.REVIEW,
    S.TROUBLESHOOTING: P.PROBLEM_SOLVING,
    S.PROGRAMMING_TUTORIAL: P.TUTORIAL,
    S.VERSION_MIGRATION: P.TUTORIAL,
    S.SECURITY_EXPLANATION: P.INFORMATIONAL,
    S.PRODUCT_COMPARISON: P.COMPARISON,
    S.FEATURE_DEMONSTRATION: P.DEMONSTRATION,
    S.ITEM_LOCATION: P.GAMING_GUIDE,
    S.CHALLENGE_COMPLETION: P.GAMING_GUIDE,
    S.EVENT_GUIDE: P.GAMING_GUIDE,
    S.WEAPON_GUIDE: P.GAMING_GUIDE,
    S.GAME_UPDATE: P.NEWS,
    S.GAME_SETTINGS: P.GAMING_GUIDE,
    S.GAME_TROUBLESHOOTING: P.PROBLEM_SOLVING,
    S.STRATEGY_GUIDE: P.GAMING_GUIDE,
    S.GAMEPLAY_DEMONSTRATION: P.DEMONSTRATION,
    S.REWARD_UNLOCK: P.GAMING_GUIDE,
}

RULES: tuple[Rule, ...] = (
    # ---- primaries (any category)
    _r("how_to", r"\bhow (?:to|do i|do you|can i)\b", 1.0, primary=P.TUTORIAL),
    _r(
        "tutorial",
        r"\b(?:tutorial|step by step|for beginners|walkthrough|course|guide)\b",
        1.2,
        primary=P.TUTORIAL,
        categories=NOT_GAME,
    ),
    _r(
        "setup",
        r"\b(?:install\w*|set ?up|setup|configure|deploy|get started|getting started)\b",
        1.2,
        primary=P.TUTORIAL,
    ),
    _r(
        "fix",
        r"\b(?:fix|fixed|error|not working|doesn'?t work|won'?t|can'?t|crash\w*|issue|"
        r"problem|troubleshoot\w*|stuck|failed|broken)\b",
        1.6,
        primary=P.PROBLEM_SOLVING,
    ),
    _r(
        "nav",
        r"\b(?:official|channel|website|docs|documentation|download page|login|"
        r"trailer)\b",
        0.8,
        primary=P.NAVIGATIONAL,
    ),
    _r(
        "versus",
        r"(?:\bvs\.?\b|\bversus\b|\bcompar\w*|\bbetter than\b|\balternatives?\b|"
        r"\bwhich is better\b)",
        2.0,
        primary=P.COMPARISON,
    ),
    _r(
        "review",
        r"\b(?:review|worth it|hands[- ]on|impressions|is it good|pros and cons|"
        r"tested)\b",
        1.6,
        primary=P.REVIEW,
    ),
    _r(
        "demo",
        r"\b(?:demo|showcase|in action|first look|unboxing|test drive|examples?)\b",
        1.4,
        primary=P.DEMONSTRATION,
    ),
    _r(
        "news",
        r"\b(?:news|announced|announcement|release date|leaks?|leaked|launch(?:ed)?|"
        r"patch notes|changelog|what'?s new|new features|when (?:is|does|will))\b",
        1.3,
        primary=P.NEWS,
    ),
    _r(
        "explain",
        r"\b(?:what (?:is|are|does)|explained|explain\w*|meaning|why|how does .* "
        r"work|overview|everything you need to know)\b",
        1.2,
        primary=P.INFORMATIONAL,
    ),
    _r(
        "run_use",
        r"\b(?:run|use|using|build|create|connect)\b.{0,30}\b(?:locally|local|with|on|in)\b",
        1.0,
        primary=P.TUTORIAL,
    ),
    _r(
        "price",
        r"\b(?:price|pricing|cost|how much|free tier|subscription)\b",
        1.0,
        primary=P.INFORMATIONAL,
    ),
    _r(
        "fun",
        r"\b(?:funny|memes?|best moments|highlights|montage|reaction|compilation|fails)\b",
        1.5,
        primary=P.ENTERTAINMENT,
    ),
    _r(
        "game_guide",
        r"\b(?:where to find|locations?|how to get|unlock\w*|quests?|boss|"
        r"tier list|loadout|tips|strategy|guide)\b",
        1.2,
        primary=P.GAMING_GUIDE,
        categories=GAME,
    ),
    # ---- AI sub-intents
    _r(
        "ai_install",
        r"\b(?:install\w*|set ?up|setup|download)\b",
        1.0,
        sub=S.MODEL_INSTALLATION,
        categories=AI,
    ),
    _r(
        "ai_local",
        r"\b(?:locally|local|offline|on (?:my )?(?:pc|mac|laptop)|self[- ]host\w*|"
        r"ollama)\b",
        1.6,
        sub=S.LOCAL_MODEL_SETUP,
        categories=AI,
    ),
    _r(
        "ai_agent",
        r"\bagents?\b.*\b(?:tutorial|build|create|make|how to|guide|setup|set up)\b|"
        r"\b(?:tutorial|build|create|make|how to)\b.*\bagents?\b",
        1.6,
        sub=S.AI_AGENT_TUTORIAL,
        categories=AI,
    ),
    _r(
        "ai_compare",
        r"(?:\bvs\.?\b|\bversus\b|\bcompar\w*|\bbetter than\b)",
        1.0,
        sub=S.MODEL_COMPARISON,
        categories=AI,
    ),
    _r("ai_bench", r"\b(?:benchmarks?|leaderboard|evals?)\b", 1.6, sub=S.BENCHMARK, categories=AI),
    _r(
        "ai_api",
        r"\b(?:api|sdk|integrat\w*|endpoint|python client)\b",
        1.6,
        sub=S.API_INTEGRATION,
        categories=AI,
    ),
    _r(
        "ai_auto",
        r"\b(?:automat\w*|workflows?|pipelines?|no[- ]code)\b",
        1.5,
        sub=S.AI_AUTOMATION,
        categories=AI,
    ),
    _r(
        "ai_demo",
        r"\b(?:demo|showcase|in action|first look|testing|tested)\b",
        1.0,
        sub=S.TOOL_DEMONSTRATION,
        categories=AI,
    ),
    _r(
        "ai_price",
        r"\b(?:price|pricing|cost|free tier|subscription|plans?)\b",
        1.8,
        sub=S.MODEL_PRICING,
        categories=AI,
    ),
    _r(
        "ai_release",
        r"\b(?:what'?s new|explained|announced|release|launch\w*|new features|"
        r"changelog|news)\b",
        1.0,
        sub=S.RELEASE_EXPLANATION,
        categories=AI,
    ),
    # ---- technology sub-intents
    _r(
        "tech_install",
        r"\b(?:install\w*|set ?up|setup|download|configure)\b",
        1.0,
        sub=S.SOFTWARE_INSTALLATION,
        categories=TECH,
    ),
    _r(
        "tech_review",
        r"\b(?:review|worth it|hands[- ]on|impressions)\b",
        1.0,
        sub=S.PRODUCT_REVIEW,
        categories=TECH,
    ),
    _r(
        "tech_bench",
        r"\b(?:benchmarks?|fps test|performance test|thermals|gaming performance)\b",
        1.6,
        sub=S.HARDWARE_BENCHMARK,
        categories=TECH,
    ),
    _r(
        "tech_fix",
        r"\b(?:fix|error|not working|crash\w*|issue|troubleshoot\w*|broken)\b",
        1.0,
        sub=S.TROUBLESHOOTING,
        categories=TECH,
    ),
    _r(
        "tech_code",
        r"\b(?:python|javascript|typescript|rust|golang|java|react|vue|django|"
        r"coding|programming|code)\b",
        1.0,
        sub=S.PROGRAMMING_TUTORIAL,
        categories=TECH,
    ),
    _r(
        "tech_migrate",
        r"\b(?:migrat\w*|upgrad\w* (?:to|from)|upgrade guide|breaking changes|"
        r"from v?\d+ to v?\d+)\b",
        2.0,
        sub=S.VERSION_MIGRATION,
        categories=TECH,
    ),
    _r(
        "tech_security",
        r"\b(?:vulnerabilit\w*|cve|security|breach|malware|zero[- ]day)\b",
        1.6,
        sub=S.SECURITY_EXPLANATION,
        categories=TECH,
    ),
    _r(
        "tech_compare",
        r"(?:\bvs\.?\b|\bversus\b|\bcompar\w*)",
        1.0,
        sub=S.PRODUCT_COMPARISON,
        categories=TECH,
    ),
    _r(
        "tech_feature",
        r"\b(?:new features|demo|showcase|hands[- ]on)\b",
        1.0,
        sub=S.FEATURE_DEMONSTRATION,
        categories=TECH,
    ),
    # ---- gaming sub-intents
    _r(
        "g_location",
        r"\b(?:where (?:to|is|are|can)|locations?|find|spawns?|map)\b",
        1.8,
        sub=S.ITEM_LOCATION,
        categories=GAME,
    ),
    _r(
        "g_challenge",
        r"\b(?:challenges?|quests?|missions?|complete)\b",
        1.6,
        sub=S.CHALLENGE_COMPLETION,
        categories=GAME,
    ),
    _r(
        "g_event",
        r"\b(?:event|live event|limited[- ]time|festival)\b",
        1.3,
        sub=S.EVENT_GUIDE,
        categories=GAME,
    ),
    _r(
        "g_weapon",
        r"\b(?:weapons?|guns?|loadout|swords?|rifles?|blade)\b",
        1.0,
        sub=S.WEAPON_GUIDE,
        categories=GAME,
    ),
    _r(
        "g_update",
        r"\b(?:update|patch|patch notes|new season|season \d+ (?:changes|update)|"
        r"chapter \d+ (?:changes|update))\b",
        1.2,
        sub=S.GAME_UPDATE,
        categories=GAME,
    ),
    _r(
        "g_settings",
        r"\b(?:settings|sensitivity|keybinds|graphics|fps boost)\b",
        1.8,
        sub=S.GAME_SETTINGS,
        categories=GAME,
    ),
    _r(
        "g_fix",
        r"\b(?:fix|crash\w*|error|not working|lag\w*|stutter\w*|won'?t launch)\b",
        1.6,
        sub=S.GAME_TROUBLESHOOTING,
        categories=GAME,
    ),
    _r(
        "g_strategy",
        r"\b(?:strategy|strategies|tips|how to win|tier list|meta)\b",
        1.4,
        sub=S.STRATEGY_GUIDE,
        categories=GAME,
    ),
    _r(
        "g_gameplay",
        r"\b(?:gameplay|playthrough|first look|lets play)\b",
        1.4,
        sub=S.GAMEPLAY_DEMONSTRATION,
        categories=GAME,
    ),
    _r(
        "g_reward",
        r"\b(?:unlock\w*|rewards?|how to get|earn|free (?:skin|item|emote))\b",
        1.3,
        sub=S.REWARD_UNLOCK,
        categories=GAME,
    ),
)

_PRECEDENCE = [
    P.PROBLEM_SOLVING,
    P.GAMING_GUIDE,
    P.COMPARISON,
    P.TUTORIAL,
    P.REVIEW,
    P.DEMONSTRATION,
    P.NEWS,
    P.INFORMATIONAL,
    P.NAVIGATIONAL,
    P.ENTERTAINMENT,
]
_TIME_WORDS = re.compile(r"\b(?:new|latest|today|this week|202\d|season \d+|chapter \d+|update)\b")
_ENTITY_CATEGORY = {
    "ai_model": "ai",
    "ai_product": "ai",
    "game": "gaming",
    "game_event": "gaming",
    "game_item": "gaming",
    "game_update": "gaming",
    "hardware": "technology",
    "software": "technology",
    "framework": "technology",
    "technology": "technology",
}


def infer_category(entities: QueryEntities, given: str | None) -> str | None:
    if given in ("ai", "technology", "gaming"):
        return given
    for f in entities.facts:
        cat = _ENTITY_CATEGORY.get(f.entity_type or "")
        if cat:
            return cat
    return given


def policy_flags(query: str, patterns: Sequence[str]) -> tuple[str, ...]:
    q = fold(query)
    return tuple("cheat_or_exploit" for p in patterns if re.search(p, q, re.IGNORECASE))[:1]


def classify(
    query: str,
    entities: QueryEntities,
    config: ContentAnalysisConfig,
    *,
    category: str | None = None,
) -> IntentClassification:
    q = fold(query)
    cat = infer_category(entities, category)
    primary_scores: dict[PrimaryIntent, float] = {}
    sub_scores: dict[SubIntent, float] = {}
    evidence: list[IntentEvidence] = []
    for rule in RULES:
        if rule.categories is not None and cat not in rule.categories:
            continue
        m = rule.pattern.search(q)
        if not m:
            continue
        if rule.primary is not None:
            primary_scores[rule.primary] = primary_scores.get(rule.primary, 0.0) + rule.weight
            evidence.append(
                IntentEvidence(
                    rule=rule.name,
                    intent=rule.primary.value,
                    matched=m.group(0),
                    weight=rule.weight,
                )
            )
        if rule.sub is not None:
            sub_scores[rule.sub] = sub_scores.get(rule.sub, 0.0) + rule.weight
            mapped = SUB_PRIMARY[rule.sub]
            primary_scores[mapped] = primary_scores.get(mapped, 0.0) + rule.weight * 0.5
            evidence.append(
                IntentEvidence(
                    rule=rule.name, intent=rule.sub.value, matched=m.group(0), weight=rule.weight
                )
            )
    ambiguity: list[str] = []
    if not primary_scores:
        primary = P.INFORMATIONAL
        ambiguity.append("no intent cue in the query; defaulted to informational")
        top = 0.0
    else:
        top = max(primary_scores.values())
        best = [p for p, s in primary_scores.items() if s == top]
        primary = min(best, key=_PRECEDENCE.index)
    secondary = tuple(
        p
        for p, s in sorted(
            primary_scores.items(), key=lambda kv: (-kv[1], _PRECEDENCE.index(kv[0]))
        )
        if p is not primary and s >= 0.5 * top
    )
    sub = _best_sub(sub_scores, primary)
    secondary_subs = tuple(
        s for s, v in sorted(sub_scores.items(), key=lambda kv: -kv[1]) if s is not sub and v >= 1.0
    )
    for ref in entities.refs:
        if ref.state == "ambiguous":
            ambiguity.append(f"'{ref.name}' matches several entities: {', '.join(ref.candidates)}")
        elif ref.state == "unknown":
            ambiguity.append(f"'{ref.name}' is not a known or evidenced entity")
        elif ref.state in ("rumored", "rejected"):
            ambiguity.append(f"'{ref.name}' is {ref.state}, not confirmed")
    if len(tokens(query)) <= 2 and not entities.facts:
        ambiguity.append("very short query")
    close = [p for p in secondary if primary_scores.get(p, 0) >= 0.85 * top]
    if close:
        ambiguity.append(
            "several intents are almost equally plausible: "
            + ", ".join(p.value for p in [primary, *close])
        )
    margin = top - max((primary_scores[p] for p in secondary), default=0.0)
    resolved = [r for r in entities.refs if r.state in ("confirmed", "reported")]
    if top >= 1.5 and margin >= 0.5 * top and not ambiguity:
        confidence = ConfidenceLabel.HIGH
    elif top >= 1.0 and not any("matches several" in a for a in ambiguity):
        confidence = ConfidenceLabel.MEDIUM
    else:
        confidence = ConfidenceLabel.LOW
    task = sub is not None or primary not in (P.INFORMATIONAL, P.NAVIGATIONAL)
    if resolved and task:
        specificity = Specificity.SPECIFIC
    elif resolved or (task and primary_scores):
        specificity = Specificity.MODERATE
    else:
        specificity = Specificity.BROAD
    fcfg = config.freshness
    time_sensitive = (
        primary in fcfg.time_sensitive_intents
        or (sub is not None and sub in fcfg.time_sensitive_sub_intents)
        or bool(_TIME_WORDS.search(q))
    ) and not (sub is not None and sub in fcfg.evergreen_sub_intents)
    return IntentClassification(
        query=query,
        category=cat,
        primary_intent=primary,
        sub_intent=sub,
        secondary_intents=secondary,
        secondary_sub_intents=secondary_subs,
        entities=tuple(entities.refs),
        specificity=specificity,
        ambiguity=tuple(dict.fromkeys(ambiguity)),
        confidence=confidence,
        supporting_evidence=tuple(evidence),
        method=ClassificationMethod.DETERMINISTIC_RULES,
        time_sensitive=time_sensitive,
        policy_flags=policy_flags(query, config.policy.prohibited_patterns),
        algorithm_version=ALGORITHM_VERSION,
    )


def _best_sub(scores: dict[SubIntent, float], primary: PrimaryIntent) -> SubIntent | None:
    if not scores:
        return None
    aligned = {s: v for s, v in scores.items() if SUB_PRIMARY[s] is primary}
    pool = aligned or scores
    top = max(pool.values())
    order = list(SubIntent)
    return min((s for s, v in pool.items() if v == top), key=order.index)


def merge_llm_suggestions(
    base: IntentClassification,
    suggestions: Sequence[tuple[PrimaryIntent, SubIntent | None]],
) -> IntentClassification:
    """Validated LLM intent suggestions: secondary intents only, or a choice between
    rule-plausible primaries when the rules gave LOW confidence."""
    if not suggestions:
        return base
    plausible = {e.intent for e in base.supporting_evidence}
    secondary = list(base.secondary_intents)
    primary, sub = base.primary_intent, base.sub_intent
    for p, s in suggestions:
        if s is not None and SUB_INTENT_CATEGORY.get(s) not in (base.category, None):
            s = None  # a sub-intent of another category is not accepted
        if base.confidence is ConfidenceLabel.LOW and p.value in plausible and p is not primary:
            secondary.insert(0, primary)
            primary, sub = p, s if s is not None and s.value in plausible else None
        elif p is not primary and p not in secondary:
            secondary.append(p)
    return base.model_copy(
        update={
            "primary_intent": primary,
            "sub_intent": sub,
            "secondary_intents": tuple(dict.fromkeys(x for x in secondary if x is not primary)),
            "method": ClassificationMethod.LLM_ASSISTED,
        }
    )
