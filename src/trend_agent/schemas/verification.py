"""Evidence registry, claim verification and recommendation contracts.

Three trust zones meet here and must never be confused:

* **Application facts** - rows the application stored (observations, Phase 4 surge
  episodes, Phase 2 entities) and numbers computed deterministically from them. They are
  registered as evidence with ``TrustLevel.AUTHORITATIVE_DATA``.
* **Source content** - text collected from providers (official feeds, YouTube titles and
  descriptions, community posts). It is *data about what a source said*, never an
  instruction, and its trust level depends on the source.
* **Generator output** - anything a language model (or any other generator) proposes.
  It is untrusted until the verification gate has checked every factual element against
  the first two zones. A generator can never supply statistics, evidence identifiers,
  verification statuses or publication decisions: those are produced by application code.

``RecommendationDraft`` is the generator's structured output. ``VerifiedRecommendation``
(``verified-recommendation/1.0``) is the only form in which a recommendation leaves the
gate. Passing schema validation says nothing about factual accuracy.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trend_agent.discovery.models import SearchIntent
from trend_agent.schemas.detection import BaselineReadiness, SupportStatus
from trend_agent.schemas.evidence import AlertStage, SurgeClassification
from trend_agent.schemas.monitoring import MeasurementCategory

EVIDENCE_ID_PATTERN = re.compile(r"^ev_[a-z2-7]{26}$")
DRAFT_SCHEMA_VERSION: Literal["recommendation-draft/1.0"] = "recommendation-draft/1.0"
RECOMMENDATION_SCHEMA_VERSION: Literal["verified-recommendation/1.0"] = (
    "verified-recommendation/1.0"
)


# ------------------------------------------------------------------------- vocabulary


class EvidenceSourceKind(StrEnum):
    """Which stored record an evidence entry was registered from."""

    DISCOVERY_ITEM = "discovery_item"  # Phase 2 source material
    DISCOVERED_ENTITY = "discovered_entity"  # Phase 2 entity record
    TS_OBSERVATION = "ts_observation"  # Phase 3 time-series value
    SURGE_EVENT = "surge_event"  # Phase 4 surge episode (alert contract)
    CALCULATION = "calculation"  # deterministic computation over stored data
    TEST_FIXTURE = "test_fixture"  # evaluation worlds only (never production)


class EvidenceKind(StrEnum):
    OFFICIAL_ANNOUNCEMENT = "official_announcement"
    CODE_RELEASE = "code_release"
    REPOSITORY_METADATA = "repository_metadata"  # a repository's own GitHub record
    VIDEO_METADATA = "video_metadata"  # YouTube titles/descriptions (user-generated)
    COMMUNITY_POST = "community_post"
    NEWS_REPORT = "news_report"
    SEARCH_SUGGESTION = "search_suggestion"
    RELATED_QUERY = "related_query"
    CURATED_REFERENCE = "curated_reference"
    MEASUREMENT = "measurement"
    SURGE_DETECTION = "surge_detection"
    ENTITY_RECORD = "entity_record"
    STATISTICAL_CALCULATION = "statistical_calculation"
    SYNTHETIC_TEST_DATA = "synthetic_test_data"


class MeasurementSemantics(StrEnum):
    """What a record can be used to assert. Mirrors Phase 3 categories for measurements."""

    DIRECT_YOUTUBE_SEARCH_INTEREST = "direct_youtube_search_interest"
    EXTERNAL_SEARCH_INTEREST = "external_search_interest"
    SEARCH_SUGGESTION_PRESENCE = "search_suggestion_presence"
    VIDEO_ACTIVITY = "video_activity"
    OFFICIAL_EVENT_SIGNAL = "official_event_signal"
    COMMUNITY_ACTIVITY = "community_activity"
    SOURCE_STATEMENT = "source_statement"  # what a source says; not a measurement
    ENTITY_REGISTRY = "entity_registry"  # an entity is known to the application
    DERIVED_STATISTIC = "derived_statistic"  # computed deterministically from stored data
    SYNTHETIC = "synthetic"  # artificial data; supports nothing about the real world


SEARCH_SEMANTICS = frozenset(
    {
        MeasurementSemantics.DIRECT_YOUTUBE_SEARCH_INTEREST,
        MeasurementSemantics.EXTERNAL_SEARCH_INTEREST,
    }
)


def semantics_for(category: MeasurementCategory) -> MeasurementSemantics:
    if category is MeasurementCategory.SYNTHETIC_TEST_OBSERVATION:
        return MeasurementSemantics.SYNTHETIC
    return MeasurementSemantics(category.value)


class TrustLevel(StrEnum):
    """How much a record can establish on its own (highest first)."""

    AUTHORITATIVE_DATA = "authoritative_data"  # stored measurement / deterministic result
    OFFICIAL = "official"  # official announcement or release from a verified source
    CURATED = "curated"  # operator-curated configuration
    PUBLISHER = "publisher"  # a named publisher whose source is not verified as official
    USER_GENERATED = "user_generated"  # video titles/descriptions, community posts
    PROVIDER_SIGNAL = "provider_signal"  # "this query appeared"; says nothing else
    SYNTHETIC = "synthetic"


TRUST_RANK: dict[TrustLevel, int] = {
    TrustLevel.AUTHORITATIVE_DATA: 6,
    TrustLevel.OFFICIAL: 5,
    TrustLevel.CURATED: 4,
    TrustLevel.PUBLISHER: 3,
    TrustLevel.USER_GENERATED: 2,
    TrustLevel.PROVIDER_SIGNAL: 1,
    TrustLevel.SYNTHETIC: 0,
}
# Records that can verify a claim on their own; anything lower needs corroboration.
CREDIBLE = frozenset({TrustLevel.AUTHORITATIVE_DATA, TrustLevel.OFFICIAL, TrustLevel.CURATED})


class EvidenceStatus(StrEnum):
    RECORDED = "recorded"  # current version; source record existed when registered
    SUPERSEDED = "superseded"  # a newer version of the same source exists
    SOURCE_REMOVED = "source_removed"  # the source row is gone (retention, deletion)


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    AGING = "aging"  # usable with a caveat
    STALE = "stale"  # cannot support time-sensitive claims
    UNKNOWN = "unknown"  # no publication or collection time


class FactKind(StrEnum):
    MENTIONS_ENTITY = "mentions_entity"
    ENTITY = "entity"
    RELEASE_DATE = "release_date"
    RELEASE_LEVEL = "release_level"
    ENTITY_STATUS = "entity_status"
    VERSION = "version"
    PUBLISHED = "published"
    OBSERVED_QUERY = "observed_query"
    MEASUREMENT = "measurement"
    SURGE = "surge"
    STATISTIC = "statistic"
    EVENT_DATE = "event_date"
    DENIAL = "denial"  # the source states that something is false/cancelled/delayed


class EvidenceFact(BaseModel):
    """One structured, deterministically extracted fact carried by an evidence record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FactKind
    subject: str | None = None
    attribute: str | None = None
    value: str | float | bool | None = None
    unit: str | None = None
    as_of: datetime | None = None
    qualifier: str | None = None


