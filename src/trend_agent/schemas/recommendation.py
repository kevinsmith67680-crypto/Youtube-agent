"""Phase 7 recommendation contract (``recommendation/1.0``) and its building blocks.

A recommendation is a *research and production suggestion* derived from verified Phase 4-6
evidence. Its numbers (demand classification, forecast probabilities, scores) are copied
from those phases or computed by deterministic application code; generated text never sets
them. Nothing here predicts views, rankings or the success of a video.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RECOMMENDATION_SCHEMA_VERSION = "recommendation/1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- enums
class RecommendationStatus(StrEnum):
    """Application-side verification outcome (an LLM never sets it)."""

    APPROVED = "APPROVED"
    APPROVED_WITH_UNCERTAINTY = "APPROVED_WITH_UNCERTAINTY"
    RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
    REJECTED = "REJECTED"


class StatementLabel(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"  # supported by cited, verified evidence
    PLANNED_DEMONSTRATION = "PLANNED_DEMONSTRATION"  # what the creator will show/do
    HYPOTHESIS_TO_TEST = "HYPOTHESIS_TO_TEST"  # a question the video investigates
    UNKNOWN = "UNKNOWN"  # not established by the evidence; research before filming


class TitleStrategy(StrEnum):
    DIRECT_ANSWER = "DIRECT_ANSWER"
    TUTORIAL = "TUTORIAL"
    COMPARISON = "COMPARISON"
    EXPERIMENT = "EXPERIMENT"
    CURIOSITY = "CURIOSITY"
    TIME_SENSITIVE_NEWS = "TIME_SENSITIVE_NEWS"
    PROBLEM_SOLVING = "PROBLEM_SOLVING"


class HookType(StrEnum):
    DEMONSTRATION_FIRST = "DEMONSTRATION_FIRST"
    PROBLEM_FIRST = "PROBLEM_FIRST"
    SURPRISING_RESULT = "SURPRISING_RESULT"
    CHALLENGE = "CHALLENGE"
    COMPARISON = "COMPARISON"
    DIRECT_TUTORIAL = "DIRECT_TUTORIAL"


class VideoFormat(StrEnum):
    # Artificial intelligence
    HANDS_ON_AI_EXPERIMENT = "HANDS_ON_AI_EXPERIMENT"
    AI_AGENT_TUTORIAL = "AI_AGENT_TUTORIAL"
    AI_PRODUCT_DEMONSTRATION = "AI_PRODUCT_DEMONSTRATION"
    AI_MODEL_COMPARISON = "AI_MODEL_COMPARISON"
    BENCHMARK_INVESTIGATION = "BENCHMARK_INVESTIGATION"
    AI_AUTOMATION_CHALLENGE = "AI_AUTOMATION_CHALLENGE"
    BUILD_AN_APP_TUTORIAL = "BUILD_AN_APP_TUTORIAL"
    AI_TOOL_REVIEW = "AI_TOOL_REVIEW"
    LOCAL_AI_INSTALLATION_GUIDE = "LOCAL_AI_INSTALLATION_GUIDE"
    AI_CAPABILITY_INVESTIGATION = "AI_CAPABILITY_INVESTIGATION"
    # Technology
    PRODUCT_COMPARISON = "PRODUCT_COMPARISON"
    HARDWARE_REVIEW = "HARDWARE_REVIEW"
    PROGRAMMING_TUTORIAL = "PROGRAMMING_TUTORIAL"
    TECHNICAL_EXPERIMENT = "TECHNICAL_EXPERIMENT"
    DEVELOPER_TOOL_WALKTHROUGH = "DEVELOPER_TOOL_WALKTHROUGH"
    TECHNOLOGY_EXPLANATION = "TECHNOLOGY_EXPLANATION"
    SOFTWARE_MIGRATION_GUIDE = "SOFTWARE_MIGRATION_GUIDE"
    CYBERSECURITY_DEMONSTRATION = "CYBERSECURITY_DEMONSTRATION"
    HARDWARE_BENCHMARK = "HARDWARE_BENCHMARK"
    PRACTICAL_BUILD_CHALLENGE = "PRACTICAL_BUILD_CHALLENGE"
    # Gaming
    LIMITED_TIME_EVENT_GUIDE = "LIMITED_TIME_EVENT_GUIDE"
    ITEM_LOCATION_WALKTHROUGH = "ITEM_LOCATION_WALKTHROUGH"
    NEW_SEASON_EXPLANATION = "NEW_SEASON_EXPLANATION"
    CHALLENGE_COMPLETION_GUIDE = "CHALLENGE_COMPLETION_GUIDE"
    NEW_GAME_REVIEW = "NEW_GAME_REVIEW"
    GAMEPLAY_STRATEGY = "GAMEPLAY_STRATEGY"
    PERFORMANCE_OPTIMIZATION_GUIDE = "PERFORMANCE_OPTIMIZATION_GUIDE"
    GAME_UPDATE_BREAKDOWN = "GAME_UPDATE_BREAKDOWN"
    GAMING_AI_EXPERIMENT = "GAMING_AI_EXPERIMENT"
    COMPETITIVE_CHALLENGE = "COMPETITIVE_CHALLENGE"


class Complexity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ProductionProfile(StrEnum):
    """Planning assumptions (configurable hours), not guaranteed production times."""

    QUICK_RESPONSE = "QUICK_RESPONSE"
    STANDARD_TUTORIAL = "STANDARD_TUTORIAL"
    DEEP_TECHNICAL_VIDEO = "DEEP_TECHNICAL_VIDEO"


class PublishingUrgency(StrEnum):
    PUBLISH_SOON = "PUBLISH_SOON"  # forecast window URGENT, or time-limited topic
    WITHIN_DAYS = "WITHIN_DAYS"  # TIMELY window
    FLEXIBLE = "FLEXIBLE"  # LONGER_WINDOW / evergreen intent
    LIKELY_TOO_LATE = "LIKELY_TOO_LATE"
    UNKNOWN = "UNKNOWN"  # no estimable window: nothing is implied


class ProductionStatus(StrEnum):
    IDEA = "IDEA"
    RESEARCHING = "RESEARCHING"
    SCRIPTING = "SCRIPTING"
    RECORDING = "RECORDING"
    EDITING = "EDITING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"
    ABANDONED = "ABANDONED"


class FeedbackType(StrEnum):
    USEFUL = "USEFUL"
    NOT_RELEVANT = "NOT_RELEVANT"
    TOO_COMPETITIVE = "TOO_COMPETITIVE"
    TOO_LATE = "TOO_LATE"
    UNVERIFIED = "UNVERIFIED"
    SAVED = "SAVED"
    DISMISSED = "DISMISSED"
    PUBLISHED = "PUBLISHED"
    # interaction tracking (no judgement implied)
    VIEWED = "VIEWED"
    SELECTED = "SELECTED"
    PRODUCED = "PRODUCED"
    ABANDONED = "ABANDONED"


# ----------------------------------------------------------------------- components
class EvidenceRef(_Frozen):
    evidence_id: str
    kind: str
    trust: str | None = None
    publisher: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    note: str = ""


class DemandSummary(_Frozen):
    """Phase 4 demand evidence as carried by the Phase 5 report (never recomputed)."""

    basis: str
    measurement_type: str | None = None
    classification: str | None = None
    alert_stage: str | None = None
    severity: float | None = None
    relative_change: float | None = None
    freshness: str | None = None
    detected_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    surge_event_id: int | None = None
    direct_youtube_search: bool
    indirect: bool = Field(description="external search, proxy activity or no demand evidence")
    statement: str
    limitations: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class WindowSummary(_Frozen):
    production_profile: str
    status: str
    earliest_publication: datetime | None = None
    probability_elevated_at_publication: float | None = None


class ForecastSummary(_Frozen):
    """Phase 6 output reduced to what the recommendation uses; absent = unavailable."""

    available: bool
    prediction_id: str | None = None
    status: str | None = None
    status_reasons: tuple[str, ...] = ()
    horizon_hours: int | None = None
    target_family: str | None = None
    is_proxy: bool = False
    breakout_probability: float | None = None
    breakout_calibration: str | None = None
    continuation_probability: float | None = None
    trajectory_value: float | None = None
    trajectory_lower: float | None = None
    trajectory_upper: float | None = None
    peak_status: str | None = None
    peak_earliest_hours: float | None = None
    peak_latest_hours: float | None = None
    windows: tuple[WindowSummary, ...] = ()
    model: str | None = None
    validated_model: bool | None = None
    issued_at: datetime | None = None
    synthetic: bool = False
    note: str = "Forecasts describe relative interest of the measured source; not views."


class TitleOption(_Frozen):
    strategy: TitleStrategy
    text: str
    rationale: str
    target_intent: str
    query_term_coverage: float = Field(ge=0, le=1)
    length: int
    accepted: bool = True
    reasons: tuple[str, ...] = ()


class Hook(_Frozen):
    hook_type: HookType
    text: str
    estimated_seconds: float
    accepted: bool = True
    verification_status: str = "not_factual"
    reasons: tuple[str, ...] = ()


class OutlineItem(_Frozen):
    label: StatementLabel
    text: str
    evidence_ids: tuple[str, ...] = ()
    note: str | None = None


class OutlineSection(_Frozen):
    heading: str
    purpose: str
    items: tuple[OutlineItem, ...]


class Outline(_Frozen):
    sections: tuple[OutlineSection, ...]
    required_evidence: tuple[str, ...] = Field(
        default=(), description="what the creator must verify or capture before publishing"
    )
    failure_points: tuple[str, ...] = ()
    conclusion_to_investigate: str
    removed_items: tuple[str, ...] = ()


class FormatChoice(_Frozen):
    video_format: VideoFormat
    label: str
    rationale: str
    serves_intent: bool
    preferred_by_creator: bool = False


class Feasibility(_Frozen):
    complexity: Complexity
    production_profile: ProductionProfile
    profile_hours: float = Field(description="configured planning assumption for the profile")
    estimated_hours_low: float
    estimated_hours_high: float
    breakdown_hours: dict[str, float]
    required_software: tuple[str, ...] = ()
    required_hardware: tuple[str, ...] = ()
    requires_verified_results: bool
    creator_has_requirements: bool | None = Field(
        default=None, description="None = unknown (not stated in the creator profile)"
    )
    fits_creator_capacity: bool | None = None
    demand_window: WindowSummary | None = None
    urgency: PublishingUrgency
    urgency_reasons: tuple[str, ...] = ()
    uncertain: bool
    notes: tuple[str, ...] = ()


class ScoreComponent(_Frozen):
    name: str
    value: float | None = Field(description="0-1; None = not available (never zero)")
    weight: float
    contribution: float | None
    available: bool
    raw: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    note: str


class Ranking(_Frozen):
    composite_score: float | None = Field(
        description="0-100 ranking index; None when ineligible. Not a probability."
    )
    eligible: bool
    eligibility_reasons: tuple[str, ...] = ()
    version: str
    components: tuple[ScoreComponent, ...]
    available_weight: float
    evidence_quality_factor: float
    rank: int | None = None
    explanation: str
    limitations: tuple[str, ...] = ()
    note: str = (
        "A transparent ranking index with experimental, unoptimised weights. It is not the "
        "probability that a video succeeds or goes viral."
    )


class VerificationCheck(_Frozen):
    check: str
    passed: bool
    detail: str = ""


class RecommendationVerification(_Frozen):
    status: RecommendationStatus
    synthetic: bool
    gate_state: str | None = None
    gate_publication: str | None = None
    gate_run_id: str | None = None
    trend_statement: str | None = None
    checks: tuple[VerificationCheck, ...] = ()
    removed: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    verified_at: datetime


class Suppression(_Frozen):
    suppressed: bool
    reason: str | None = None
    duplicate_of: str | None = None


# ----------------------------------------------------------------------- contract
class RecommendationOutput(_Frozen):
    schema_version: Literal["recommendation/1.0"] = "recommendation/1.0"
    recommendation_id: str
    opportunity_id: str
    concept_index: int
    category: str | None
    region: str | None
    language: str | None
    primary_query: str
    query_id: int | None
    topic_cluster_id: int | None
    target_intent: str
    target_sub_intent: str | None
    title: str
    alternative_titles: tuple[TitleOption, ...]
    hooks: tuple[Hook, ...]
    video_format: VideoFormat
    format_label: str
    format_rationale: str
    alternative_approaches: tuple[FormatChoice, ...] = ()
    target_audience: str
    core_viewer_problem: str
    premise: str
    why_it_may_satisfy_demand: str
    key_information_requirements: tuple[str, ...]
    key_questions: tuple[str, ...]
    outline: Outline
    evidence: tuple[EvidenceRef, ...]
    competing_videos: tuple[str, ...] = Field(
        default=(), description="YouTube video IDs inspected in Phase 5 (links only)"
    )
    uncertainties: tuple[str, ...]
    production: Feasibility
    ranking: Ranking
    verification: RecommendationVerification
    demand: DemandSummary
    forecast: ForecastSummary
    gap_type: str | None = None
    gap_strength: str | None = None
    research_hypothesis: bool = Field(
        default=False, description="a labelled research candidate, not a verified idea"
    )
    suppression: Suppression = Suppression(suppressed=False)
    creator_profile_id: str | None = None
    generator: str
    generator_model: str | None = None
    input_fingerprint: str
    generated_at: datetime
    versions: dict[str, str]
    synthetic: bool
    notice: str = (
        "A research and production suggestion grounded in stored evidence. It does not "
        "predict views, rankings or revenue; verify unknown items before filming."
    )


# ----------------------------------------------------------------- creator profile
class CreatorProfile(BaseModel):
    """Optional personalisation. Nothing about channel performance is inferred."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = "default"
    display_name: str = "Default creator"
    preferred_categories: list[str] = Field(
        default_factory=lambda: ["ai", "technology", "gaming"], max_length=8
    )
    channel_focus: str = Field(default="", max_length=200)
    primary_regions: list[str] = Field(default_factory=lambda: ["GB", "US"], max_length=10)
    language: str = Field(default="en", max_length=12)
    preferred_formats: list[VideoFormat] = Field(default_factory=list, max_length=30)
    production_capacity_videos_per_week: float | None = Field(default=None, ge=0, le=50)
    available_hours_per_week: float | None = Field(default=None, ge=0, le=168)
    skills: list[str] = Field(default_factory=list, max_length=30)
    equipment: list[str] = Field(default_factory=list, max_length=30)
    preferred_duration_minutes: int | None = Field(default=None, ge=1, le=240)
    channel_size: str | None = Field(default=None, max_length=40)
    content_exclusions: list[str] = Field(default_factory=list, max_length=30)
    topic_preferences: list[str] = Field(default_factory=list, max_length=30)
    production_style: str = Field(
        default="practical demonstrations, tutorials and experiments", max_length=200
    )
    recommendation_quantity: int = Field(default=20, ge=1, le=100)
    notification_preferences: dict[str, bool] = Field(
        default_factory=lambda: {"in_app": True, "email": False, "telegram": False}
    )
    llm_daily_token_budget: int | None = Field(default=None, ge=0, le=10_000_000)

    @field_validator("profile_id")
    @classmethod
    def _profile_id(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
            raise ValueError("profile_id: 1-64 letters, digits, '_' or '-'")
        return v

    @field_validator("preferred_categories")
    @classmethod
    def _categories(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in ("ai", "technology", "gaming")]
        if bad:
            raise ValueError(f"unsupported categories: {', '.join(bad)}")
        return list(dict.fromkeys(v))

    @field_validator("primary_regions")
    @classmethod
    def _regions(cls, v: list[str]) -> list[str]:
        out = [r.upper() for r in v]
        if any(len(r) != 2 or not r.isalpha() for r in out):
            raise ValueError("regions are two-letter country codes")
        return out

    @field_validator("notification_preferences")
    @classmethod
    def _channels(cls, v: dict[str, bool]) -> dict[str, bool]:
        bad = [k for k in v if k not in ("in_app", "email", "telegram")]
        if bad:
            raise ValueError(f"unknown notification channels: {', '.join(bad)}")
        return v

    @field_validator("skills", "equipment", "content_exclusions", "topic_preferences")
    @classmethod
    def _short_items(cls, v: list[str]) -> list[str]:
        if any(len(x) > 80 for x in v):
            raise ValueError("each entry must be at most 80 characters")
        return [x.strip() for x in v if x.strip()]
