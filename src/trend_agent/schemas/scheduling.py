"""Provider freshness/capability profiles and scheduling results.

Profiles describe what a source can actually deliver. Polling faster than a source
publishes cannot create finer measurements: a daily source checked every five minutes
still yields one value per day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trend_agent.discovery.models import Category
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory, MonitoringTier


class RevisionBehaviour(StrEnum):
    IMMUTABLE = "immutable"  # published values never change; a change is a conflict
    REVISABLE = "revisable"  # values may be republished (lineage kept)
    PARTIAL_PERIODS_REVISED = "partial_periods_revised"  # only in-progress periods change
    UNKNOWN = "unknown"  # treat changes as revisions, flag frequent ones


class NormalizationBehaviour(StrEnum):
    CONSISTENT_INDEX = "consistent_index"  # comparable across requests (same scale version)
    PER_REQUEST = "per_request"  # 0-100 relative to each request: never combine requests
    ABSOLUTE = "absolute"  # counts
    PRESENCE = "presence"  # 0/1


class WindowStrategy(StrEnum):
    INCREMENTAL = "incremental"  # request only periods after the latest stored (+overlap)
    FULL_WINDOW = "full_window"  # per-request scales: re-request a fixed window each time


class CollectorKind(StrEnum):
    SEARCH_INTEREST = "search_interest"  # Phase 1 SearchInterestProvider
    EVENT_SIGNALS = "event_signals"  # derived from stored Phase 2 discovery evidence
    NONE = "none"  # profile documented, collection not implemented/permitted


class Tristate(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class QuotaSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_provider_quota: bool = Field(
        default=False, description="Provider has its own QuotaManager (e.g. YouTube)"
    )
    daily_requests: int | None = Field(default=None, ge=0, description="Local request cap")
    reset_timezone: str = "UTC"
    safety_margin_pct: float = Field(default=10.0, ge=0, lt=100)
    request_cost: int = Field(default=1, ge=1)


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_name: str = ""
    enabled: bool = True
    collector: CollectorKind = CollectorKind.SEARCH_INTEREST
    synthetic: bool = False
    measurement_categories: tuple[MeasurementCategory, ...] = ()
    supports_youtube_search: Tristate = Tristate.UNKNOWN
    granularities: tuple[TimeGranularity, ...] = (TimeGranularity.DAY,)
    collection_granularity: TimeGranularity = TimeGranularity.DAY
    publication_cadence: timedelta | None = Field(
        default=timedelta(days=1), description="How often the source publishes new periods"
    )
    typical_delay: timedelta | None = Field(
        default=None, description="Declared delay from period end to publication"
    )
    historical_lookback: timedelta | None = Field(
        default=None, description="History available on a normal request"
    )
    max_historical_lookback: timedelta | None = None
    max_window_per_request: timedelta = timedelta(days=90)
    min_poll_interval: timedelta = timedelta(hours=1)
    rate_limit: str = "unknown"
    quota: QuotaSpec = QuotaSpec()
    revision_behaviour: RevisionBehaviour = RevisionBehaviour.UNKNOWN
    revision_window: timedelta = timedelta(days=3)
    sampling: str = "unknown"
    normalization: NormalizationBehaviour = NormalizationBehaviour.CONSISTENT_INDEX
    window_strategy: WindowStrategy = WindowStrategy.INCREMENTAL
    full_window: timedelta = timedelta(days=90)
    constant_values_expected: bool = False
    retention_policy: str
    collection_disabled_reason: str | None = None
    notes: str = ""

    @field_validator("supports_youtube_search", mode="before")
    @classmethod
    def _yaml_bool(cls, value: Any) -> Any:
        # YAML 1.1 reads unquoted yes/no as booleans.
        if isinstance(value, bool):
            return Tristate.YES if value else Tristate.NO
        return value

    @property
    def supports_history(self) -> bool:
        return bool(self.max_historical_lookback or self.historical_lookback)

    def describe(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        return {k: v for k, v in data.items() if v not in (None, "", [], ())}


class DelayEstimate(BaseModel):
    """Observed publication delay (period end -> availability). An estimate, not a promise."""

    model_config = ConfigDict(frozen=True)

    p50: timedelta | None
    p90: timedelta | None
    samples_exact: int  # publication time known
    samples_upper_bound: int  # only collection time known (true delay <= sample)
    basis: str  # publication_time | collection_upper_bound | declared | none
    observed_cadence: timedelta | None = None
    declared_delay: timedelta | None = None

    @property
    def expected(self) -> timedelta | None:
        return self.p50 if self.p50 is not None else self.declared_delay


class NextCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    next_at: datetime
    reason: str
    expected_period_end: datetime | None = None
    expected_available_at: datetime | None = None


class PlannedJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_id: int
    provider_name: str
    category: Category
    tier: MonitoringTier
    score: float
    pool: str  # exploitation | exploration
    estimated_cost: int
    quota_bucket: str | None
    reason: str
    expected_period_end: datetime | None = None


class CategoryUsage(BaseModel):
    reserved: int = 0
    exploration_reserved: int = 0
    used: int = 0
    exploration_used: int = 0
    redistributed_in: int = 0
    candidates: int = 0


class PlanReport(BaseModel):
    started_at: datetime
    dry_run: bool
    capacity: int
    due_targets: int = 0
    planned: list[PlannedJob] = Field(default_factory=list)
    jobs_created: int = 0
    jobs_already_queued: int = 0
    skipped_no_new_data: int = 0
    skipped_backoff: int = 0
    deferred_quota: int = 0
    provider_unavailable: int = 0
    categories: dict[str, CategoryUsage] = Field(default_factory=dict)
    estimated_quota: dict[str, dict[str, int]] = Field(default_factory=dict)
    quota_incidents: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
