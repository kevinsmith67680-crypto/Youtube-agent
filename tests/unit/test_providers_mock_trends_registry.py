from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import FIXED_NOW, MutableClock
from trend_agent.config import TrendsAccessMode
from trend_agent.ingestion.validation import validate_batch
from trend_agent.providers.base import RequestContext, SearchInterestProvider
from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    InvalidRequestError,
    ProviderNotConfiguredError,
    ProviderNotFoundError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.google_trends import (
    GoogleTrendsProvider,
    TrendsPoint,
    TrendsResponse,
    measurement_type_for,
)
from trend_agent.providers.mock import MockScenario, MockSearchInterestProvider
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    MeasurementType,
    ProviderStatus,
    RecordKind,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.provider import SearchInterestRequest

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 3, 1, tzinfo=UTC)  # 59 complete days before FIXED_NOW


def request(scenario: MockScenario | None = None, **kw: object) -> SearchInterestRequest:
    options = {"scenario": scenario.value} if scenario else {}
    base: dict[str, object] = {
        "query": "AI agents",
        "region": "GB",
        "language": "en",
        "start": START,
        "end": END,
        "provider_options": options,
    }
    base.update(kw)
    return SearchInterestRequest(**base)  # type: ignore[arg-type]


async def fetch(
    provider: MockSearchInterestProvider, req: SearchInterestRequest
) -> list[float | None]:
    batch = await provider.fetch_search_interest(req, RequestContext())
    return [o.value for o in batch.observations]


# ----------------------------------------------------------------------------- mock


async def test_mock_is_deterministic_and_seed_sensitive() -> None:
    clock = MutableClock()
    a = MockSearchInterestProvider(seed=1, clock=clock)
    b = MockSearchInterestProvider(seed=1, clock=clock)
    c = MockSearchInterestProvider(seed=2, clock=clock)
    assert await fetch(a, request()) == await fetch(b, request())
    assert await fetch(a, request()) != await fetch(c, request())


async def test_every_mock_record_is_labelled_synthetic_and_valid() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    for scenario in [
        s
        for s in MockScenario
        if s.name not in {"PROVIDER_FAILURE", "RATE_LIMITED", "INVALID_RECORDS", "DUPLICATES"}
    ]:
        batch = await provider.fetch_search_interest(request(scenario), RequestContext())
        assert batch.observations
        assert all(o.data_nature == "synthetic" for o in batch.observations)
        assert all(
            (o.source_reference or "").startswith("synthetic://") for o in batch.observations
        )
        report = validate_batch(batch.observations, provider.descriptor, now=FIXED_NOW)
        assert not report.rejected, (scenario, report.rejected[:1])


async def test_mock_scenario_shapes() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    stable = await fetch(provider, request(MockScenario.STABLE))
    assert len(stable) == 59
    surge = await fetch(provider, request(MockScenario.SUDDEN_SURGE))
    assert max(v for v in surge if v is not None) == 100.0
    assert surge.index(100.0) == len(surge) - 7  # surge onset in final week
    assert max(v for v in surge[:-7] if v is not None) < 40
    growth = await fetch(provider, request(MockScenario.GRADUAL_GROWTH))
    assert sum(v for v in growth[-10:] if v) > sum(v for v in growth[:10] if v)
    decline = await fetch(provider, request(MockScenario.DECLINING))
    assert sum(v for v in decline[:10] if v) > sum(v for v in decline[-10:] if v)


async def test_mock_sparse_and_missing_use_explicit_quality() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    sparse = await provider.fetch_search_interest(request(MockScenario.SPARSE), RequestContext())
    qualities = {o.sample_quality for o in sparse.observations}
    assert {"suppressed", "low_volume"} <= qualities
    assert all(o.value is None for o in sparse.observations if o.sample_quality == "suppressed")
    missing = await provider.fetch_search_interest(
        request(MockScenario.MISSING_DATA), RequestContext()
    )
    gaps = [o for o in missing.observations if o.sample_quality == "missing"]
    assert gaps
    assert all(o.value is None for o in gaps)


