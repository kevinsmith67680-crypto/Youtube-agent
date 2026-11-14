"""Dashboard / REST API read and write models (``dashboard/1.0``).

Every chartable value carries what it is (``DataClass``), where it came from, when it was
observed and when it became available. Unavailable data is ``None`` with a reason, never a
substituted number.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trend_agent.schemas.recommendation import (
    CreatorProfile,
    EvidenceRef,
    FeedbackType,
    ProductionStatus,
    RecommendationOutput,
)

DASHBOARD_SCHEMA_VERSION = "dashboard/1.0"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataClass(StrEnum):
    OBSERVED = "OBSERVED"
    FORECAST = "FORECAST"
    PROXY_SIGNAL = "PROXY_SIGNAL"
    SYNTHETIC = "SYNTHETIC"
    MISSING = "MISSING"


# ------------------------------------------------------------------- generic
class Page[T](_M):
    items: list[T]
    total: int
    page: int
    page_size: int


class ErrorBody(_M):
    error: str
    detail: str | Any = None
    status: int


# ---------------------------------------------------------------- opportunities
class OpportunitySummary(_M):
    id: str
    query: str
    category: str | None
    region: str | None
    language: str | None
    intent: str
    sub_intent: str | None
    demand_basis: str
    demand_label: str
    direct_youtube_search: bool
    surge_stage: str | None
    surge_severity: float | None
    gap_type: str | None
    gap_strength: str | None
    forecast_status: str | None
    breakout_probability: float | None
    breakout_calibration: str | None
    opportunity_score: float | None
    time_sensitive: bool
    urgency: str | None
    evidence_quality: str | None
    verification_state: str
    status: str
    recommendation_count: int
    approved_count: int
    last_updated: datetime
    synthetic: bool


class GapView(_M):
    gap_type: str
    statement: str
    evidence_strength: str
    verification_status: str
    requirement_keys: list[str]
    limitations: list[str]
    evidence_ids: list[str]


class RequirementView(_M):
    key: str
    requirement: str
    priority: str
    coverage: str
    statement: str
    covered_by: list[str]
    evidence_tier: str


class CompetingVideoView(_M):
    video_id: str
    url: str
    published_at: datetime | None
    relevance_score: float
    entity_match: str
    video_format: str
    freshness: str
    evidence_tier: str
    finding_basis: str = Field(description="metadata-based or full-content (transcript) finding")


class OpportunityDetail(_M):
    summary: OpportunitySummary
    demand_statement: str
    demand_limitations: list[str]
    intent_entities: list[dict[str, Any]]
    requirements: list[RequirementView]
    gaps: list[GapView]
    primary_gap: GapView | None
    competing_videos: list[CompetingVideoView]
    supply_scope: str
    explanation: dict[str, str]
    main_uncertainty: str
    coverage_limitations: list[str]
    score_components: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    recommendations: list[RecommendationSummary]
    forecasts: list[ForecastView]
    notice: str


class GapRow(_M):
    """One content gap of one opportunity (Content Gap Explorer)."""

    opportunity_id: str
    query: str
    category: str | None
    region: str | None
    is_primary: bool
    gap: GapView
    opportunity_score: float | None
    opportunity_status: str
    demand_label: str
    recommendation_count: int
    analysed_at: datetime
    synthetic: bool


# ---------------------------------------------------------------------- trends
class SeriesPoint(_M):
    period_start: datetime
    period_end: datetime
    available_at: datetime | None
    value: float | None
    quality: str
    revision: int = 1


class TrendSeriesView(_M):
    series_id: int | None
    label: str
    query: str
    provider: str
    measurement: str
    measurement_definition: str
    data_class: DataClass
    granularity: str
    unit: str
    region: str | None
    scale_type: str
    synthetic: bool
    points: list[SeriesPoint]
    freshness_hours: float | None
    note: str


class SurgeEpisodeView(_M):
    id: int | None
    query: str
    start: datetime | None
    end: datetime | None
    detected_at: datetime | None
    stage: str | None
    classification: str | None
    severity: float | None
    measurement: str | None
    statement: str
    synthetic: bool


class ForecastPointView(_M):
    lead_hours: int
    forecast_time: datetime
    value: float | None
    lower: float | None
    upper: float | None
    interval_level: float | None
    method: str
    model: str


class ForecastView(_M):
    prediction_id: str
    query: str
    horizon_hours: int
    status: str
    status_reasons: list[str]
    issued_at: datetime
    data_cutoff: datetime
    breakout_probability: float | None
    breakout_calibration: str | None
    continuation_probability: float | None
    baseline_index: float | None
    threshold_index: float | None
    trajectory: list[ForecastPointView]
    peak: dict[str, Any] | None
    windows: list[dict[str, Any]]
    target_family: str
    source_classification: str
    is_proxy: bool
    synthetic: bool
    model: str | None
    notice: str


class TrendSummary(_M):
    id: str
    query: str
    category: str | None
    provider: str
    measurement: str
    data_class: DataClass
    granularity: str
    latest_value: float | None
    latest_at: datetime | None
    freshness_hours: float | None
    surge_stage: str | None
    opportunity_id: str | None
    synthetic: bool


class TrendDetail(_M):
    summary: TrendSummary
    series: list[TrendSeriesView]
    episodes: list[SurgeEpisodeView]
    forecasts: list[ForecastView]
    evidence: list[EvidenceRef]
    announcements: list[dict[str, Any]]
    limitations: list[str]


# -------------------------------------------------------------- recommendations
class RecommendationSummary(_M):
    id: str
    opportunity_id: str
    title: str
    query: str
    category: str | None
    video_format: str
    format_label: str
    intent: str
    sub_intent: str | None
    score: float | None
    rank: int | None
    status: str
    research: bool
    suppressed: bool
    suppression_reason: str | None
    urgency: str
    hours_low: float
    hours_high: float
    production_profile: str
    complexity: str
    demand_basis: str
    forecast_available: bool
    synthetic: bool
    saved: bool = False
    dismissed: bool = False
    production_status: str | None = None
    generated_at: datetime


class ProductionView(_M):
    status: ProductionStatus
    planned_publication_time: datetime | None
    actual_publication_time: datetime | None
    published_video_id: str | None
    notes: str | None
    updated_at: datetime
    history: list[dict[str, Any]] = Field(default_factory=list)


class UserState(_M):
    saved: bool
    saved_at: datetime | None
    notes: str | None
    dismissed: bool
    production: ProductionView | None
    feedback: dict[str, int]


class RecommendationDetail(_M):
    recommendation: RecommendationOutput
    user: UserState
    opportunity: OpportunitySummary | None


class SavedIdea(_M):
    recommendation: RecommendationSummary
    saved_at: datetime
    notes: str | None
    production: ProductionView | None


# ------------------------------------------------------------- system / overview
class ProviderView(_M):
    name: str
    kind: str
    status: str  # ok | unconfigured | unavailable | degraded | unknown | synthetic
    configured: bool
    detail: str
    last_success: datetime | None = None


class FreshnessView(_M):
    provider: str
    measurement: str
    series: int
    latest_observation: datetime | None
    age_hours: float | None
    status: str


class OverviewView(_M):
    mode: str
    synthetic: bool
    monitored_queries: int | None
    active_surges: int | None
    opportunities: int | None
    approved_recommendations: int | None
    research_candidates: int | None
    recent_topics: list[dict[str, Any]]
    providers: list[ProviderView]
    freshness: list[FreshnessView]
    category_distribution: dict[str, int]
    top_recommendations: list[RecommendationSummary]
    notes: list[str]


class SystemStatusView(_M):
    mode: str
    synthetic: bool
    version: str
    database: str
    components: dict[str, str]
    llm: dict[str, Any]
    versions: dict[str, str]
    counts: dict[str, int | None]
    notifications: list[ProviderView]
    generated_at: datetime


class DataQualityView(_M):
    items: list[FreshnessView]
    notes: list[str]


class AlertView(_M):
    id: str
    event_type: str
    severity: str
    title: str
    body: str
    created_at: datetime
    read: bool
    recommendation_id: str | None
    opportunity_id: str | None
    synthetic: bool
    deliveries: list[dict[str, Any]]


# ------------------------------------------------------------------- requests
class FeedbackIn(_M):
    feedback_type: FeedbackType
    value: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


class SaveIn(_M):
    notes: str | None = Field(default=None, max_length=2000)


class ProductionIn(_M):
    status: ProductionStatus
    planned_publication_time: datetime | None = None
    actual_publication_time: datetime | None = None
    published_video_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{11}$")
    notes: str | None = Field(default=None, max_length=2000)


class OutcomeIn(_M):
    """A creator-entered outcome for a published recommendation (Phase 8, opt-in).

    Only what the creator reads from their own YouTube Studio; nothing is fetched. The
    search-traffic share is optional and never inferred. ``consent`` must be true."""

    days_since_publication: int = Field(ge=0, le=3650)
    published_video_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{11}$")
    views: int | None = Field(default=None, ge=0)
    impressions: int | None = Field(default=None, ge=0)
    click_through_rate: float | None = Field(default=None, ge=0, le=1)
    average_view_duration_seconds: float | None = Field(default=None, ge=0)
    search_traffic_share: float | None = Field(default=None, ge=0, le=1)
    consent: Literal[True] = Field(description="the creator agrees to store this outcome")
    notes: str | None = Field(default=None, max_length=2000)


class GenerateIn(_M):
    category: str | None = Field(default=None, pattern=r"^(ai|technology|gaming)$")
    all: bool = False
    max_opportunities: int = Field(default=5, ge=1, le=100)
    dry_run: bool = False


class GenerateResult(_M):
    run_id: str
    considered: int
    generated: int
    approved: int
    research: int
    rejected: int
    suppressed: int
    skipped: list[str]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    seconds: float
    synthetic: bool
    generator: str
    reused: int = Field(default=0, description="unchanged evidence: stored ideas reused")
    notes: list[str] = Field(default_factory=list)
    dry_run: bool = False


class ProfileIn(CreatorProfile):
    pass


OpportunityDetail.model_rebuild()
