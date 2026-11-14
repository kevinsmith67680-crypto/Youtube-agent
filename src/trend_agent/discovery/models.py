"""Keyword-discovery domain models.

Discovery finds and prioritises *candidate* keywords. Nothing in this package measures
search demand: a discovered keyword, however well corroborated, is a hypothesis to be
monitored by later phases. The types below make that explicit:

* ``EvidenceType`` says what kind of source material supports a candidate (an official
  announcement, a video title, a community post, ...). None of them is search volume.
* ``KeywordOrigin`` separates queries actually returned by a query-level provider
  (``OBSERVED_QUERY``) from phrases we generated (``GENERATED_HYPOTHESIS``). Only a later
  phase may mark a query ``VERIFIED_INTEREST``, and only from real search-interest data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trend_agent.utils.time import ensure_utc


class Category(StrEnum):
    AI = "ai"
    TECHNOLOGY = "technology"
    GAMING = "gaming"
    OTHER = "other"


TARGET_CATEGORIES: tuple[Category, ...] = (Category.AI, Category.TECHNOLOGY, Category.GAMING)


class EvidenceType(StrEnum):
    OFFICIAL_ANNOUNCEMENT = "official_announcement"
    VIDEO_TITLE = "video_title"
    VIDEO_DESCRIPTION = "video_description"
    RELATED_QUERY = "related_query"
    SEARCH_SUGGESTION = "search_suggestion"
    COMMUNITY_DISCUSSION = "community_discussion"
    CODE_RELEASE = "code_release"
    # A repository's own record (name, self-description, topics) from the GitHub API: the
    # identity is verified by GitHub; the description is the owner's unverified claim.
    REPOSITORY_METADATA = "repository_metadata"
    # A game's record in an official platform catalog (Twitch Get Games: ID, name, IGDB ID).
    # Verifies the game exists under this name; says nothing about updates, events or demand.
    GAME_CATALOG = "game_catalog"
    CURATED_SEED = "curated_seed"
    SYNTHETIC_TEST_DATA = "synthetic_test_data"


# Evidence types whose phrases are *queries people typed* according to the provider.
QUERY_LEVEL_EVIDENCE = frozenset({EvidenceType.RELATED_QUERY, EvidenceType.SEARCH_SUGGESTION})


class EntityType(StrEnum):
    AI_MODEL = "ai_model"
    AI_PRODUCT = "ai_product"
    COMPANY = "company"
    SOFTWARE = "software"
    FRAMEWORK = "framework"
    GAME = "game"
    GAME_EVENT = "game_event"
    GAME_ITEM = "game_item"
    GAME_UPDATE = "game_update"
    HARDWARE = "hardware"
    TECHNOLOGY = "technology"
    RELEASE = "release"
    FEATURE = "feature"


class EntityStatus(StrEnum):
    VERIFIED = "verified"  # grounded in source text and corroborated enough to expand
    NEEDS_VERIFICATION = "needs_verification"  # grounded but weak (e.g. one community claim)
    REJECTED = "rejected"  # e.g. an LLM-proposed name absent from the source text


class EntityOrigin(StrEnum):
    GAZETTEER = "gazetteer"  # curated configuration
    EXTRACTED = "extracted"  # deterministic extraction from evidence
    LLM_PROPOSED = "llm_proposed"  # optional LLM extractor (always grounding-checked)


class SearchIntent(StrEnum):
    TUTORIAL = "tutorial"
    HOW_TO = "how_to"
    EXPLANATION = "explanation"
    NEWS = "news"
    COMPARISON = "comparison"
    REVIEW = "review"
    DEMONSTRATION = "demonstration"
    TROUBLESHOOTING = "troubleshooting"
    GAME_GUIDE = "game_guide"
    ITEM_LOCATION = "item_location"
    CHALLENGE_GUIDE = "challenge_guide"
    PRODUCT_DISCOVERY = "product_discovery"
    OTHER = "other"


class KeywordOrigin(StrEnum):
    OBSERVED_QUERY = "observed_query"  # returned by a suggestion / related-query provider
    GENERATED_HYPOTHESIS = "generated_hypothesis"  # produced by us from entities/templates
    VERIFIED_INTEREST = "verified_interest"  # reserved: set only from real interest data


class DiscoveryMethod(StrEnum):
    OBSERVED_QUERY = "observed_query"
    ENTITY_TEMPLATE = "entity_template"
    ENTITY_COMPARISON = "entity_comparison"
    TITLE_PHRASE = "title_phrase"
    SEED_TERM = "seed_term"
    LLM_SUGGESTION = "llm_suggestion"


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    PRIORITIZED = "prioritized"
    MONITORING = "monitoring"
    DORMANT = "dormant"
    REJECTED = "rejected"


class NoveltyKind(StrEnum):
    NEW_ENTITY = "new_entity"
    NEW_VERSION = "new_version"
    NEW_EVENT = "new_event"
    NEW_INTENT = "new_intent"
    RECURRING = "recurring"
    EVERGREEN = "evergreen"


class SourceType(StrEnum):
    FEED = "feed"
    API_QUERY = "api_query"
    CHART = "chart"
    CHANNEL = "channel"
    REPOSITORY = "repository"
    COMMUNITY = "community"
    SEED = "seed"
    EXPLORATION_QUERY = "exploration_query"
    REPOSITORY_SEARCH = "repository_search"  # GitHub repository search (bounded, dated)
    SYNTHETIC = "synthetic"


class SourcePool(StrEnum):
    EXPLOITATION = "exploitation"
    EXPLORATION = "exploration"


class SourceHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"


class EvidenceRelation(StrEnum):
    """How a piece of evidence supports a keyword."""

    QUERY_OBSERVED = "query_observed"  # the item lists this exact query
    ENTITY_MENTION = "entity_mention"  # the item mentions the keyword's entity
    TEMPLATE_GROUNDING = "template_grounding"  # the item grounds a template requirement


# ---------------------------------------------------------------------------- sources


class DiscoverySource(BaseModel):
    """A configured place to look for candidates (feed, query, chart, channel, seed)."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(pattern=r"^[A-Za-z0-9_:\-./@]+$", max_length=200)
    provider: str
    name: str
    source_type: SourceType
    category: Category
    reference: str = Field(description="URL, query text, channel handle, repo, ...")
    pool: SourcePool = SourcePool.EXPLOITATION
    enabled: bool = True
    reliability: float = Field(default=0.7, ge=0.0, le=1.0)
    min_poll_interval_minutes: int = Field(default=360, ge=1)
    evidence_type: EvidenceType | None = None
    publisher: str | None = None
    region: str | None = None
    language: str | None = None
    entity_hint: str | None = Field(
        default=None, description="Entity a release feed is about (e.g. a package name)"
    )
    entity_hint_type: str | None = None
    verified: bool = Field(
        default=False, description="Reference confirmed to exist by the configuration author"
    )
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------- items