async def test_mock_out_of_order_duplicates_and_mixed_normalisation() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    shuffled = await provider.fetch_search_interest(
        request(MockScenario.OUT_OF_ORDER), RequestContext()
    )
    starts = [o.observation_start for o in shuffled.observations]
    assert starts != sorted(starts)  # type: ignore[type-var]
    dupes = await provider.fetch_search_interest(request(MockScenario.DUPLICATES), RequestContext())
    report = validate_batch(dupes.observations, provider.descriptor, now=FIXED_NOW)
    assert report.duplicates_in_batch == 3
    assert report.rejected_by_reason() == {"duplicate_conflict": 2}
    mixed = await provider.fetch_search_interest(
        request(MockScenario.MIXED_NORMALIZATION), RequestContext()
    )
    batches = {
        o.normalization_context.batch_id for o in mixed.observations if o.normalization_context
    }
    assert len(batches) == 2


async def test_mock_partial_period_for_current_window() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    batch = await provider.fetch_search_interest(
        request(end=FIXED_NOW + timedelta(days=5)), RequestContext()
    )
    last = batch.observations[-1]
    assert last.sample_quality == "partial_period"
    assert last.observation_end is not None
    assert last.observation_end > FIXED_NOW  # nothing beyond "now" is generated except it
    assert len(batch.observations) == 60


async def test_mock_failure_scenarios_raise_typed_errors() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    with pytest.raises(TemporarilyUnavailableError):
        await provider.fetch_search_interest(
            request(MockScenario.PROVIDER_FAILURE), RequestContext()
        )
    with pytest.raises(RateLimitedError):
        await provider.fetch_search_interest(request(MockScenario.RATE_LIMITED), RequestContext())
    with pytest.raises(InvalidRequestError):
        await provider.fetch_search_interest(
            request(provider_options={"scenario": "nope"}), RequestContext()
        )
    with pytest.raises(CapabilityNotSupportedError):
        await provider.fetch_search_interest(
            request(granularity=TimeGranularity.HOUR), RequestContext()
        )


async def test_mock_scenario_overrides_per_query() -> None:
    provider = MockSearchInterestProvider(
        clock=MutableClock(), scenario_overrides={"Broken Query": MockScenario.PROVIDER_FAILURE}
    )
    with pytest.raises(TemporarilyUnavailableError):
        await provider.fetch_search_interest(request(query="broken  query"), RequestContext())
    assert await fetch(provider, request())


async def test_mock_web_property_is_external_interest() -> None:
    provider = MockSearchInterestProvider(clock=MutableClock())
    batch = await provider.fetch_search_interest(
        request(search_property=SearchProperty.GOOGLE_WEB_SEARCH), RequestContext()
    )
    assert {o.measurement_type for o in batch.observations} == {"external_search_interest"}


# ----------------------------------------------------------------------------- google trends


def trends(**kw: object) -> GoogleTrendsProvider:
    base: dict[str, object] = {
        "access_mode": TrendsAccessMode.DISABLED,
        "credentials_file": None,
        "endpoint": None,
        "retention_days": None,
        "clock": MutableClock(),
    }
    base.update(kw)
    return GoogleTrendsProvider(**base)  # type: ignore[arg-type]


@dataclass
class FakeTransport:
    verified: bool = True
    supported_properties: frozenset[SearchProperty] = frozenset({SearchProperty.GOOGLE_WEB_SEARCH})
    supported_granularities: frozenset[TimeGranularity] = frozenset(
        {TimeGranularity.DAY, TimeGranularity.WEEK}
    )
    returned_granularity: TimeGranularity = TimeGranularity.DAY
    calls: list[SearchInterestRequest] = field(default_factory=list)

    async def fetch(self, req: SearchInterestRequest) -> TrendsResponse:
        self.calls.append(req)
        day = timedelta(days=1)
        points = (
            TrendsPoint("ai agents", START, START + day, 55.0),
            TrendsPoint("ai agents", START + day, START + 2 * day, 0.0),  # insufficient data
            TrendsPoint("ai agents", START + 2 * day, START + 3 * day, None, suppressed=True),
            TrendsPoint("ai agents", START + 3 * day, START + 4 * day, None),
            TrendsPoint("ai agents", START + 4 * day, START + 5 * day, 12.0, low_volume=True),
        )
        return TrendsResponse(
            points=points,
            granularity=self.returned_granularity,
            scale_type=ScaleType.CONSISTENT_INDEX,
            scale_method="trends_alpha_consistent",
            scale_version="alpha-1",
            batch_id="req-1",
            co_normalized_terms=("ai agents",),
            geography=req.region or "WORLD",
            search_property=req.search_property,
            category=None,
            data_nature=DataNature.SAMPLED,
            source_reference="trends-alpha://request/req-1",
            available_at=START + 6 * day,
        )

    async def aclose(self) -> None:
        return None