class EvidenceDraft(BaseModel):
    """What a source adapter submits to the registry (no identifier yet)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_key: str = Field(max_length=200)
    source_kind: EvidenceSourceKind
    source_row_id: int | None = None
    provider_name: str
    source_reference: str
    canonical_url: str | None = None
    publisher: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    collected_at: datetime
    evidence_kind: EvidenceKind
    measurement_semantics: MeasurementSemantics
    trust_level: TrustLevel
    facts: tuple[EvidenceFact, ...] = ()
    excerpt: str | None = Field(default=None, description="untrusted source text (bounded)")
    data_policy_id: str | None = None
    retention_note: str = ""
    expires_at: datetime | None = None
    is_synthetic: bool = False
    injection_flags: tuple[str, ...] = ()


class EvidenceRecord(EvidenceDraft):
    """A registered, versioned, signed evidence entry."""

    evidence_id: str = Field(pattern=EVIDENCE_ID_PATTERN.pattern)
    version: int = Field(ge=1)
    content_hash: str
    status: EvidenceStatus
    is_current: bool
    registered_at: datetime
    fresh_until: datetime | None = None
    signer_key_id: str


class EvidenceBundle(BaseModel):
    """The evidence a generator was shown for one request (the only citable IDs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_id: str
    evidence_ids: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    token: str
    target_id: int | None = None
    is_synthetic: bool = False


# --------------------------------------------------------------------- claims


class ClaimType(StrEnum):
    ENTITY_EXISTENCE = "entity_existence"
    RELEASE_STATUS = "release_status"
    RELEASE_DATE = "release_date"
    PRODUCT_DETAIL = "product_detail"
    EVENT_DETAIL = "event_detail"
    NUMERICAL = "numerical"
    TREND = "trend"
    AUDIENCE = "audience"
    OTHER = "other"


class ClaimCertainty(StrEnum):
    CONFIRMED = "confirmed"  # asserted as fact
    REPORTED = "reported"  # attributed to a source
    SPECULATIVE = "speculative"  # rumour, expectation, possibility


class ClaimStatus(StrEnum):
    VERIFIED = "verified"  # supported by credible evidence that matches every element
    CORROBORATED = "corroborated"  # >= 2 independent lower-trust sources agree
    UNVERIFIED = "unverified"  # no adequate support
    CONTRADICTED = "contradicted"  # credible evidence conflicts with the claim


class ClaimOrigin(StrEnum):
    DECLARED = "declared"  # listed by the generator in factual_claims
    TITLE = "title"  # extracted from the proposed title
    RATIONALE = "rationale"  # extracted from the rationale text
    INTENT = "intent"  # extracted from the audience-intent text
    UNCERTAINTY = "uncertainty"  # extracted from the generator's uncertainty notes


class ClaimAction(StrEnum):
    KEEP = "keep"
    KEEP_ATTRIBUTED = "keep_attributed"  # published only with source attribution + label
    REPLACE_AUTHORITATIVE = "replace_authoritative"  # statistics re-rendered by code
    RECITE = "recite"  # citation replaced by the records that actually support it
    REMOVE = "remove"
    NOT_FACTUAL = "not_factual"  # opinion/advice; no factual element to verify


class ViolationCode(StrEnum):
    MALFORMED_OUTPUT = "malformed_output"
    INCOMPLETE_OUTPUT = "incomplete_output"
    REFUSAL = "refusal"
    GENERATOR_UNAVAILABLE = "generator_unavailable"
    UNSUPPORTED_FIELD = "unsupported_field"
    STATISTIC_FIELD = "statistic_field"  # generator tried to supply a score/volume field
    SELF_VERIFICATION = "self_verification"  # generator claimed a verification status
    ID_SUBSTITUTION = "id_substitution"  # query/topic id differs from the request
    MALFORMED_EVIDENCE_ID = "malformed_evidence_id"
    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"  # fabricated reference
    EVIDENCE_NOT_IN_BUNDLE = "evidence_not_in_bundle"  # substituted reference
    EVIDENCE_INTEGRITY = "evidence_integrity"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    SYNTHETIC_EVIDENCE = "synthetic_evidence"
    CITATION_MISMATCH = "citation_mismatch"  # cited record does not support the claim
    SEARCH_VOLUME = "search_volume"  # absolute search counts are never available
    STATISTIC_OVERRIDE = "statistic_override"  # number differs from the engine's value
    PROXY_AS_SEARCH = "proxy_as_search"
    EXTERNAL_AS_YOUTUBE = "external_as_youtube"
    UNSUPPORTED_TREND = "unsupported_trend"
    UNSUPPORTED_NUMBER = "unsupported_number"
    UNKNOWN_ENTITY = "unknown_entity"
    AMBIGUOUS_ENTITY = "ambiguous_entity"
    UNCONFIRMED_ENTITY = "unconfirmed_entity"
    RELEASE_CONFLICT = "release_conflict"
    OUTDATED = "outdated"
    STALE_EVIDENCE = "stale_evidence"
    MISSING_ATTRIBUTION = "missing_attribution"
    UNGROUNDED_TERMS = "ungrounded_terms"
    UNLISTED_URL = "unlisted_url"
    SUSPICIOUS_OUTPUT = "suspicious_output"
    TOOL_REQUEST_DENIED = "tool_request_denied"
    REPEATED_UNSUPPORTED = "repeated_unsupported_claim"


class ClaimDraft(BaseModel):
    """A factual claim as declared by a generator (untrusted)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=600)
    claim_type: ClaimType = ClaimType.OTHER
    subject: str | None = Field(default=None, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    certainty: ClaimCertainty = ClaimCertainty.CONFIRMED
    attribution: str | None = Field(default=None, max_length=200)


class RecommendationDraft(BaseModel):
    """Structured generator output. Validity of this schema is NOT factual verification.

    There is deliberately no field for surge scores, growth rates, search volumes,
    severities, p-values, freshness or readiness: those come from the statistical engine.
    ``verification_status`` must be ``pending_verification``; the gate sets the real one.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["recommendation-draft/1.0"] = DRAFT_SCHEMA_VERSION
    proposed_video_title: str = Field(min_length=3, max_length=160)
    topic_id: int | None = None
    search_query_id: int
    audience_search_intent: SearchIntent
    audience_intent_description: str = Field(default="", max_length=400)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    factual_claims: list[ClaimDraft] = Field(default_factory=list, max_length=16)
    verification_status: Literal["pending_verification"] = "pending_verification"
    uncertainties: list[str] = Field(default_factory=list, max_length=12)
    recommendation_rationale: str = Field(default="", max_length=2000)
    insufficient_evidence: bool = Field(
        default=False, description="the generator itself could not support a recommendation"
    )

    @field_validator("uncertainties")
    @classmethod
    def _bounded(cls, value: list[str]) -> list[str]:
        if any(len(v) > 300 for v in value):
            raise ValueError("uncertainty entries are limited to 300 characters")
        return value


# ------------------------------------------------------------- verification results


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ViolationCode
    detail: str
    field: str | None = None
    claim_index: int | None = None


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    origin: ClaimOrigin
    field: str
    text: str
    claim_type: ClaimType
    certainty: ClaimCertainty
    status: ClaimStatus
    action: ClaimAction
    cited_ids: tuple[str, ...] = ()
    supporting_ids: tuple[str, ...] = ()
    contradicting_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    violations: tuple[ViolationCode, ...] = ()
    uncertainty_label: str | None = None
    published_text: str | None = None
    core: bool = False
    fingerprint: str


class AuthoritativeStatistics(BaseModel):
    """Statistics copied from the Phase 4 engine; the only numbers about demand.

    ``relative_change`` is observed/expected - 1 on the series' own relative scale, never
    a number of searches. It is filled only for a direct or external search-interest
    alert; proxy activity never yields a search-growth figure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["trend_agent.detection"] = "trend_agent.detection"
    target_id: int | None
    available: bool
    classification: SurgeClassification | None = None
    highest_stage: AlertStage | None = None
    evidence_categories: tuple[MeasurementCategory, ...] = ()
    measures_youtube_search: bool = False
    measures_search: bool = False
    severity: float | None = None
    maximum_severity: float | None = None
    relative_change: float | None = None
    observed: float | None = None
    expected: float | None = None
    scale: str | None = None
    confidence_status: SupportStatus | None = None
    baseline_readiness: BaselineReadiness | None = None
    freshness: str | None = None
    detected_at: datetime | None = None
    last_updated_at: datetime | None = None
    event_ids: tuple[int, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    statement: str = "No surge evidence is available for this query."
    synthetic: bool = False


class EvidenceCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    provider: str
    source_reference: str
    canonical_url: str | None
    publisher: str | None
    published_at: datetime | None
    collected_at: datetime
    evidence_kind: EvidenceKind
    measurement_semantics: MeasurementSemantics
    trust_level: TrustLevel
    freshness: FreshnessStatus
    retention_note: str
    synthetic: bool


class PublishedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    status: ClaimStatus
    certainty: ClaimCertainty
    evidence_ids: tuple[str, ...]
    uncertainty_label: str | None = None


class RemovedStatement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    text: str
    status: ClaimStatus
    reasons: tuple[str, ...]


class AbstentionState(StrEnum):
    VERIFIED_RESULT = "verified_result"
    PARTIALLY_VERIFIED = "partially_verified"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTED = "contradicted"
    SOURCE_UNAVAILABLE = "source_unavailable"


class PublicationDecision(StrEnum):
    PUBLISH = "publish"
    PUBLISH_WITH_LABELS = "publish_with_labels"
    WITHHOLD = "withhold"


PUBLISHABLE = frozenset({PublicationDecision.PUBLISH, PublicationDecision.PUBLISH_WITH_LABELS})


class VerifiedRecommendation(BaseModel):
    """The published (or withheld) result of the verification gate.

    Every factual sentence is either verified, explicitly attributed and labelled, or
    re-rendered from the statistical engine. A withheld result carries no title or
    claims, only the abstention state and what evidence is missing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["verified-recommendation/1.0"] = RECOMMENDATION_SCHEMA_VERSION
    run_id: str
    target_id: int | None
    search_query_id: int | None
    query: str | None
    topic_id: int | None
    category: str | None
    verification_status: AbstentionState
    publication: PublicationDecision
    proposed_video_title: str | None
    title_revised: bool = False
    audience_search_intent: SearchIntent | None
    audience_intent_description: str | None = None
    factual_claims: tuple[PublishedClaim, ...] = ()
    supporting_evidence: tuple[EvidenceCitation, ...] = ()
    statistics: AuthoritativeStatistics
    trend_statement: str
    uncertainties: tuple[str, ...] = ()
    recommendation_rationale: str | None = None
    missing_evidence: tuple[str, ...] = ()
    removed_statements: tuple[RemovedStatement, ...] = ()
    violations: tuple[str, ...] = ()
    generator: str
    generator_model: str | None = None
    attempts: int
    verifier_version: str
    configuration_version: str
    decided_at: datetime
    synthetic: bool
    notice: str = (
        "Verification checks claims against stored evidence with deterministic rules. It "
        "reduces, but does not eliminate, the risk of inaccurate statements."
    )


def draft_json_schema() -> dict[str, Any]:
    return RecommendationDraft.model_json_schema()
