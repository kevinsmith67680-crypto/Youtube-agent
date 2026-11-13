from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.helpers import FIXED_NOW
from trend_agent.comparability import (
    check_comparability,
    comparability_key,
    group_by_comparability,
)
from trend_agent.ingestion.validation import validate_batch, validate_candidate
from trend_agent.providers.mock import MockSearchInterestProvider
from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    MeasurementType,
    ProviderType,
    RejectionReason,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.observation import (
    NormalizationContext,
    ObservationCandidate,
    ValidatedObservation,
)
from trend_agent.schemas.provider import ProviderDescriptor

DAY = datetime(2026, 2, 20, tzinfo=UTC)
MOCK = MockSearchInterestProvider().descriptor

REAL = ProviderDescriptor(
    name="licensed_trends",
    display_name="Licensed",
    version="2.0",
    compatible_versions=frozenset({"1.9"}),
    provider_type=ProviderType.LICENSED_THIRD_PARTY,
    capabilities=frozenset({Capability.SEARCH_INTEREST}),
    measurement_types=frozenset(
        {MeasurementType.EXTERNAL_SEARCH_INTEREST, MeasurementType.DIRECT_SEARCH_INTEREST}
    ),
    search_properties=frozenset({SearchProperty.GOOGLE_WEB_SEARCH, SearchProperty.YOUTUBE_SEARCH}),
    supported_granularities=frozenset({TimeGranularity.DAY, TimeGranularity.WEEK}),
    requires_authentication=True,
    historical_data_available=True,
    data_nature=DataNature.SAMPLED,
    enabled=True,
)


def context(batch: str | None = "b1", **kw: Any) -> NormalizationContext:
    base: dict[str, Any] = {
        "scale_type": ScaleType.RELATIVE_PER_REQUEST,
        "method": "max_in_request_0_100",
        "batch_id": batch,
        "co_normalized_terms": ("ai agents",),
        "granularity": TimeGranularity.DAY,
        "search_property": SearchProperty.YOUTUBE_SEARCH,
    }
    base.update(kw)
    return NormalizationContext(**base)


def candidate(**overrides: Any) -> ObservationCandidate:
    base: dict[str, Any] = {
        "query": "AI Agents",
        "query_language": "en",
        "query_region": "gb",
        "provider_name": "mock",
        "provider_version": "1.0.0",
        "measurement_type": "direct_search_interest",
        "search_property": "youtube_search",
        "observation_start": DAY,
        "observation_end": DAY + timedelta(days=1),
        "granularity": "day",
        "collected_at": FIXED_NOW,
        "available_at": DAY + timedelta(days=2),
        "value": 42.0,
        "scale_type": "relative_per_request",
        "normalization_context": context(),
        "geography": "gb",
        "language": "en",
        "sample_quality": "complete",
        "data_nature": "synthetic",
        "source_reference": "synthetic://mock/test",
    }
    base.update(overrides)
    return ObservationCandidate(**base)


def reasons(result: object) -> set[RejectionReason]:
    assert isinstance(result, list), f"expected rejection, got {result!r}"
    return {issue.reason for issue in result}


def check(c: ObservationCandidate, descriptor: ProviderDescriptor = MOCK) -> object:
    return validate_candidate(c, descriptor, now=FIXED_NOW)


# ----------------------------------------------------------------------------- valid


def test_valid_observation_is_normalised() -> None:
    obs = check(candidate())
    assert isinstance(obs, ValidatedObservation)
    assert obs.canonical_query == "ai agents"
    assert obs.original_query == "AI Agents"
    assert obs.geography == "GB"
    assert obs.query_region == "GB"
    assert obs.is_synthetic
    assert len(obs.normalization_key) == 64
    assert obs.observation_start.tzinfo is UTC


def test_non_utc_timestamps_are_converted() -> None:
    from datetime import timezone

    tz = timezone(timedelta(hours=-5))
    obs = check(
        candidate(
            observation_start=DAY.astimezone(tz),
            observation_end=(DAY + timedelta(days=1)).astimezone(tz),
        )
    )
    assert isinstance(obs, ValidatedObservation)
    assert obs.observation_start == DAY
    assert obs.observation_start.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("quality", "value"),
    [
        ("suppressed", None),
        ("missing", None),
        ("low_volume", None),
        ("low_volume", 0.0),
        ("complete", 0.0),
    ],
)
def test_low_missing_and_suppressed_values_are_valid(quality: str, value: float | None) -> None:
    assert isinstance(check(candidate(sample_quality=quality, value=value)), ValidatedObservation)


def test_partial_period_allowed_when_labelled() -> None:
    open_window = candidate(
        observation_start=FIXED_NOW - timedelta(hours=12),
        observation_end=FIXED_NOW + timedelta(hours=12),
        available_at=FIXED_NOW,
        sample_quality="partial_period",
    )
    assert isinstance(check(open_window), ValidatedObservation)
    assert RejectionReason.INVALID_WINDOW in reasons(
        check(open_window.model_copy(update={"sample_quality": "complete"}))
    )