class DiscoveryItem(BaseModel):
    """One piece of source material returned by a discovery provider."""

    model_config = ConfigDict(frozen=True)

    provider_name: str
    source_key: str
    source_reference: str = Field(min_length=1, description="Stable URL or API identifier")
    evidence_type: EvidenceType
    title: str = Field(min_length=1, max_length=1000)
    text: str | None = Field(default=None, description="Body/description used for extraction")
    url: str | None = None
    publisher: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    collected_at: datetime
    category_hint: Category | None = None
    observed_queries: tuple[str, ...] = ()
    entity_hint: str | None = None
    entity_hint_type: str | None = None
    is_synthetic: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("published_at", "collected_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @property
    def full_text(self) -> str:
        """Title and body as separate sentences (patterns must never span the two)."""
        if not self.text:
            return self.title
        title = self.title.rstrip()
        separator = " " if title.endswith((".", "!", "?")) else ". "
        return f"{title}{separator}{self.text}"


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: DiscoverySource
    since: datetime | None = None
    max_items: int = Field(default=50, ge=1, le=500)
    etag: str | None = None
    last_modified: str | None = None

    @field_validator("since")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_name: str
    source_key: str
    items: tuple[DiscoveryItem, ...] = ()
    not_modified: bool = Field(default=False, description="Source unchanged since last fetch")
    etag: str | None = None
    last_modified: str | None = None
    warnings: tuple[str, ...] = ()
    quota_used: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------- entities


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_name: str
    entity_type: EntityType
    category: Category
    matched_text: str
    aliases: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    extractor: str
    origin: EntityOrigin = EntityOrigin.EXTRACTED
    parent_name: str | None = None
    parent_type: EntityType | None = None
    version: str | None = None
    attributes: tuple[str, ...] = Field(
        default=(), description="Grounded capability flags, e.g. 'local', 'api', 'pricing'"
    )
    related: tuple[str, ...] = Field(
        default=(), description="Co-mentioned entities of the same kind (comparison targets)"
    )
    status: EntityStatus = EntityStatus.VERIFIED
    flags: tuple[str, ...] = ()
