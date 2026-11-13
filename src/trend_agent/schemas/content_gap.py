"""Content-gap and supply models (Phase 5).

A gap is a *hypothesis about the inspected evidence*, never a proven failure of existing
videos. Every gap states its evidence strength, limitations and the analysis scope.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GapType(StrEnum):
    NO_RELEVANT_RESULTS = "no_relevant_results"
    LOW_RELEVANCE = "low_relevance"
    POTENTIALLY_OUTDATED = "potentially_outdated"
    MISSING_SPECIFIC_ANSWER = "missing_specific_answer"
    INSUFFICIENT_PRACTICAL_DEMONSTRATION = "insufficient_practical_demonstration"
    VERSION_MISMATCH = "version_mismatch"
    LANGUAGE_OR_REGION_GAP = "language_or_region_gap"
    FORMAT_GAP = "format_gap"
    NO_VERIFIABLE_GAP = "no_verifiable_gap"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


OPPORTUNITY_GAPS = frozenset(
    {
        GapType.NO_RELEVANT_RESULTS,
        GapType.LOW_RELEVANCE,
        GapType.POTENTIALLY_OUTDATED,
        GapType.MISSING_SPECIFIC_ANSWER,
        GapType.INSUFFICIENT_PRACTICAL_DEMONSTRATION,
        GapType.VERSION_MISMATCH,
        GapType.LANGUAGE_OR_REGION_GAP,
        GapType.FORMAT_GAP,
    }
)


class EvidenceStrength(StrEnum):
    STRONG = "strong"  # verified facts + adequate sample + relevant evidence tier
    MODERATE = "moderate"
    WEAK = "weak"  # metadata-only inference or small sample: a hypothesis


STRENGTH_RANK = {EvidenceStrength.WEAK: 1, EvidenceStrength.MODERATE: 2, EvidenceStrength.STRONG: 3}


class GapVerification(StrEnum):
    VERIFIED = "verified"  # every factual premise verified, observation reproducible
    HYPOTHESIS = "hypothesis"  # inferred from incomplete (e.g. metadata-only) evidence
    UNVERIFIED = "unverified"
    REJECTED = "rejected"  # failed verification


class ContentGap(_Frozen):
    gap_type: GapType
    target_query: str
    intent: str
    sub_intent: str | None
    requirement_keys: tuple[str, ...] = ()
    statement: str = Field(description="deterministic, scope-limited description")
    supporting_evidence: tuple[str, ...] = Field(description="evidence IDs")
    evidence_strength: EvidenceStrength
    limitations: tuple[str, ...]
    analysis_method: str
    detected_at: datetime
    verification_status: GapVerification
    observations: dict[str, float | int | str | None] = Field(default_factory=dict)


class SupplyClass(StrEnum):
    OBSERVED_SUPPLY = "observed_supply"  # counted within the inspected result set
    ESTIMATED_SUPPLY = "estimated_supply"  # an API estimate (never used as a count)
    UNKNOWN_SUPPLY = "unknown_supply"  # nothing retrieved


class SupplySummary(_Frozen):
    supply_class: SupplyClass
    scope: str = Field(description='e.g. "Of the 30 retrieved results inspected, ..."')
    inspected: int
    relevant: int
    highly_relevant: int
    recent_relevant: int
    exact_entity: int
    older_version: int
    distinct_channels: int
    formats: dict[str, int] = Field(default_factory=dict)
    directly_relevant_formats: dict[str, int] = Field(default_factory=dict)
    share_older_version: float | None = None
    api_total_estimate: int | None = Field(
        default=None, description="reported by the API; not a count of competing videos"
    )
    statements: tuple[str, ...] = ()
    disabled_metrics: tuple[str, ...] = ()
