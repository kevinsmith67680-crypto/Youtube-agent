"""Phase 5 search-intent taxonomy and information requirements.

Confidence values here are *labels* derived from rule agreement (HIGH / MEDIUM / LOW),
not calibrated probabilities. Information requirements are *candidate* needs implied by
the query; product-specific details in them are never facts until verified.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PrimaryIntent(StrEnum):
    INFORMATIONAL = "informational"
    TUTORIAL = "tutorial"
    PROBLEM_SOLVING = "problem_solving"
    NAVIGATIONAL = "navigational"
    COMPARISON = "comparison"
    REVIEW = "review"
    DEMONSTRATION = "demonstration"
    NEWS = "news"
    ENTERTAINMENT = "entertainment"
    GAMING_GUIDE = "gaming_guide"


class SubIntent(StrEnum):
    # AI
    MODEL_INSTALLATION = "model_installation"
    AI_AGENT_TUTORIAL = "ai_agent_tutorial"
    MODEL_COMPARISON = "model_comparison"
    BENCHMARK = "benchmark"
    API_INTEGRATION = "api_integration"
    AI_AUTOMATION = "ai_automation"
    LOCAL_MODEL_SETUP = "local_model_setup"
    TOOL_DEMONSTRATION = "tool_demonstration"
    MODEL_PRICING = "model_pricing"
    RELEASE_EXPLANATION = "release_explanation"
    # Technology
    SOFTWARE_INSTALLATION = "software_installation"
    PRODUCT_REVIEW = "product_review"
    HARDWARE_BENCHMARK = "hardware_benchmark"
    TROUBLESHOOTING = "troubleshooting"
    PROGRAMMING_TUTORIAL = "programming_tutorial"
    VERSION_MIGRATION = "version_migration"
    SECURITY_EXPLANATION = "security_explanation"
    PRODUCT_COMPARISON = "product_comparison"
    FEATURE_DEMONSTRATION = "feature_demonstration"
    # Gaming
    ITEM_LOCATION = "item_location"
    CHALLENGE_COMPLETION = "challenge_completion"
    EVENT_GUIDE = "event_guide"
    WEAPON_GUIDE = "weapon_guide"
    GAME_UPDATE = "game_update"
    GAME_SETTINGS = "game_settings"
    GAME_TROUBLESHOOTING = "game_troubleshooting"
    STRATEGY_GUIDE = "strategy_guide"
    GAMEPLAY_DEMONSTRATION = "gameplay_demonstration"
    REWARD_UNLOCK = "reward_unlock"


_AI_SUBS = (
    SubIntent.MODEL_INSTALLATION,
    SubIntent.AI_AGENT_TUTORIAL,
    SubIntent.MODEL_COMPARISON,
    SubIntent.BENCHMARK,
    SubIntent.API_INTEGRATION,
    SubIntent.AI_AUTOMATION,
    SubIntent.LOCAL_MODEL_SETUP,
    SubIntent.TOOL_DEMONSTRATION,
    SubIntent.MODEL_PRICING,
    SubIntent.RELEASE_EXPLANATION,
)
_TECH_SUBS = (
    SubIntent.SOFTWARE_INSTALLATION,
    SubIntent.PRODUCT_REVIEW,
    SubIntent.HARDWARE_BENCHMARK,
    SubIntent.TROUBLESHOOTING,
    SubIntent.PROGRAMMING_TUTORIAL,
    SubIntent.VERSION_MIGRATION,
    SubIntent.SECURITY_EXPLANATION,
    SubIntent.PRODUCT_COMPARISON,
    SubIntent.FEATURE_DEMONSTRATION,
)
_GAME_SUBS = (
    SubIntent.ITEM_LOCATION,
    SubIntent.CHALLENGE_COMPLETION,
    SubIntent.EVENT_GUIDE,
    SubIntent.WEAPON_GUIDE,
    SubIntent.GAME_UPDATE,
    SubIntent.GAME_SETTINGS,
    SubIntent.GAME_TROUBLESHOOTING,
    SubIntent.STRATEGY_GUIDE,
    SubIntent.GAMEPLAY_DEMONSTRATION,
    SubIntent.REWARD_UNLOCK,
)
SUB_INTENT_CATEGORY: dict[SubIntent, str] = {
    **dict.fromkeys(_AI_SUBS, "ai"),
    **dict.fromkeys(_TECH_SUBS, "technology"),
    **dict.fromkeys(_GAME_SUBS, "gaming"),
}


class ConfidenceLabel(StrEnum):
    """Rule-agreement label; NOT a probability (no calibration has been performed)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Specificity(StrEnum):
    SPECIFIC = "specific"  # entity (and version when relevant) plus a concrete task
    MODERATE = "moderate"  # entity or task, not both
    BROAD = "broad"  # neither a resolved entity nor a concrete task


class ClassificationMethod(StrEnum):
    DETERMINISTIC_RULES = "deterministic_rules"
    LLM_ASSISTED = "llm_assisted"  # rules + validated LLM suggestions
    MANUAL = "manual"


class EntityRef(_Frozen):
    name: str
    entity_key: str | None = None
    entity_type: str | None = None
    version: str | None = None
    state: str = Field(
        description="confirmed | reported | rumored | unknown | ambiguous | rejected"
    )
    evidence_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


class IntentEvidence(_Frozen):
    """A rule or signal that supports an intent (what matched, where)."""

    rule: str
    intent: str
    matched: str
    weight: float


class IntentClassification(_Frozen):
    query: str
    category: str | None
    primary_intent: PrimaryIntent
    sub_intent: SubIntent | None
    secondary_intents: tuple[PrimaryIntent, ...] = ()
    secondary_sub_intents: tuple[SubIntent, ...] = ()
    entities: tuple[EntityRef, ...] = ()
    specificity: Specificity
    ambiguity: tuple[str, ...] = ()
    confidence: ConfidenceLabel
    supporting_evidence: tuple[IntentEvidence, ...] = ()
    method: ClassificationMethod
    time_sensitive: bool = False
    policy_flags: tuple[str, ...] = Field(
        default=(), description="e.g. 'cheat_or_exploit' - such queries are never opportunities"
    )
    algorithm_version: str


class RequirementPriority(StrEnum):
    ESSENTIAL = "essential"
    IMPORTANT = "important"
    OPTIONAL = "optional"


class InformationRequirement(_Frozen):
    """A candidate informational need implied by the query (not a product fact)."""

    key: str = Field(description="stable identifier within the template, e.g. 'install_steps'")
    requirement: str
    priority: RequirementPriority
    cues: tuple[str, ...] = Field(description="words whose presence in evidence suggests coverage")
    requires_verified_entity: bool = False
    blocking: bool = Field(
        default=False,
        description="the analysis cannot proceed without it (e.g. the item must exist)",
    )
    source: str = "template"  # template | llm_suggested
    template: str


class QueryDecomposition(_Frozen):
    query: str
    primary_intent: PrimaryIntent
    sub_intent: SubIntent | None
    requirements: tuple[InformationRequirement, ...]
    notes: tuple[str, ...] = ()
    algorithm_version: str