def test_trends_disabled_reports_not_configured() -> None:
    report = trends().status()
    assert report.status is ProviderStatus.NOT_CONFIGURED
    assert "limited-access alpha" in report.detail
    assert "TREND_AGENT_GOOGLE_TRENDS_ACCESS_MODE" in report.missing_configuration


def test_trends_alpha_mode_without_credentials_lists_missing() -> None:
    report = trends(access_mode=TrendsAccessMode.OFFICIAL_ALPHA).status()
    assert report.status is ProviderStatus.NOT_CONFIGURED
    assert report.missing_configuration == (
        "TREND_AGENT_GOOGLE_TRENDS_CREDENTIALS_FILE",
        "TREND_AGENT_GOOGLE_TRENDS_ENDPOINT",
    )


def test_trends_configured_without_verified_transport_is_access_restricted() -> None:
    provider = trends(
        access_mode=TrendsAccessMode.OFFICIAL_ALPHA,
        credentials_file="/secure/creds.json",
        endpoint="https://example.invalid/trends",
        transport=FakeTransport(verified=False),
    )
    assert provider.status().status is ProviderStatus.ACCESS_RESTRICTED
    assert provider.descriptor.supported_granularities == frozenset()  # unknown, not assumed
    assert provider.descriptor.data_nature is DataNature.UNAVAILABLE


async def test_trends_never_fabricates_data_without_access() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        await trends().fetch_search_interest(request(), RequestContext())
    transport = FakeTransport(verified=False)
    restricted = trends(
        access_mode=TrendsAccessMode.OFFICIAL_ALPHA,
        credentials_file="c",
        endpoint="https://example.invalid/t",
        transport=transport,
    )
    with pytest.raises(AccessRestrictedError):
        await restricted.fetch_search_interest(request(), RequestContext())
    assert transport.calls == []


def available_trends(transport: FakeTransport | None = None) -> GoogleTrendsProvider:
    return trends(
        access_mode=TrendsAccessMode.OFFICIAL_ALPHA,
        credentials_file="c",
        endpoint="https://example.invalid/t",
        retention_days=30,
        transport=transport or FakeTransport(),
    )


async def test_trends_maps_points_with_honest_labels() -> None:
    provider = available_trends()
    assert provider.status().status is ProviderStatus.AVAILABLE
    assert isinstance(provider, SearchInterestProvider)
    batch = await provider.fetch_search_interest(
        request(search_property=SearchProperty.GOOGLE_WEB_SEARCH), RequestContext()
    )
    obs = batch.observations
    assert {o.measurement_type for o in obs} == {"external_search_interest"}
    assert [o.sample_quality for o in obs] == [
        "complete",
        "low_volume",  # 0 = insufficient data, not zero demand
        "suppressed",
        "missing",
        "low_volume",
    ]
    assert obs[0].normalization_context is not None
    assert obs[0].normalization_context.scale_type is ScaleType.CONSISTENT_INDEX
    report = validate_batch(obs, provider.descriptor, now=FIXED_NOW)
    assert not report.rejected
    assert len(report.accepted) == 5
    rule = provider.descriptor.retention_for(RecordKind.SEARCH_INTEREST_OBSERVATION)
    assert rule.persist_allowed
    assert rule.max_retention == timedelta(days=30)


async def test_trends_rejects_unsupported_property_and_granularity() -> None:
    provider = available_trends()
    with pytest.raises(CapabilityNotSupportedError):
        await provider.fetch_search_interest(
            request(search_property=SearchProperty.YOUTUBE_SEARCH), RequestContext()
        )
    with pytest.raises(CapabilityNotSupportedError):
        await provider.fetch_search_interest(
            request(
                search_property=SearchProperty.GOOGLE_WEB_SEARCH, granularity=TimeGranularity.HOUR
            ),
            RequestContext(),
        )


