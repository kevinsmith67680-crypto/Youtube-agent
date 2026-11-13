"""Comparability, readiness, quality, replay, synthetic provider, collector, observation model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from tests.helpers import make_settings
from tests.unit.monitoring.helpers import CONFIG, DAY, KEY_A, KEY_B, MONDAY, point, series
from trend_agent.monitoring.collectors import (
    SearchInterestCollector,
    TargetSnapshot,
    local_quota,
)
from trend_agent.monitoring.comparability import assess_pair, assess_series, comparable_groups
from trend_agent.monitoring.planner import window_for
from trend_agent.monitoring.policies import PolicyRegistry
from trend_agent.monitoring.quality import AttemptStats, QualityReport, check_series
from trend_agent.monitoring.readiness import SeriesReadiness, combine_target, evaluate_series
from trend_agent.monitoring.replay import (
    AvailabilityMode,
    CollectingConsumer,
    availability,
    replay,
)
from trend_agent.monitoring.synthetic import SyntheticTrendsProvider, load_monitoring_scenario
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import QuotaExhaustedError, TemporarilyUnavailableError
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaLedger
from trend_agent.schemas.enums import (
    DataNature,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.monitoring import (
    ComparabilityReason,
    ComparabilityStatus,
    FreshnessStatus,
    MeasurementCategory,
    QualityIssue,
    QualityStatus,
    ReadinessState,
    Severity,
)
from trend_agent.schemas.observation import NormalizationContext, ObservationCandidate
from trend_agent.schemas.provider import SearchInterestRequest
from trend_agent.schemas.time_series import ObservationDraft, StoredPoint

R = ComparabilityReason
C = ComparabilityStatus


# ---------------------------------------------------------------------------- comparability


def test_same_series_is_comparable() -> None:
    a, b = point(0), point(1)
    assert assess_pair(a, b).status is C.COMPARABLE


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"provider_name": "other"}, R.DIFFERENT_PROVIDER),
        ({"region": "GB"}, R.DIFFERENT_REGION),
        ({"language": "de"}, R.DIFFERENT_LANGUAGE),
        (
            {"measurement_category": MeasurementCategory.EXTERNAL_SEARCH_INTEREST},
            R.DIFFERENT_MEASUREMENT,
        ),
        ({"granularity": TimeGranularity.WEEK}, R.DIFFERENT_GRANULARITY),
        ({"unit": "count"}, R.DIFFERENT_UNIT),
        ({"scale_type": ScaleType.ABSOLUTE_COUNT}, R.DIFFERENT_SCALE_TYPE),
        ({"normalization_key": KEY_B}, R.DIFFERENT_SCALE_DEFINITION),
        ({"is_synthetic": True}, R.SYNTHETIC_VS_REAL),
        ({"measurement_category": MeasurementCategory.VIDEO_ACTIVITY}, R.PROXY_VS_SEARCH_INTEREST),
    ],
)
def test_incompatible_measurements(change: dict[str, Any], reason: ComparabilityReason) -> None:
    verdict = assess_pair(point(0), point(1, **change))
    assert verdict.status is C.INCOMPATIBLE
    assert reason in verdict.reasons


def test_independently_normalised_series_are_never_combined() -> None:
    a = point(0, scale_type=ScaleType.RELATIVE_PER_REQUEST, normalization_key=KEY_A)
    b = point(1, scale_type=ScaleType.RELATIVE_PER_REQUEST, normalization_key=KEY_B)
    verdict = assess_pair(a, b)
    assert verdict.status is C.INCOMPATIBLE
    assert verdict.reasons == (R.DIFFERENT_NORMALIZATION_CONTEXT,)
    groups = comparable_groups([a, b, point(2, scale_type=ScaleType.RELATIVE_PER_REQUEST)])
    assert sorted(g.size for g in groups) == [1, 2]
    missing = assess_pair(a, b, contexts={KEY_A: {"batch_id": None}, KEY_B: {"batch_id": "x"}})
    assert missing.status is C.INSUFFICIENT_METADATA
    assert R.MISSING_BATCH_ID in missing.reasons


def test_caveats_and_missing_values() -> None:
    partial = point(1, sample_quality=SampleQuality.PARTIAL_PERIOD)
    assert assess_pair(point(0), partial).status is C.COMPARABLE_WITH_CAVEATS
    revised = point(1, revision=2, revision_kind="revision")
    assert R.REVISED_VALUE in assess_pair(point(0), revised).reasons
    version = point(1, provider_version="2")
    assert R.PROVIDER_VERSION_CHANGED in assess_pair(point(0), version).reasons
    missing = assess_pair(point(0), point(1, value=None))
    assert missing.status is C.INCOMPATIBLE  # a missing value is not zero
    assert missing.reasons == (R.NO_VALUE,)
    assert assess_series(series(5)).status is C.COMPARABLE


# ---------------------------------------------------------------------------- readiness


PROFILE = CONFIG.readiness.profiles["short_term"]


def ready(points: list[StoredPoint], **kw: object) -> SeriesReadiness:
    base: dict[str, object] = {
        "series_id": 1,
        "profile_name": "short_term",
        "profile": PROFILE,
        "now": MONDAY + DAY * 21,
        "freshness": FreshnessStatus.SOURCE_CURRENT,
        "provider_available": True,
    }
    base.update(kw)
    return evaluate_series(points, **base)  # type: ignore[arg-type]


def test_readiness_states_and_reasons() -> None:
    r = ready(series(20))
    assert r.state is ReadinessState.READY
    assert r.coverage == 1.0
    few = ready(series(5))
    assert few.state is ReadinessState.INSUFFICIENT_HISTORY
    gappy = [p for i, p in enumerate(series(20)) if i % 3 != 1]
    g = ready(gappy)
    assert g.state is ReadinessState.PARTIALLY_READY
    assert g.missing_periods == 6
    assert any("coverage" in reason for reason in g.reasons)
    stale = ready(series(20), now=MONDAY + DAY * 40, freshness=FreshnessStatus.PROVIDER_DELAYED)
    assert stale.state is ReadinessState.STALE_HISTORY
    weekly = ready(series(20, granularity=TimeGranularity.WEEK))
    assert weekly.state is ReadinessState.INCOMPATIBLE_HISTORY
    empty = ready([], provider_available=False)
    assert empty.state is ReadinessState.SOURCE_UNAVAILABLE
    nulls = ready([point(i, value=None if i % 2 else 50.0) for i in range(20)])
    assert nulls.missing_values == 10  # missing values counted, never treated as zero
    assert nulls.state is not ReadinessState.READY
    proxy = ready(series(20, measurement_category=MeasurementCategory.OFFICIAL_EVENT_SIGNAL))
    assert proxy.usable_for_search_demand is False


def test_split_history_is_incompatible_not_combined() -> None:
    a = ready(series(8, normalization_key=KEY_A), series_id=1)
    b = ready(series(8, normalization_key=KEY_B), series_id=2)
    combined = combine_target(1, "q", "p", [a, b], PROFILE, True)
    assert combined.state is ReadinessState.INCOMPATIBLE_HISTORY
    assert "not combined" in combined.reasons[0]


# ---------------------------------------------------------------------------- quality


def quality(points: list[StoredPoint], **kw: object) -> QualityReport:
    base: dict[str, object] = {
        "series_id": 1,
        "profile": CONFIG.providers["synthetic_trends"],
        "thresholds": CONFIG.quality,
        "freshness": None,
        "arrival": [],
        "provider_versions": ["1"],
        "attempts": AttemptStats(),
    }
    base.update(kw)
    return check_series(points, **base)  # type: ignore[arg-type]


def test_quality_findings() -> None:
    clean = quality(series(10))
    assert clean.status.value == "healthy"
    gappy = quality([p for i, p in enumerate(series(12)) if i not in (3, 4, 5, 6)])
    issues = {f.issue for f in gappy.findings}
    assert {QualityIssue.MISSING_PERIODS, QualityIssue.UNEXPECTED_GAP} <= issues
    constant = [point(i, 50.0) for i in range(20)]
    assert QualityIssue.CONSTANT_VALUES in {f.issue for f in quality(constant).findings}
    expected_constant = quality(constant, profile=CONFIG.providers["discovery_evidence"])
    assert QualityIssue.CONSTANT_VALUES not in {f.issue for f in expected_constant.findings}
    revs = series(5) + [point(i, 60.0, revision=2, revision_kind="revision") for i in range(3)]
    assert QualityIssue.FREQUENT_REVISIONS in {f.issue for f in quality(revs).findings}
    auth = quality(series(5), attempts=AttemptStats(attempts=3, failures=3, auth_failures=3))
    assert auth.status.value == "unhealthy"
    low = quality([point(i, 2.0, sample_quality=SampleQuality.LOW_VOLUME) for i in range(5)])
    finding = next(f for f in low.findings if f.issue is QualityIssue.LOW_VOLUME_VALUES)
    assert finding.severity is Severity.INFO  # a data property, not a source failure
    late = quality(series(3), arrival=[(MONDAY + DAY, MONDAY), (MONDAY, MONDAY + DAY)])
    assert QualityIssue.OUT_OF_ORDER_ARRIVAL in {f.issue for f in late.findings}


# ---------------------------------------------------------------------------- replay


def test_late_publication_is_invisible_before_it_was_published() -> None:
    """Monday's value published Wednesday must not be visible on a simulated Tuesday."""
    monday = point(
        0,
        provider_published_at=MONDAY + DAY * 2 + timedelta(hours=12),
        collected_at=MONDAY + DAY * 2 + timedelta(hours=12),
        ingested_at=MONDAY + DAY * 2 + timedelta(hours=12),
    )
    consumer = CollectingConsumer()
    report = replay(
        [monday], start=MONDAY + DAY, end=MONDAY + DAY * 3, step=DAY, consumers=[consumer]
    )
    tuesday, wednesday, thursday = report.steps[0], report.steps[1], report.steps[2]
    assert tuesday.visible == 0
    assert tuesday.withheld_not_yet_available == 1
    assert wednesday.visible == 0  # published 12:00, replay step at 00:00
    assert thursday.visible == 1
    assert consumer.visible_periods(MONDAY + DAY) == set()


