"""Phase 5 retrieved-result, relevance, freshness and coverage models.

``api_position`` is the order of one specific API response (for one query variant,
region, language and request time). It is **not** a YouTube search ranking position:
the YouTube Data API does not reproduce a logged-in viewer's results.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceTier(StrEnum):
    """What was actually inspected for a video."""

    A_METADATA = "a_metadata"  # title, description, dates, duration
    B_CAPTIONS = "b_captions"  # authorised captions/transcripts
    C_FULL_CONTENT = "c_full_content"  # authorised full-content analysis
    D_INSUFFICIENT = "d_insufficient"  # nothing usable was available


TIER_RANK = {
    EvidenceTier.D_INSUFFICIENT: 0,
    EvidenceTier.A_METADATA: 1,
    EvidenceTier.B_CAPTIONS: 2,
    EvidenceTier.C_FULL_CONTENT: 3,
}


class RetrievalRequest(_Frozen):
    query: str
    variants: tuple[str, ...] = ()
    region: str | None = None
    language: str | None = None
    published_after: datetime | None = None
    max_results_per_variant: int = Field(default=25, ge=1, le=50)


class RetrievalStatus(StrEnum):
    FETCHED = "fetched"
    CACHED = "cached"
    PARTIAL = "partial"  # some variants failed
    UNAVAILABLE = "unavailable"  # provider not configured / quota / error


class CandidateVideo(_Frozen):
    """Normalised, permitted metadata of one retrieved video (untrusted text, sanitised)."""

    video_id: str
    title: str
    description: str | None
    channel_id: str
    channel_title: str | None
    published_at: datetime | None
    duration_seconds: int | None = None
    default_language: str | None = None
    default_audio_language: str | None = None
    url: str
    variant: str = Field(description="the query variant that returned it")
    api_position: int = Field(ge=1, description="order within that one API response")
    collected_at: datetime
    expires_at: datetime | None = None
    tier: EvidenceTier = EvidenceTier.A_METADATA
    transcript: str | None = Field(default=None, description="only with authorised access")
    injection_flags: tuple[str, ...] = ()
    evidence_id: str | None = None
    synthetic: bool = False


class RetrievedResultSet(_Frozen):
    request: RetrievalRequest
    status: RetrievalStatus
    videos: tuple[CandidateVideo, ...]
    provider: str
    collected_at: datetime | None
    requests_made: int = 0
    quota_units: int = 0
    cache_hits: int = 0
    approx_total_reported: int | None = Field(
        default=None,
        description="the API's totalResults estimate: not a count of competing videos",
    )
    warnings: tuple[str, ...] = ()


class VideoFormat(StrEnum):
    TUTORIAL = "tutorial"
    DEMONSTRATION = "demonstration"
    NEWS = "news"
    REVIEW = "review"
    COMPARISON = "comparison"
    GUIDE = "guide"
    SHORT = "short"
    ENTERTAINMENT = "entertainment"
    UNKNOWN = "unknown"


class EntityMatch(StrEnum):
    EXACT = "exact"  # same entity and version (or no version involved)
    FAMILY = "family"  # same family, version not stated
    VERSION_MISMATCH = "version_mismatch"  # same family, a different version
    OTHER_ENTITY = "other_entity"  # a different entity sharing the name
    NONE = "none"  # the entity is not mentioned
    NOT_APPLICABLE = "not_applicable"  # the query has no entity


class RelevanceAssessment(_Frozen):
    video_id: str
    score: float = Field(ge=0, le=100, description="uncalibrated relevance index")
    relevant: bool
    highly_relevant: bool
    entity_match: EntityMatch
    matched_entity: str | None
    matched_versions: tuple[str, ...] = ()
    intent_match: bool
    matched_intent_cues: tuple[str, ...] = ()
    video_format: VideoFormat
    keyword_coverage: float = Field(ge=0, le=1)
    language_compatible: bool | None
    evidence_tier: EvidenceTier
    explanation: str
    limitations: tuple[str, ...] = ()
    algorithm_version: str
    components: dict[str, float] = Field(default_factory=dict)


class FreshnessStatus(StrEnum):
    CURRENT = "current"
    POTENTIALLY_OUTDATED = "potentially_outdated"
    OUTDATED_WITH_EVIDENCE = "outdated_with_evidence"
    UNKNOWN = "unknown"


class FreshnessAssessment(_Frozen):
    video_id: str
    status: FreshnessStatus
    reason: str
    reference_event: str | None = None
    reference_date: datetime | None = None
    evidence_ids: tuple[str, ...] = ()
    time_sensitive: bool


class CoverageStatus(StrEnum):
    COVERED = "covered"
    PARTIALLY_COVERED = "partially_covered"
    NOT_EVIDENCED = "not_evidenced"  # NOT "missing": the inspected evidence does not show it
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"


class CoverageCell(_Frozen):
    requirement_key: str
    video_id: str
    status: CoverageStatus
    evidence_tier: EvidenceTier
    matched_cues: tuple[str, ...] = ()
    note: str
    evidence_id: str | None = None


class RequirementSummary(_Frozen):
    requirement_key: str
    requirement: str
    priority: str
    best_status: CoverageStatus
    covered_by: tuple[str, ...] = ()
    partially_by: tuple[str, ...] = ()
    inspected_videos: int
    max_tier: EvidenceTier
    statement: str