async def test_trends_records_actual_granularity_with_warning() -> None:
    provider = available_trends(FakeTransport(returned_granularity=TimeGranularity.WEEK))
    batch = await provider.fetch_search_interest(
        request(search_property=SearchProperty.GOOGLE_WEB_SEARCH), RequestContext()
    )
    assert {o.granularity for o in batch.observations} == {"week"}
    assert any("provider returned week" in w for w in batch.warnings)


def test_trends_property_to_measurement_mapping() -> None:
    assert (
        measurement_type_for(SearchProperty.YOUTUBE_SEARCH)
        is MeasurementType.DIRECT_SEARCH_INTEREST
    )
    assert (
        measurement_type_for(SearchProperty.GOOGLE_WEB_SEARCH)
        is MeasurementType.EXTERNAL_SEARCH_INTEREST
    )
    assert measurement_type_for(SearchProperty.UNKNOWN) is MeasurementType.UNKNOWN


def test_trends_storage_denied_until_retention_configured() -> None:
    rule = trends().descriptor.retention_for(RecordKind.SEARCH_INTEREST_OBSERVATION)
    assert rule.persist_allowed is False


def test_sample_quality_enum_is_used() -> None:
    assert SampleQuality.LOW_VOLUME.value == "low_volume"


# ----------------------------------------------------------------------------- registry


def test_registry_lookup_and_capabilities() -> None:
    registry = ProviderRegistry()
    mock = MockSearchInterestProvider(clock=MutableClock())
    registry.register(mock)
    registry.register(trends())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(MockSearchInterestProvider())
    assert registry.names() == ["google_trends", "mock"]
    assert registry.search_interest("mock") is mock
    with pytest.raises(ProviderNotFoundError, match="registered: google_trends, mock"):
        registry.get("nope")
    with pytest.raises(CapabilityNotSupportedError):
        registry.video_data("mock")
    matrix = registry.capability_matrix()
    assert matrix["mock"][Capability.SEARCH_INTEREST] is ProviderStatus.AVAILABLE
    assert matrix["mock"][Capability.VIDEO_SEARCH] is ProviderStatus.UNSUPPORTED
    assert matrix["google_trends"][Capability.SEARCH_INTEREST] is ProviderStatus.NOT_CONFIGURED
    assert [p.descriptor.name for p in registry.with_capability(Capability.SEARCH_INTEREST)] == [
        "google_trends",
        "mock",
    ]


def test_request_validation() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        request(end=START)
    with pytest.raises(ValueError, match="naive"):
        request(start=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="invalid region"):
        request(region="UK")


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (ProviderStatus.NOT_CONFIGURED, "not_configured"),
        (ProviderStatus.DISABLED, "provider_disabled"),
        (ProviderStatus.ACCESS_RESTRICTED, "access_restricted"),
        (ProviderStatus.QUOTA_EXHAUSTED, "quota_exhausted"),
        (ProviderStatus.UNSUPPORTED, "unsupported_capability"),
        (ProviderStatus.RATE_LIMITED, "rate_limited"),
        (ProviderStatus.TEMPORARILY_UNAVAILABLE, "temporarily_unavailable"),
    ],
)
def test_status_to_typed_error_mapping(status: ProviderStatus, error_type: str) -> None:
    from trend_agent.ingestion.service import _status_error
    from trend_agent.schemas.provider import CapabilityStatus, ProviderStatusReport

    report = ProviderStatusReport(
        provider_name="p",
        status=status,
        detail="d",
        capabilities=(CapabilityStatus(capability=Capability.SEARCH_INTEREST, status=status),),
        checked_at=FIXED_NOW,
    )
    error = _status_error(report, Capability.SEARCH_INTEREST)
    assert error.error_type == error_type
    assert error.retryable is (
        status in {ProviderStatus.RATE_LIMITED, ProviderStatus.TEMPORARILY_UNAVAILABLE}
    )