def test_unknown_publication_uses_conservative_collection_time() -> None:
    p = point(
        0,
        provider_published_at=None,
        collected_at=MONDAY + DAY * 5,
        ingested_at=MONDAY + DAY * 5 + timedelta(minutes=1),
    )
    at, basis = availability(p, AvailabilityMode.PUBLICATION)
    assert at == MONDAY + DAY * 5 + timedelta(minutes=1)
    assert basis.startswith("conservative")
    backfilled = point(0, collected_at=MONDAY + DAY * 30, ingested_at=MONDAY + DAY * 30)
    assert availability(backfilled, AvailabilityMode.AGENT)[0] == MONDAY + DAY * 30


def test_revisions_replace_values_only_from_their_availability() -> None:
    original = point(0, 40.0, provider_published_at=MONDAY + DAY * 2)
    revised = point(
        0,
        47.0,
        revision=2,
        revision_kind="revision",
        supersedes_id=original.id,
        provider_published_at=MONDAY + DAY * 4,
    )
    conflict = point(
        0,
        99.0,
        revision=3,
        revision_kind="conflict",
        is_current=False,
        provider_published_at=MONDAY + DAY * 3,
    )
    consumer = CollectingConsumer()
    report = replay(
        [revised, conflict, original],
        start=MONDAY + DAY * 2,
        end=MONDAY + DAY * 5,
        step=DAY,
        consumers=[consumer],
    )
    values = [snap.series[1][0].value if snap.series else None for snap in consumer.snapshots]
    assert values == [40.0, 40.0, 47.0, 47.0]  # conflict never visible
    assert report.steps[2].revisions_applied == 1
    again = replay(
        [original, conflict, revised], start=MONDAY + DAY * 2, end=MONDAY + DAY * 5, step=DAY
    )
    assert again.final_digest == report.final_digest  # deterministic regardless of input order
    with pytest.raises(ValueError, match="step"):
        replay([original], start=MONDAY, end=MONDAY, step=timedelta(0))


