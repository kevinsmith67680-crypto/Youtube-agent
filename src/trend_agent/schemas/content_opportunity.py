"""Phase 5 demand evidence (from Phase 4), opportunity scores and opportunity reports.

``DemandBasis`` is a *label over* the existing Phase 4 contract, not a new statistic:

| DemandBasis                            | Phase 4 source                                         |
|----------------------------------------|--------------------------------------------------------|
| CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE  | DIRECT_SEARCH_SURGE at stage CONFIRMED                 |
| DIRECT_SEARCH_CANDIDATE                | DIRECT_SEARCH_SURGE at stage CANDIDATE                 |
| EXTERNAL_SEARCH_SURGE                  | EXTERNAL_SEARCH_SURGE (never YouTube search)           |
| POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY   | POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY, PROXY_ATTENTION |
| COLD_START_GROWTH_CANDIDATE            | cold_start_state = COLD_START_GROWTH_CANDIDATE         |
| WATCH                                  | stage WATCH                                            |
| INSUFFICIENT_EVIDENCE                  | stage INSUFFICIENT_EVIDENCE / no usable episode        |
| MANUAL_NO_DEMAND_EVIDENCE              | manual analysis, no Phase 4 episode                    |

The opportunity score is a ranking index (0-100), not a probability of video success.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trend_agent.schemas.content_gap import ContentGap, EvidenceStrength, SupplySummary
from trend_agent.schemas.search_intent import (
    InformationRequirement,
    IntentClassification,
)
from trend_agent.schemas.search_result import (
    FreshnessAssessment,
    RelevanceAssessment,
    RequirementSummary,
)

REPORT_SCHEMA_VERSION: Literal["content-opportunity/1.0"] = "content-opportunity/1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DemandBasis(StrEnum):
    CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE = "confirmed_direct_youtube_search_surge"
    DIRECT_SEARCH_CANDIDATE = "direct_search_candidate"
    EXTERNAL_SEARCH_SURGE = "external_search_surge"
    POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY = "potential_youtube_demand_opportunity"
    COLD_START_GROWTH_CANDIDATE = "cold_start_growth_candidate"
    WATCH = "watch"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    MANUAL_NO_DEMAND_EVIDENCE = "manual_no_demand_evidence"


class AnalysisTrigger(StrEnum):
    SURGE_EVENT = "surge_event"
    MANUAL = "manual"


class DemandEvidence(_Frozen):
    """Phase 4 facts preserved verbatim for the analysis (never recomputed)."""

    basis: DemandBasis
    trigger: AnalysisTrigger
    surge_event_id: int | None = None
    query_id: int | None = None
    canonical_query: str
    category: str | None = None
    topic_cluster_id: int | None = None
    entity_key: str | None = None
    detected_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    measurement_type: str | None = None
    classification: str | None = None
    alert_stage: str | None = None
    confidence_status: str | None = None
    cold_start_state: str | None = None
    severity: float | None = None
    relative_change: float | None = None
    freshness: str | None = None
    limitations: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    source_references: tuple[str, ...] = ()
    synthetic: bool = False
    statement: str


class ScoreComponent(_Frozen):
    name: Literal[
        "demand_evidence",
        "intent_clarity",
        "content_gap_strength",
        "supply_relevance",
        "freshness_opportunity",
        "evidence_quality",
    ]
    value: float | None = Field(description="0-1; None = not available (excluded, not zero)")
    weight: float
    contribution: float | None
    raw: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    note: str


class OpportunityStatus(StrEnum):
    CANDIDATE = "candidate"  # a supported content opportunity worth investigating
    NO_OPPORTUNITY = "no_opportunity"  # NO_VERIFIABLE_GAP
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BLOCKED_POLICY = "blocked_policy"  # e.g. cheats / exploits / account compromise


class OpportunityScore(_Frozen):
    score: float | None = Field(ge=0, le=100, description="ranking index; None = unavailable")
    score_version: str
    components: tuple[ScoreComponent, ...]
    available_weight: float
    cap: float | None = None
    cap_reason: str | None = None
    note: str = "A ranking index for prioritising research, not a probability of success."


class ReportClaimKind(StrEnum):
    FACT = "fact"  # about the world (entity, release, version)
    OBSERVATION = "observation"  # about the inspected evidence (computed by code)
    HYPOTHESIS = "hypothesis"  # a gap inference; never stated as fact
    NARRATIVE = "narrative"  # optional LLM explanation sentence


class ReportClaimStatus(StrEnum):
    VERIFIED = "verified"
    CORROBORATED = "corroborated"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReportClaim(_Frozen):
    kind: ReportClaimKind
    text: str
    evidence_ids: tuple[str, ...]
    status: ReportClaimStatus
    published: bool
    reasons: tuple[str, ...] = ()
    label: str | None = None


class VideoReference(_Frozen):
    video_id: str
    url: str
    channel_title: str | None
    published_at: datetime | None
    relevance_score: float
    entity_match: str
    video_format: str
    freshness: str
    evidence_tier: str
    evidence_id: str | None


class OpportunityReport(_Frozen):
    schema_version: Literal["content-opportunity/1.0"] = REPORT_SCHEMA_VERSION
    opportunity_id: str
    analysis_run_id: str
    query_id: int | None
    canonical_query: str
    category: str | None
    topic_cluster_id: int | None
    entity: str | None
    entity_key: str | None
    intent: IntentClassification
    demand: DemandEvidence
    analysed_at: datetime
    region: str | None
    language: str | None
    inspected_results: int
    coverage_limitations: tuple[str, ...]
    requirements: tuple[InformationRequirement, ...]
    requirement_coverage: tuple[RequirementSummary, ...]
    relevant_videos: tuple[VideoReference, ...]
    gaps: tuple[ContentGap, ...]
    primary_gap: ContentGap | None
    supply: SupplySummary
    score: OpportunityScore
    evidence_quality: EvidenceStrength | None
    status: OpportunityStatus
    verification_state: str
    main_uncertainty: str
    claims: tuple[ReportClaim, ...]
    source_references: tuple[str, ...]
    explanation: dict[str, str] = Field(
        description="audience_wants, existing_content, possibly_missing, why_investigate, "
        "evidence_needed"
    )
    suggested_content_angle: str | None = Field(
        default=None,
        description="the detected need in one sentence; not a title, script or production plan",
    )
    relevance: tuple[RelevanceAssessment, ...] = ()
    freshness: tuple[FreshnessAssessment, ...] = ()
    analysis_version: str
    configuration_version: str
    llm_model: str | None = None
    synthetic: bool
    notice: str = (
        "Content gaps are hypotheses about the inspected results, not proof that existing "
        "videos fail viewers. Opportunity scores rank research priorities; they are not "
        "probabilities."
    )
