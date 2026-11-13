"""Domain enumerations.

These enums are the single source of truth for the semantic labels attached to every
record. They are persisted as their string values (see ``db/models.py``) and enforced with
CHECK constraints, so a value outside these sets cannot be stored.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum


class MeasurementType(StrEnum):
    """What an observation actually measures.

    DIRECT_SEARCH_INTEREST
        A measured or provider-supplied relative search-interest signal specifically
        attributable to YouTube Search. Requires ``SearchProperty.YOUTUBE_SEARCH``.
    SEARCH_SUGGESTION
        A suggested search phrase. A discovery signal, *not* evidence of search volume.
    VIDEO_ACTIVITY
        Video metadata, upload activity or engagement statistics: supply/attention
        indicators, not search demand.
    EXTERNAL_SEARCH_INTEREST
        Search interest from Google Web Search or another non-YouTube property. Must never
        be relabelled as YouTube demand.
    UNKNOWN
        Origin or meaning cannot be established. Never used as a demand signal.
    """

    DIRECT_SEARCH_INTEREST = "direct_search_interest"
    SEARCH_SUGGESTION = "search_suggestion"
    VIDEO_ACTIVITY = "video_activity"
    EXTERNAL_SEARCH_INTEREST = "external_search_interest"
    UNKNOWN = "unknown"


class SearchProperty(StrEnum):
    """The search surface an observation refers to."""

    YOUTUBE_SEARCH = "youtube_search"
    GOOGLE_WEB_SEARCH = "google_web_search"
    GOOGLE_NEWS_SEARCH = "google_news_search"
    GOOGLE_IMAGE_SEARCH = "google_image_search"
    GOOGLE_SHOPPING = "google_shopping"
    OTHER_EXTERNAL = "other_external"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ScaleType(StrEnum):
    """How an observation's numeric value is scaled.

    RELATIVE_PER_REQUEST
        0-100, normalised to the maximum within one request (Google Trends website style).
        Only comparable with values from the *same* normalisation batch.
    CONSISTENT_INDEX
        A provider-defined index that the provider states is consistently scaled across
        requests. Comparable across requests only under the same scale version, property,
        geography and granularity.
    ABSOLUTE_COUNT
        A measured count of events.
    ESTIMATED_COUNT
        A modelled estimate of a count; comparable only within one estimation model.
    RANK
        Ordinal position (1 = first). Not a magnitude.
    PRESENCE
        0/1 indicator that something was observed.
    """

    RELATIVE_PER_REQUEST = "relative_per_request"
    CONSISTENT_INDEX = "consistent_index"
    ABSOLUTE_COUNT = "absolute_count"
    ESTIMATED_COUNT = "estimated_count"
    RANK = "rank"
    PRESENCE = "presence"


SCALE_BOUNDS: dict[ScaleType, tuple[float, float | None, bool]] = {
    # scale: (minimum, maximum or None, integer_only)
    ScaleType.RELATIVE_PER_REQUEST: (0.0, 100.0, False),
    ScaleType.CONSISTENT_INDEX: (0.0, None, False),
    ScaleType.ABSOLUTE_COUNT: (0.0, None, True),
    ScaleType.ESTIMATED_COUNT: (0.0, None, False),
    ScaleType.RANK: (1.0, None, True),
    ScaleType.PRESENCE: (0.0, 1.0, True),
}


class SampleQuality(StrEnum):
    """Completeness / reliability of a single observation.

    COMPLETE        Value present and the window is fully covered.
    PARTIAL_PERIOD  Value present but the window was still in progress at collection.
    LOW_VOLUME      Provider flagged the value as low volume / below reliability threshold.
                    A value may or may not be present. Not equivalent to zero demand.
    SUPPRESSED      Provider withheld the value (e.g. privacy threshold). Value is NULL.
    MISSING         No data was returned for this window. Value is NULL.
    """

    COMPLETE = "complete"
    PARTIAL_PERIOD = "partial_period"
    LOW_VOLUME = "low_volume"
    SUPPRESSED = "suppressed"
    MISSING = "missing"


VALUE_REQUIRED_QUALITIES = frozenset({SampleQuality.COMPLETE, SampleQuality.PARTIAL_PERIOD})
VALUE_FORBIDDEN_QUALITIES = frozenset({SampleQuality.SUPPRESSED, SampleQuality.MISSING})


class DataNature(StrEnum):
    """Whether values are measured, sampled, estimated, synthetic or unavailable."""

    ACTUAL = "actual"
    SAMPLED = "sampled"
    ESTIMATED = "estimated"
    SYNTHETIC = "synthetic"
    UNAVAILABLE = "unavailable"  # provider-level declaration only; never on an observation


class TimeGranularity(StrEnum):
    POINT = "point"  # instantaneous snapshot (start == end)
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"

    def allowed_durations(self) -> tuple[timedelta, timedelta]:
        """Inclusive (min, max) window duration for this granularity.

        Day/week/month/year windows may be expressed in a local time zone and converted
        to UTC, so a one-hour DST tolerance is permitted.
        """
        hour = timedelta(hours=1)
        match self:
            case TimeGranularity.POINT:
                return timedelta(0), timedelta(0)
            case TimeGranularity.HOUR:
                return hour, hour
            case TimeGranularity.DAY:
                return timedelta(days=1) - hour, timedelta(days=1) + hour
            case TimeGranularity.WEEK:
                return timedelta(days=7) - hour, timedelta(days=7) + hour
            case TimeGranularity.MONTH:
                return timedelta(days=28) - hour, timedelta(days=31) + hour
            case TimeGranularity.YEAR:
                return timedelta(days=365) - hour, timedelta(days=366) + hour


class Capability(StrEnum):
    SEARCH_INTEREST = "search_interest"
    SEARCH_SUGGESTIONS = "search_suggestions"
    VIDEO_SEARCH = "video_search"
    VIDEO_STATISTICS = "video_statistics"
    CHANNEL_METADATA = "channel_metadata"
    TREND_DISCOVERY = "trend_discovery"


class ProviderStatus(StrEnum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    ACCESS_RESTRICTED = "access_restricted"
    UNSUPPORTED = "unsupported"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    DISABLED = "disabled"


class ProviderType(StrEnum):
    OFFICIAL_API = "official_api"
    LICENSED_THIRD_PARTY = "licensed_third_party"
    SYNTHETIC = "synthetic"


class IngestionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # some fetch units failed while others succeeded
    FAILED = "failed"
    CANCELLED = "cancelled"


class RejectionReason(StrEnum):
    MISSING_PROVENANCE = "missing_provenance"
    PROVIDER_MISMATCH = "provider_mismatch"
    UNRECOGNIZED_PROVIDER_VERSION = "unrecognized_provider_version"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_WINDOW = "invalid_window"
    GRANULARITY_MISMATCH = "granularity_mismatch"
    NON_FINITE_VALUE = "non_finite_value"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    INCONSISTENT_SAMPLE_QUALITY = "inconsistent_sample_quality"
    INVALID_REGION = "invalid_region"
    INVALID_LANGUAGE = "invalid_language"
    INVALID_QUERY = "invalid_query"
    UNSUPPORTED_MEASUREMENT_TYPE = "unsupported_measurement_type"
    MISLABELED_SEARCH_PROPERTY = "mislabeled_search_property"
    INVALID_SCALE = "invalid_scale"
    MISSING_NORMALIZATION_CONTEXT = "missing_normalization_context"
    SYNTHETIC_LABEL_MISMATCH = "synthetic_label_mismatch"
    INVALID_ENUM_VALUE = "invalid_enum_value"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    CONFLICTS_WITH_STORED = "conflicts_with_stored"
    MALFORMED_PROVIDER_ITEM = "malformed_provider_item"


class RecordKind(StrEnum):
    SEARCH_INTEREST_OBSERVATION = "search_interest_observation"
    VIDEO = "video"
    VIDEO_STATISTICS = "video_statistics"
    CHANNEL = "channel"
    VIDEO_SEARCH_RESULT = "video_search_result"
    DISCOVERY_ITEM = "discovery_item"  # Phase 2: source material behind keyword candidates