# ---------------------------------------------------------------------------- synthetic + collector


SCENARIO = load_monitoring_scenario()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def fetch(
    provider: SyntheticTrendsProvider, query: str, start: datetime, end: datetime
) -> list[ObservationCandidate]:
    req = SearchInterestRequest(
        query=query,
        region="US",
        language="en",
        start=start,
        end=end,
        granularity=TimeGranularity.DAY,
        search_property=SearchProperty.YOUTUBE_SEARCH,
    )
    return list((await provider.fetch_search_interest(req, RequestContext())).observations)


async def test_synthetic_provider_publication_revision_and_outage() -> None:
    clock = Clock(datetime(2026, 3, 4, 11, tzinfo=UTC))
    provider = SyntheticTrendsProvider("synthetic_trends", SCENARIO, clock=clock)
    obs = await fetch(provider, "ai coding agents", MONDAY - DAY * 3, MONDAY + DAY * 5)
    # Wed 11:00: Monday (published Wed 12:00) not yet out.
    assert max(o.observation_start for o in obs if o.observation_start) == MONDAY - DAY
    clock.now = datetime(2026, 3, 4, 12, tzinfo=UTC)
    obs = await fetch(provider, "ai coding agents", MONDAY - DAY * 3, MONDAY + DAY * 5)
    assert max(o.observation_start for o in obs if o.observation_start) == MONDAY
    assert all(o.data_nature == DataNature.SYNTHETIC.value for o in obs)
    keys = {o.normalization_context.key() for o in obs if o.normalization_context}
    clock.now = datetime(2026, 3, 9, 12, tzinfo=UTC)
    later = await fetch(provider, "ai coding agents", MONDAY - DAY * 3, MONDAY + DAY * 5)
    assert {o.normalization_context.key() for o in later if o.normalization_context} == keys
    tuesday = next(o for o in later if o.observation_start == MONDAY + DAY)
    assert tuesday.available_at == datetime(2026, 3, 7, 6, tzinfo=UTC)  # revision time
    clock.now = datetime(2026, 3, 7, 6, tzinfo=UTC)
    with pytest.raises(TemporarilyUnavailableError):
        await fetch(provider, "ai coding agents", MONDAY, MONDAY + DAY)
    web = SyntheticTrendsProvider(
        "synthetic_trends_web", SCENARIO, clock=Clock(datetime(2026, 3, 9, tzinfo=UTC))
    )
    req = SearchInterestRequest(
        query="ai coding agents",
        region="US",
        start=MONDAY - DAY * 10,
        end=MONDAY,
        granularity=TimeGranularity.DAY,
        search_property=SearchProperty.GOOGLE_WEB_SEARCH,
    )
    batch = await web.fetch_search_interest(req, RequestContext())
    assert {o.scale_type for o in batch.observations} == {ScaleType.RELATIVE_PER_REQUEST.value}
    assert max(o.value or 0 for o in batch.observations) == 100.0