# ----------------------------------------------------------------------------- invalid


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"provider_name": None}, RejectionReason.MISSING_PROVENANCE),
        ({"provider_version": ""}, RejectionReason.MISSING_PROVENANCE),
        ({"source_reference": None}, RejectionReason.MISSING_PROVENANCE),
        ({"source_reference": "   "}, RejectionReason.MISSING_PROVENANCE),
        ({"collected_at": None}, RejectionReason.MISSING_PROVENANCE),
        ({"provider_name": "other"}, RejectionReason.PROVIDER_MISMATCH),
        ({"provider_version": "9.9.9"}, RejectionReason.UNRECOGNIZED_PROVIDER_VERSION),
        ({"observation_start": None}, RejectionReason.INVALID_TIMESTAMP),
        ({"observation_start": datetime(2026, 2, 20)}, RejectionReason.INVALID_TIMESTAMP),
        ({"collected_at": FIXED_NOW + timedelta(hours=1)}, RejectionReason.INVALID_TIMESTAMP),
        ({"available_at": FIXED_NOW + timedelta(days=1)}, RejectionReason.INVALID_TIMESTAMP),
        ({"available_at": DAY - timedelta(days=3)}, RejectionReason.INVALID_TIMESTAMP),
        (
            {
                "observation_start": datetime(2001, 1, 1, tzinfo=UTC),
                "observation_end": datetime(2001, 1, 2, tzinfo=UTC),
            },
            RejectionReason.INVALID_TIMESTAMP,
        ),
        ({"observation_end": DAY - timedelta(days=1)}, RejectionReason.INVALID_WINDOW),
        ({"observation_end": DAY + timedelta(days=2)}, RejectionReason.GRANULARITY_MISMATCH),
        ({"granularity": "fortnight"}, RejectionReason.INVALID_ENUM_VALUE),
        ({"value": math.nan}, RejectionReason.NON_FINITE_VALUE),
        ({"value": math.inf}, RejectionReason.NON_FINITE_VALUE),
        ({"value": 100.5}, RejectionReason.VALUE_OUT_OF_RANGE),
        ({"value": -1.0}, RejectionReason.VALUE_OUT_OF_RANGE),
        (
            {"value": None, "sample_quality": "complete"},
            RejectionReason.INCONSISTENT_SAMPLE_QUALITY,
        ),
        (
            {"value": 5.0, "sample_quality": "suppressed"},
            RejectionReason.INCONSISTENT_SAMPLE_QUALITY,
        ),
        ({"geography": "UK"}, RejectionReason.INVALID_REGION),
        ({"geography": None}, RejectionReason.INVALID_REGION),
        ({"query_region": "XX"}, RejectionReason.INVALID_REGION),
        ({"language": "english"}, RejectionReason.INVALID_LANGUAGE),
        ({"query": "   "}, RejectionReason.INVALID_QUERY),
        ({"measurement_type": "search_volume"}, RejectionReason.UNSUPPORTED_MEASUREMENT_TYPE),
        ({"measurement_type": "video_activity"}, RejectionReason.UNSUPPORTED_MEASUREMENT_TYPE),
        ({"search_property": "google_web_search"}, RejectionReason.MISLABELED_SEARCH_PROPERTY),
        ({"scale_type": "absolute_count"}, RejectionReason.INVALID_SCALE),
        ({"normalization_context": None}, RejectionReason.MISSING_NORMALIZATION_CONTEXT),
        (
            {"normalization_context": context(batch=None)},
            RejectionReason.MISSING_NORMALIZATION_CONTEXT,
        ),
        (
            {"normalization_context": context(granularity=TimeGranularity.WEEK)},
            RejectionReason.INVALID_SCALE,
        ),
        ({"data_nature": "actual"}, RejectionReason.SYNTHETIC_LABEL_MISMATCH),
        ({"source_reference": "https://real.example"}, RejectionReason.SYNTHETIC_LABEL_MISMATCH),
        ({"data_nature": "unavailable"}, RejectionReason.INVALID_ENUM_VALUE),
    ],
)
def test_invalid_candidates_are_rejected(
    overrides: dict[str, Any], expected: RejectionReason
) -> None:
    assert expected in reasons(check(candidate(**overrides)))


def test_all_issues_are_reported_not_just_the_first() -> None:
    found = reasons(check(candidate(value=math.nan, geography="ZZ", source_reference=None)))
    assert {
        RejectionReason.NON_FINITE_VALUE,
        RejectionReason.INVALID_REGION,
        RejectionReason.MISSING_PROVENANCE,
    } <= found


def test_external_interest_can_never_be_labelled_youtube() -> None:
    web = candidate(
        provider_name="licensed_trends",
        provider_version="2.0",
        measurement_type="direct_search_interest",
        search_property="google_web_search",
        data_nature="sampled",
        source_reference="https://licensed.example/r/1",
        normalization_context=context(search_property=SearchProperty.GOOGLE_WEB_SEARCH),
    )
    assert RejectionReason.MISLABELED_SEARCH_PROPERTY in reasons(check(web, REAL))
    youtube_as_external = web.model_copy(
        update={
            "measurement_type": "external_search_interest",
            "search_property": "youtube_search",
            "normalization_context": context(),
        }
    )
    assert RejectionReason.MISLABELED_SEARCH_PROPERTY in reasons(check(youtube_as_external, REAL))
    correct = web.model_copy(update={"measurement_type": "external_search_interest"})
    assert isinstance(check(correct, REAL), ValidatedObservation)


