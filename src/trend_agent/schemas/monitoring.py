"""Phase 3 monitoring vocabulary: targets, tiers, freshness, readiness, comparability, jobs.

Every label here is operational. A ``CRITICAL`` tier means "watch closely because the
subject is new and time-sensitive", never "this query is surging"; a ``READY`` dataset
means "enough comparable history exists for later analysis", never "demand is rising".
"""

from __future__ import annotations

from enum import StrEnum

from trend_agent.schemas.enums import MeasurementType, SearchProperty


class MeasurementCategory(StrEnum):
    """What a time-series observation measures (Phase 3 canonical categories).

    DIRECT_YOUTUBE_SEARCH_INTEREST  provider-supplied interest attributable to YouTube Search.
    EXTERNAL_SEARCH_INTEREST        search interest on another property (e.g. Google Web).
    SEARCH_SUGGESTION_PRESENCE      the phrase appeared in a supported suggestion system.
    VIDEO_ACTIVITY                  video metadata/statistics (supply/attention, not demand).
    OFFICIAL_EVENT_SIGNAL           timestamped official release/announcement/patch/event.
    COMMUNITY_ACTIVITY              timestamped community attention from authorised APIs.
    SYNTHETIC_TEST_OBSERVATION      artificial data for tests and demonstrations only.
    """

    DIRECT_YOUTUBE_SEARCH_INTEREST = "direct_youtube_search_interest"
    EXTERNAL_SEARCH_INTEREST = "external_search_interest"
    SEARCH_SUGGESTION_PRESENCE = "search_suggestion_presence"
    VIDEO_ACTIVITY = "video_activity"
    OFFICIAL_EVENT_SIGNAL = "official_event_signal"
    COMMUNITY_ACTIVITY = "community_activity"
    SYNTHETIC_TEST_OBSERVATION = "synthetic_test_observation"


SEARCH_INTEREST_CATEGORIES = frozenset(
    {
        MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        MeasurementCategory.EXTERNAL_SEARCH_INTEREST,
    }
)
PROXY_CATEGORIES = frozenset(
    {
        MeasurementCategory.SEARCH_SUGGESTION_PRESENCE,
        MeasurementCategory.VIDEO_ACTIVITY,
        MeasurementCategory.OFFICIAL_EVENT_SIGNAL,
        MeasurementCategory.COMMUNITY_ACTIVITY,
    }
)


def category_for_measurement(
    measurement: MeasurementType, search_property: SearchProperty
) -> MeasurementCategory | None:
    """Map a Phase 1 measurement type to a Phase 3 category (None: not a time series)."""
    if measurement is MeasurementType.DIRECT_SEARCH_INTEREST:
        return MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST
    if measurement is MeasurementType.EXTERNAL_SEARCH_INTEREST:
        return MeasurementCategory.EXTERNAL_SEARCH_INTEREST
    if measurement is MeasurementType.VIDEO_ACTIVITY:
        return MeasurementCategory.VIDEO_ACTIVITY
    if measurement is MeasurementType.SEARCH_SUGGESTION:
        return MeasurementCategory.SEARCH_SUGGESTION_PRESENCE
    return None


def search_property_for(category: MeasurementCategory) -> SearchProperty:
    if category is MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST:
        return SearchProperty.YOUTUBE_SEARCH
    if category is MeasurementCategory.EXTERNAL_SEARCH_INTEREST:
        return SearchProperty.GOOGLE_WEB_SEARCH
    return SearchProperty.NOT_APPLICABLE


class MonitoringStatus(StrEnum):
    PENDING = "pending"  # selected, awaiting activation (capacity, provider, budget)
    ACTIVE = "active"
    PAUSED = "paused"  # manual
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # no usable provider for the measurement
    QUOTA_BLOCKED = "quota_blocked"  # deferred until the provider's quota resets
    STALE = "stale"  # repeated collection failures; data falling behind
    DORMANT = "dormant"  # low monitoring value; history kept
    DISABLED = "disabled"  # excluded (manual) or keyword rejected


COLLECTABLE_STATUSES = frozenset(
    {MonitoringStatus.ACTIVE, MonitoringStatus.STALE, MonitoringStatus.QUOTA_BLOCKED}
)