def snap(**kw: object) -> TargetSnapshot:
    base: dict[str, object] = {
        "target_id": 1,
        "keyword_id": 1,
        "query_id": 7,
        "canonical_query": "lattice 5 migration guide",
        "original_query": "lattice 5 migration guide",
        "region": "US",
        "language": "en",
        "measurement_category": MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        "is_proxy": False,
        "is_synthetic": True,
    }
    base.update(kw)
    return TargetSnapshot(**base)  # type: ignore[arg-type]


async def test_collector_validates_labels_and_enforces_quota() -> None:
    clock = Clock(datetime(2026, 3, 9, 12, tzinfo=UTC))
    profile = CONFIG.providers["synthetic_trends"].model_copy(
        update={
            "quota": CONFIG.providers["synthetic_trends"].quota.model_copy(
                update={"daily_requests": 1}
            )
        }
    )
    collector = SearchInterestCollector(
        SyntheticTrendsProvider("synthetic_trends", SCENARIO, clock=clock),
        profile,
        policies=PolicyRegistry(CONFIG, make_settings()),
        quota=local_quota(profile, InMemoryQuotaStore(), clock),
        clock=clock,
    )
    ctx = RequestContext(ledger=QuotaLedger())
    result = await collector.collect(snap(), MONDAY - DAY * 5, clock.now, ctx)
    assert result.in_batch_duplicates > 0  # duplicates removed by Phase 1 validation
    days = {d.period_start for d in result.drafts}
    assert len(days) == len(result.drafts)
    assert {d.measurement_category for d in result.drafts} == {
        MeasurementCategory.SYNTHETIC_TEST_OBSERVATION
    }
    assert {d.simulated_category for d in result.drafts} == {
        MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST
    }
    assert all(d.provider_published_at is not None for d in result.drafts)
    assert ctx.ledger.used == {"requests": 1}
    with pytest.raises(QuotaExhaustedError):
        await collector.collect(snap(), MONDAY, clock.now, RequestContext())
    external = await SearchInterestCollector(
        SyntheticTrendsProvider("synthetic_trends", SCENARIO, clock=clock),
        profile,
        policies=PolicyRegistry(CONFIG, make_settings()),
        quota=None,
        clock=clock,
    ).collect(
        snap(measurement_category=MeasurementCategory.EXTERNAL_SEARCH_INTEREST),
        MONDAY - DAY * 2,
        clock.now,
        RequestContext(),
    )
    assert {d.simulated_category for d in external.drafts} == {
        MeasurementCategory.EXTERNAL_SEARCH_INTEREST
    }  # never relabelled as YouTube interest