def test_real_provider_cannot_emit_synthetic_and_compatible_versions_accepted() -> None:
    real = candidate(
        provider_name="licensed_trends",
        provider_version="1.9",
        data_nature="sampled",
        source_reference="https://licensed.example/r/1",
    )
    assert isinstance(check(real, REAL), ValidatedObservation)
    fake = real.model_copy(update={"data_nature": "synthetic"})
    assert RejectionReason.SYNTHETIC_LABEL_MISMATCH in reasons(check(fake, REAL))


# ----------------------------------------------------------------------------- batches


def test_batch_deduplicates_identical_and_rejects_conflicts() -> None:
    a = candidate()
    second_day = candidate(
        observation_start=DAY + timedelta(days=1), observation_end=DAY + timedelta(days=2)
    )
    conflicting = second_day.model_copy(update={"value": 99.0})
    bad = candidate(value=math.nan)
    report = validate_batch(
        [a, a.model_copy(), a.model_copy(), second_day, conflicting, bad], MOCK, now=FIXED_NOW
    )
    assert report.received == 6
    assert len(report.accepted) == 1
    assert report.duplicates_in_batch == 2
    assert len(report.rejected) == 3
    assert report.rejected_by_reason() == {"duplicate_conflict": 2, "non_finite_value": 1}
    assert report.received == (
        len(report.accepted) + report.duplicates_in_batch + len(report.rejected)
    )


def test_query_variants_collapse_to_one_identity() -> None:
    report = validate_batch(
        [candidate(query="AI agents"), candidate(query="  ai   AGENTS ")], MOCK, now=FIXED_NOW
    )
    assert len(report.accepted) == 1
    assert report.duplicates_in_batch == 1


# ----------------------------------------------------------------------------- comparability


def valid(**overrides: Any) -> ValidatedObservation:
    obs = check(candidate(**overrides))
    assert isinstance(obs, ValidatedObservation)
    return obs


def next_day() -> dict[str, Any]:
    return {
        "observation_start": DAY + timedelta(days=1),
        "observation_end": DAY + timedelta(days=2),
    }


def test_same_batch_is_comparable() -> None:
    result = check_comparability(valid(), valid(**next_day(), value=60.0))
    assert result.comparable
    assert result.reasons == ()
    assert bool(result)


def test_different_normalisation_batches_are_not_comparable() -> None:
    result = check_comparability(valid(), valid(**next_day(), normalization_context=context("b2")))
    assert not result.comparable
    assert any("different batches" in r for r in result.reasons)


def test_synthetic_vs_real_and_property_mismatch() -> None:
    real = check(
        candidate(
            provider_name="licensed_trends",
            provider_version="2.0",
            measurement_type="external_search_interest",
            search_property="google_web_search",
            data_nature="sampled",
            source_reference="https://licensed.example/1",
            normalization_context=context(search_property=SearchProperty.GOOGLE_WEB_SEARCH),
        ),
        REAL,
    )
    assert isinstance(real, ValidatedObservation)
    result = check_comparability(valid(), real)
    joined = " ".join(result.reasons)
    assert "different measurement types" in joined
    assert "YouTube vs web" in joined
    assert "synthetic vs real" in joined


@pytest.mark.parametrize(
    "overrides",
    [
        {"sample_quality": "suppressed", "value": None},
        {"sample_quality": "low_volume", "value": 1.0},
        {"geography": "US"},
        {"language": "de"},
    ],
)
def test_quality_and_context_mismatches_block_comparison(overrides: dict[str, Any]) -> None:
    assert not check_comparability(valid(), valid(**next_day(), **overrides)).comparable


def test_consistent_index_is_comparable_across_requests_only_with_same_version() -> None:
    def ci(batch: str, version: str, **kw: Any) -> ValidatedObservation:
        return valid(
            scale_type="consistent_index",
            normalization_context=context(
                batch, scale_type=ScaleType.CONSISTENT_INDEX, method="ci", scale_version=version
            ),
            **kw,
        )

    assert check_comparability(ci("r1", "1"), ci("r2", "1", **next_day())).comparable
    assert not check_comparability(ci("r1", "1"), ci("r2", "2", **next_day())).comparable


def test_grouping_by_comparability() -> None:
    a = valid()
    b = valid(**next_day())
    c = valid(normalization_context=context("other"))
    groups = group_by_comparability([b, c, a])
    assert len(groups) == 2
    assert groups[comparability_key(a)] == [a, b]  # sorted by window
    assert comparability_key(a) != comparability_key(c)


def test_normalisation_key_is_order_independent() -> None:
    one = context(co_normalized_terms=("b", "a"))
    two = context(co_normalized_terms=("a", "b"))
    assert one.key() == two.key()
    assert context(batch="x").key() != context(batch="y").key()


def test_value_matches_quality_helper_constants() -> None:
    assert SampleQuality.COMPLETE.value == "complete"