class MonitoringTier(StrEnum):
    """Operational importance, NOT confirmed search growth."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    DORMANT = "dormant"


TIER_ORDER = [
    MonitoringTier.CRITICAL,
    MonitoringTier.HIGH,
    MonitoringTier.NORMAL,
    MonitoringTier.LOW,
    MonitoringTier.DORMANT,
]


class FreshnessStatus(StrEnum):
    SOURCE_CURRENT = "source_current"  # we hold the latest period the source should have
    PROVIDER_DELAYED = "provider_delayed"  # checked after expected availability; not out yet
    COLLECTION_DELAYED = "collection_delayed"  # data should be out but we have not checked
    NO_NEW_OBSERVATION = "no_new_observation"  # last check found nothing new (within norms)
    SOURCE_UNAVAILABLE = "source_unavailable"  # provider not configured/unhealthy
    UNKNOWN_FRESHNESS = "unknown_freshness"  # no data / no cadence information yet


class ReadinessState(StrEnum):
    READY = "ready"
    PARTIALLY_READY = "partially_ready"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOMPATIBLE_HISTORY = "incompatible_history"
    STALE_HISTORY = "stale_history"
    SOURCE_UNAVAILABLE = "source_unavailable"


class ComparabilityStatus(StrEnum):
    COMPARABLE = "comparable"
    COMPARABLE_WITH_CAVEATS = "comparable_with_caveats"
    INCOMPATIBLE = "incompatible"
    INSUFFICIENT_METADATA = "insufficient_metadata"


class ComparabilityReason(StrEnum):
    """Machine-readable reasons attached to every comparability decision."""

    DIFFERENT_PROVIDER = "different_provider"
    DIFFERENT_MEASUREMENT = "different_measurement_category"
    DIFFERENT_SEARCH_PROPERTY = "different_search_property"
    DIFFERENT_REGION = "different_region"
    DIFFERENT_LANGUAGE = "different_language"
    DIFFERENT_GRANULARITY = "different_granularity"
    DIFFERENT_SCALE_TYPE = "different_scale_type"
    DIFFERENT_SCALE_DEFINITION = "different_scale_definition"
    DIFFERENT_NORMALIZATION_CONTEXT = "different_normalization_context"
    DIFFERENT_UNIT = "different_unit"
    SYNTHETIC_VS_REAL = "synthetic_vs_real"
    PROXY_VS_SEARCH_INTEREST = "proxy_vs_search_interest"
    MISSING_NORMALIZATION_CONTEXT = "missing_normalization_context"
    MISSING_BATCH_ID = "missing_batch_id"
    MISSING_GRANULARITY = "missing_granularity"
    NO_VALUE = "no_value"
    PARTIAL_PERIOD = "partial_period"
    LOW_VOLUME = "low_volume"
    REVISED_VALUE = "revised_value"
    PROVIDER_VERSION_CHANGED = "provider_version_changed"
    CONFLICTING_VALUE = "conflicting_value"


class JobStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # new or revised observations stored
    NO_NEW_DATA = "no_new_data"  # request succeeded; nothing new (not a data refresh)
    DEFERRED = "deferred"  # waiting for quota reset / provider recovery
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


CLAIMABLE_JOB_STATUSES = frozenset({JobStatus.PENDING, JobStatus.RETRY_WAIT, JobStatus.DEFERRED})
LEASED_JOB_STATUSES = frozenset({JobStatus.CLAIMED, JobStatus.RUNNING})
FINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.NO_NEW_DATA, JobStatus.FAILED, JobStatus.CANCELLED}
)


class JobType(StrEnum):
    SCHEDULED_COLLECTION = "scheduled_collection"
    MANUAL_COLLECTION = "manual_collection"
    BACKFILL = "backfill"


class ErrorCategory(StrEnum):
    """Job failure classification; decides retry behaviour."""

    TRANSIENT = "transient"  # network, 5xx, timeout: retry with backoff
    RATE_LIMITED = "rate_limited"  # retry after provider's Retry-After
    QUOTA = "quota"  # defer until quota reset
    AUTHORIZATION = "authorization"  # invalid credentials / access restricted: permanent
    CAPABILITY = "capability"  # unsupported request / not configured: permanent
    VALIDATION = "validation"  # invalid request or response: permanent
    LEASE_EXPIRED = "lease_expired"  # worker vanished; re-run (idempotent writes)
    DB_WRITE = "db_write"  # storage failure: retry
    INTERNAL = "internal"  # unexpected bug: retry a bounded number of times


class RevisionKind(StrEnum):
    INITIAL = "initial"
    REVISION = "revision"  # provider republished a different value (lineage kept)
    CONFLICT = "conflict"  # contradicts an immutable value; stored, not current


class WriteOutcome(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"  # duplicate fetch of an identical observation
    REVISED = "revised"
    CONFLICTING = "conflicting"


class QualityStatus(StrEnum):
    """Per-observation quality label."""

    VALID = "valid"
    CAVEAT = "caveat"  # partial period, low volume, unknown publication time, ...
    CONFLICTING = "conflicting"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class QualityIssue(StrEnum):
    MISSING_PERIODS = "missing_periods"
    MISSING_VALUES = "missing_values"  # provider returned missing/suppressed (not zero)
    LOW_VOLUME_VALUES = "low_volume_values"  # data property, not a source failure
    DUPLICATE_RECORDS = "duplicate_records"
    OUT_OF_ORDER_ARRIVAL = "out_of_order_arrival"
    UNEXPECTED_GAP = "unexpected_gap"
    STALE_SERIES = "stale_series"
    STALE_PROVIDER = "stale_provider"
    INVALID_TIMESTAMPS = "invalid_timestamps"
    INCOMPATIBLE_SCALES = "incompatible_scales"
    NORMALIZATION_CHANGED = "normalization_changed"
    PROVIDER_VERSION_CHANGED = "provider_version_changed"
    SCHEMA_CHANGE_SUSPECTED = "schema_change_suspected"
    CONSTANT_VALUES = "constant_values"
    FREQUENT_REVISIONS = "frequent_revisions"
    CONFLICTING_VALUES = "conflicting_values"
    MISSING_PROVENANCE = "missing_provenance"
    AUTHENTICATION_FAILURES = "authentication_failures"
    PROVIDER_ERRORS = "provider_errors"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