def test_window_strategies() -> None:
    now = MONDAY + DAY * 30
    trends = CONFIG.providers["synthetic_trends"]
    assert window_for(
        now=now, profile=trends, latest_period_end=None, initial_lookback=DAY * 30
    ) == (now - DAY * 30, now)
    latest = now - DAY * 2
    assert (
        window_for(now=now, profile=trends, latest_period_end=latest, initial_lookback=DAY * 30)[0]
        == latest - trends.revision_window
    )
    mock = CONFIG.providers["mock"]
    assert (
        window_for(now=now, profile=mock, latest_period_end=latest, initial_lookback=DAY * 30)[0]
        == now - mock.full_window
    )


# ---------------------------------------------------------------------------- observation model


def draft(**kw: object) -> ObservationDraft:
    base: dict[str, object] = {
        "query_id": 1,
        "target_id": 1,
        "provider_name": "p",
        "provider_version": "1",
        "measurement_category": MeasurementCategory.EXTERNAL_SEARCH_INTEREST,
        "unit": "index",
        "period_start": MONDAY,
        "period_end": MONDAY + DAY,
        "granularity": TimeGranularity.DAY,
        "collected_at": MONDAY + DAY * 3,
        "value": 10.0,
        "scale_type": ScaleType.CONSISTENT_INDEX,
        "normalization_context": NormalizationContext(
            scale_type=ScaleType.CONSISTENT_INDEX, method="m"
        ),
        "region": "US",
        "language": "en",
        "search_property": SearchProperty.GOOGLE_WEB_SEARCH,
        "data_nature": DataNature.ACTUAL,
        "sample_quality": SampleQuality.COMPLETE,
        "source_reference": "ref",
        "data_policy_id": "x@1",
    }
    base.update(kw)
    return ObservationDraft(**base)  # type: ignore[arg-type]


def test_observation_draft_invariants() -> None:
    assert draft().quality_status() is QualityStatus.CAVEAT  # publication time unknown
    assert draft(provider_published_at=MONDAY + DAY * 2).quality_status() is QualityStatus.VALID
    with pytest.raises(ValidationError, match="not zero"):
        draft(sample_quality=SampleQuality.SUPPRESSED)
    with pytest.raises(ValidationError, match="requires a value"):
        draft(value=None)
    missing = draft(value=None, sample_quality=SampleQuality.MISSING)
    assert missing.value is None
    with pytest.raises(ValidationError, match="synthetic"):
        draft(data_nature=DataNature.SYNTHETIC)
    with pytest.raises(ValidationError, match="published after"):
        draft(provider_published_at=MONDAY + DAY * 5)
    with pytest.raises(ValidationError, match="timezone-naive"):
        draft(period_start=datetime(2026, 3, 2), period_end=datetime(2026, 3, 3))
    a, b = draft(), draft(value=11.0)
    assert a.identity().key() == b.identity().key()  # same series, different value
    c = draft(region="GB")
    assert c.identity().key() != a.identity().key()
