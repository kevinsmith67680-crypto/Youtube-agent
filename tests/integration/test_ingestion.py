"""End-to-end ingestion against PostgreSQL, with fake providers / HTTP transports."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from tests.helpers import FakeYouTube, MutableClock, json_response, load_fixture
from tests.unit.test_providers_mock_trends_registry import FakeTransport, available_trends
from trend_agent.config import TrendsAccessMode
from trend_agent.db.models import (
    ChannelRow,
    IngestionRunRow,
    ProviderRateLimitRow,
    RejectedRecordRow,
    SearchInterestObservationRow,
    VideoRow,
    VideoSearchResultRow,
    VideoStatisticsRow,
)
from trend_agent.db.repositories import ObservationRepository, SqlQuotaStore, VideoRepository
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.ingestion.service import (
    IngestionService,
    IngestionSettings,
    SearchInterestIngestion,
)
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import QuotaExhaustedError
from trend_agent.providers.google_trends import GoogleTrendsProvider
from trend_agent.providers.mock import MockScenario, MockSearchInterestProvider
from trend_agent.providers.registry import ProviderRegistry
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.schemas.enums import IngestionStatus, ProviderStatus
from trend_agent.schemas.observation import ObservationCandidate
from trend_agent.schemas.provider import (
    CapabilityStatus,
    PopularVideosRequest,
    ProviderDescriptor,
    ProviderStatusReport,
    SearchInterestBatch,
    SearchInterestRequest,
    VideoSearchRequest,
)
from trend_agent.schemas.video import VideoDetails

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 3, 1, tzinfo=UTC)  # 59 complete days before the fixed clock

MakeYouTube = Callable[..., YouTubeDataApiProvider]


class StaticProvider:
    """Minimal search-interest provider with scripted behaviour (structural typing)."""

    def __init__(self, behaviour: Callable[[SearchInterestRequest], Any], clock: MutableClock):
        self._behaviour = behaviour
        self._clock = clock
        self.calls: list[str] = []
        self._descriptor = MockSearchInterestProvider().descriptor.model_copy(
            update={"name": "static"}
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def status(self) -> ProviderStatusReport:
        return ProviderStatusReport(
            provider_name="static",
            status=ProviderStatus.AVAILABLE,
            detail="",
            capabilities=tuple(
                CapabilityStatus(capability=c, status=ProviderStatus.AVAILABLE)
                for c in self._descriptor.capabilities
            ),
            checked_at=self._clock(),
        )

    async def aclose(self) -> None:
        return None

    async def fetch_search_interest(
        self, request: SearchInterestRequest, ctx: RequestContext
    ) -> SearchInterestBatch:
        self.calls.append(request.query)
        result = self._behaviour(request)
        if asyncio.iscoroutine(result):
            result = await result
        values: list[float] = result
        context_model = (
            (
                await MockSearchInterestProvider(clock=self._clock).fetch_search_interest(
                    request, ctx
                )
            )
            .observations[0]
            .normalization_context
        )
        observations = tuple(
            ObservationCandidate(
                query=request.query,
                query_language=request.language,
                query_region=request.region,
                provider_name="static",
                provider_version="1.0.0",
                measurement_type="direct_search_interest",
                search_property="youtube_search",
                observation_start=START + timedelta(days=i),
                observation_end=START + timedelta(days=i + 1),
                granularity="day",
                collected_at=self._clock(),
                value=value,
                scale_type="relative_per_request",
                normalization_context=context_model,
                geography=request.region or "WORLD",
                sample_quality="complete",
                data_nature="synthetic",
                source_reference="synthetic://static",
            )
            for i, value in enumerate(values)
        )
        return SearchInterestBatch(
            provider_name="static",
            provider_version="1.0.0",
            request=request,
            observations=observations,
            source_reference="synthetic://static",
        )


@pytest.fixture
def registry(make_youtube: MakeYouTube, clock: MutableClock) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(
        MockSearchInterestProvider(
            clock=clock,
            scenario_overrides={
                "broken query": MockScenario.PROVIDER_FAILURE,
                "throttled": MockScenario.RATE_LIMITED,
            },
        )
    )
    reg.register(make_youtube())
    reg.register(
        GoogleTrendsProvider(
            access_mode=TrendsAccessMode.DISABLED,
            credentials_file=None,
            endpoint=None,
            retention_days=None,
            clock=clock,
        )
    )
    return reg


def make_service(
    registry: ProviderRegistry,
    factory: SessionFactory,
    clock: MutableClock,
    **settings: Any,
) -> IngestionService:
    return IngestionService(
        registry=registry,
        session_factory=factory,
        settings=IngestionSettings(**settings),
        clock=clock,
    )


def interest(*queries: str, provider: str = "mock", **kw: Any) -> SearchInterestIngestion:
    base: dict[str, Any] = {
        "provider": provider,
        "queries": queries,
        "region": "GB",
        "language": "en",
        "start": START,
        "end": END,
    }
    base.update(kw)
    return SearchInterestIngestion(**base)


def count(factory: SessionFactory, model: type) -> int:
    with transaction(factory) as s:
        return int(s.execute(sa.select(sa.func.count()).select_from(model)).scalar_one())


def run_row(factory: SessionFactory, run_id: Any) -> IngestionRunRow:
    with transaction(factory) as s:
        row = s.get(IngestionRunRow, run_id)
    assert row is not None
    return row


# ----------------------------------------------------------------------------- search interest


async def test_complete_mock_ingestion(
    registry: ProviderRegistry, session_factory: SessionFactory, clock: MutableClock
) -> None:
    summary = await make_service(registry, session_factory, clock).ingest_search_interest(
        interest("AI agents")
    )
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.totals() == {
        "received": 59,
        "stored": 59,
        "already_present": 0,
        "duplicate_in_batch": 0,
        "rejected": 0,
        "withheld": 0,
    }
    assert summary.is_balanced()
    assert summary.estimated_quota_used == 0
    row = run_row(session_factory, summary.run_id)
    assert row.status is IngestionStatus.SUCCEEDED
    assert (row.records_received, row.records_accepted, row.records_rejected) == (59, 59, 0)
    assert row.completed_at is not None
    with transaction(session_factory) as s:
        stored = ObservationRepository(s).list_for_query("ai agents")
    assert len(stored) == 59
    assert all(o.ingestion_run_id == summary.run_id for o in stored)
    assert all(o.observation.is_synthetic for o in stored)
    assert all(o.expires_at is None for o in stored)  # synthetic: no source retention limit


async def test_repeated_ingestion_is_idempotent(
    registry: ProviderRegistry, session_factory: SessionFactory, clock: MutableClock
) -> None:
    service = make_service(registry, session_factory, clock)
    await service.ingest_search_interest(interest("AI agents"))
    clock.advance(timedelta(hours=3))  # re-collected later: same observations
    again = await service.ingest_search_interest(interest("ai  AGENTS"))
    assert again.status is IngestionStatus.SUCCEEDED
    assert again.records_stored == 0
    assert again.records_already_present == 59
    assert count(session_factory, SearchInterestObservationRow) == 59


async def test_partial_failure(
    registry: ProviderRegistry, session_factory: SessionFactory, clock: MutableClock
) -> None:
    summary = await make_service(registry, session_factory, clock).ingest_search_interest(
        interest("AI agents", "broken query")
    )
    assert summary.status is IngestionStatus.PARTIAL
    failed = [f for f in summary.fetches if not f.succeeded]
    assert [(f.unit, f.error_type) for f in failed] == [("broken query", "temporarily_unavailable")]
    assert summary.records_stored == 59
    row = run_row(session_factory, summary.run_id)
    assert row.status is IngestionStatus.PARTIAL
    assert row.error_type == "temporarily_unavailable"


async def test_all_units_failing_marks_run_failed(
    registry: ProviderRegistry, session_factory: SessionFactory, clock: MutableClock
) -> None:
    summary = await make_service(registry, session_factory, clock).ingest_search_interest(
        interest("broken query", "throttled")
    )
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_type == "multiple"
    assert summary.records_received == 0


@pytest.mark.parametrize(
    ("provider", "error_type"),
    [
        ("youtube_data_api", "unsupported_capability"),
        ("google_trends", "not_configured"),
        ("does_not_exist", "provider_not_found"),
    ],
)
async def test_unusable_providers_fail_before_fetch_and_are_recorded(
    registry: ProviderRegistry,
    session_factory: SessionFactory,
    clock: MutableClock,
    fake_youtube: FakeYouTube,
    provider: str,
    error_type: str,
) -> None:
    summary = await make_service(registry, session_factory, clock).ingest_search_interest(
        interest("AI agents", provider=provider)
    )
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_type == error_type
    assert fake_youtube.requests == []
    row = run_row(session_factory, summary.run_id)
    assert row.status is IngestionStatus.FAILED
    assert row.error_type == error_type
    if provider == "google_trends":
        assert "TREND_AGENT_GOOGLE_TRENDS_ACCESS_MODE" in summary.extra["missing_configuration"]


async def test_invalid_records_are_quarantined_with_reasons(
    registry: ProviderRegistry, session_factory: SessionFactory, clock: MutableClock
) -> None:
    summary = await make_service(registry, session_factory, clock).ingest_search_interest(
        interest("AI agents", provider_options={"scenario": "invalid_records"})
    )
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.records_rejected == 6
    assert summary.records_stored == 53
    assert summary.is_balanced()
    with transaction(session_factory) as s:
        rows = s.execute(sa.select(RejectedRecordRow)).scalars().all()
    assert {r.reason_code.value for r in rows} == {
        "non_finite_value",
        "value_out_of_range",
        "invalid_window",
        "invalid_region",
        "missing_provenance",
        "invalid_timestamp",
    }
    assert all(r.ingestion_run_id == summary.run_id for r in rows)
    nan_row = next(r for r in rows if r.reason_code.value == "non_finite_value")
    assert nan_row.payload["value"] == "nan"  # stored JSON-safely for diagnosis


async def test_conflict_with_stored_value_is_rejected_not_overwritten(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    values = [[10.0, 20.0], [10.0, 99.0]]
    provider = StaticProvider(lambda _: values.pop(0), clock)
    reg = ProviderRegistry()
    reg.register(provider)
    service = make_service(reg, session_factory, clock)
    await service.ingest_search_interest(interest("AI agents", provider="static"))
    second = await service.ingest_search_interest(interest("AI agents", provider="static"))
    assert second.records_already_present == 1
    assert second.records_rejected == 1
    assert second.rejected_by_reason == {"conflicts_with_stored": 1}
    with transaction(session_factory) as s:
        stored = ObservationRepository(s).list_for_query("ai agents")
    assert sorted(o.observation.value or 0 for o in stored) == [10.0, 20.0]


async def test_storage_policy_withholds_data_without_retention_permission(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    provider = available_trends(FakeTransport())
    provider = GoogleTrendsProvider(  # same, but no retention period configured
        access_mode=TrendsAccessMode.OFFICIAL_ALPHA,
        credentials_file="c",
        endpoint="https://example.invalid/t",
        retention_days=None,
        transport=FakeTransport(),
        clock=clock,
    )
    reg = ProviderRegistry()
    reg.register(provider)
    summary = await make_service(reg, session_factory, clock).ingest_search_interest(
        interest("AI agents", provider="google_trends", search_property="google_web_search")
    )
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.records_withheld == 5
    assert summary.records_stored == 0
    assert count(session_factory, SearchInterestObservationRow) == 0


async def test_trends_with_retention_stores_external_interest_with_expiry(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    reg = ProviderRegistry()
    reg.register(available_trends(FakeTransport()))
    summary = await make_service(reg, session_factory, clock).ingest_search_interest(
        interest("AI agents", provider="google_trends", search_property="google_web_search")
    )
    assert summary.records_stored == 5
    with transaction(session_factory) as s:
        stored = ObservationRepository(s).list_for_query("ai agents")
    assert {o.observation.measurement_type.value for o in stored} == {"external_search_interest"}
    assert all(o.expires_at is not None for o in stored)


async def test_database_failure_rolls_back_the_whole_run(
    registry: ProviderRegistry,
    session_factory: SessionFactory,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ObservationRepository.insert
    calls = {"n": 0}

    def flaky_insert(self: ObservationRepository, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 10:
            raise OperationalError("INSERT", {}, Exception("connection lost"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ObservationRepository, "insert", flaky_insert)
    summary = await make_service(registry, session_factory, clock).ingest_search_interest(
        interest("AI agents")
    )
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_type == "database_error"
    assert "no records stored" in (summary.error_message or "")
    assert count(session_factory, SearchInterestObservationRow) == 0  # 9 earlier inserts undone
    assert run_row(session_factory, summary.run_id).status is IngestionStatus.FAILED


async def test_cancellation_marks_run_cancelled(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    started = asyncio.Event()

    async def hang(_: SearchInterestRequest) -> list[float]:
        started.set()
        await asyncio.Event().wait()
        return []

    reg = ProviderRegistry()
    reg.register(StaticProvider(hang, clock))
    task = asyncio.create_task(
        make_service(reg, session_factory, clock).ingest_search_interest(
            interest("AI agents", provider="static")
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with transaction(session_factory) as s:
        row = s.execute(sa.select(IngestionRunRow)).scalar_one()
    assert row.status is IngestionStatus.CANCELLED
    assert count(session_factory, SearchInterestObservationRow) == 0


async def test_fetch_timeout_is_a_typed_failure(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    async def slow(_: SearchInterestRequest) -> list[float]:
        await asyncio.sleep(5)
        return [1.0]

    reg = ProviderRegistry()
    reg.register(StaticProvider(slow, clock))
    summary = await make_service(
        reg, session_factory, clock, fetch_timeout_seconds=0.05
    ).ingest_search_interest(interest("AI agents", provider="static"))
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_type == "timeout"


async def test_quota_exhaustion_stops_remaining_units(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    def exhausted(_: SearchInterestRequest) -> list[float]:
        raise QuotaExhaustedError("used up", bucket="b", resets_at=None, source="server")

    provider = StaticProvider(exhausted, clock)
    reg = ProviderRegistry()
    reg.register(provider)
    summary = await make_service(
        reg, session_factory, clock, max_concurrency=1
    ).ingest_search_interest(interest("q1", "q2", "q3", provider="static"))
    assert provider.calls == ["q1"]  # no further requests after exhaustion
    assert [f.attempted for f in summary.fetches] == [True, False, False]
    assert {f.error_type for f in summary.fetches} == {"quota_exhausted"}
    assert summary.status is IngestionStatus.FAILED


# ----------------------------------------------------------------------------- youtube


def youtube_registry(provider: YouTubeDataApiProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(provider)
    return reg


def script_search(fake: FakeYouTube) -> None:
    fake.queue("search", json_response(200, load_fixture("search_page2.json")))
    videos = load_fixture("videos.json")
    videos["items"][0]["id"] = "vidAAAAAAA3"
    fake.queue("videos", json_response(200, {"items": [videos["items"][0]]}))
    channels = load_fixture("channels.json")
    fake.queue("channels", json_response(200, {"items": [channels["items"][0]]}))


async def test_video_search_ingestion_with_quota_accounting_and_stats_withheld(
    make_youtube: MakeYouTube,
    fake_youtube: FakeYouTube,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    script_search(fake_youtube)
    provider = make_youtube(store=SqlQuotaStore(session_factory))
    summary = await make_service(
        youtube_registry(provider), session_factory, clock
    ).ingest_video_search("youtube_data_api", VideoSearchRequest(query="AI agents", region="GB"))
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.quota_used == {"search": 1, "default": 2}
    assert summary.by_kind["video_statistics"] == {"received": 1, "withheld": 1}
    assert summary.by_kind["video_search_result"]["stored"] == 1
    assert summary.by_kind["channel"]["stored"] == 1
    assert summary.extra["approx_matching_videos_not_search_volume"] == 1_000_000
    assert summary.is_balanced()
    assert count(session_factory, VideoStatisticsRow) == 0  # compliance default
    assert count(session_factory, VideoSearchResultRow) == 1
    assert count(session_factory, ChannelRow) == 1
    with transaction(session_factory) as s:
        v = s.get(VideoRow, "vidAAAAAAA3")
        quota_rows = s.execute(sa.select(ProviderRateLimitRow)).scalars().all()
    assert v is not None
    assert v.duration_seconds == 933  # refreshed from videos.list
    assert v.metadata_expires_at == v.metadata_collected_at + timedelta(days=30)
    assert {(r.bucket, r.units_used) for r in quota_rows} == {("search", 1), ("default", 2)}
    assert run_row(session_factory, summary.run_id).estimated_quota_used == 3


async def test_statistics_persist_only_when_enabled_and_fresh_channels_not_refetched(
    make_youtube: MakeYouTube,
    fake_youtube: FakeYouTube,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    service = make_service(
        youtube_registry(make_youtube(persist_statistics=True)), session_factory, clock
    )
    script_search(fake_youtube)
    first = await service.ingest_video_search("youtube_data_api", VideoSearchRequest(query="x"))
    assert first.by_kind["video_statistics"]["stored"] == 1
    clock.advance(timedelta(hours=1))
    script_search(fake_youtube)
    second = await service.ingest_video_search("youtube_data_api", VideoSearchRequest(query="x"))
    assert second.extra["channels_skipped_fresh"] == 1
    assert len(fake_youtube.calls("channels")) == 1  # no needless second lookup
    assert second.quota_used == {"search": 1, "default": 1}
    assert count(session_factory, VideoStatisticsRow) == 2  # two snapshots over time


async def test_popular_ingestion_partial_failure(
    make_youtube: MakeYouTube,
    fake_youtube: FakeYouTube,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    fake_youtube.queue("videos", json_response(200, load_fixture("popular.json")))
    fake_youtube.queue("channels", json_response(403, load_fixture("error_api_not_enabled.json")))
    summary = await make_service(
        youtube_registry(make_youtube()), session_factory, clock
    ).ingest_popular_videos("youtube_data_api", PopularVideosRequest(region="GB", max_results=2))
    assert summary.status is IngestionStatus.PARTIAL
    assert summary.error_type == "access_restricted"
    assert summary.by_kind["video"]["stored"] == 2
    assert count(session_factory, VideoRow) == 2


async def test_youtube_not_configured_spends_no_quota(
    make_youtube: MakeYouTube,
    fake_youtube: FakeYouTube,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    summary = await make_service(
        youtube_registry(make_youtube(api_key=None)), session_factory, clock
    ).ingest_video_search("youtube_data_api", VideoSearchRequest(query="x"))
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_type == "not_configured"
    assert fake_youtube.requests == []


async def test_refresh_extends_available_and_deletes_unavailable_videos(
    make_youtube: MakeYouTube,
    fake_youtube: FakeYouTube,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    provider = make_youtube()
    from trend_agent.db.repositories import ProviderRepository

    with transaction(session_factory) as s:
        pid = ProviderRepository(s).sync(provider.descriptor)
        repo = VideoRepository(s)
        for vid in ("vidAAAAAAA1", "gone0000001"):
            repo.upsert_video_details(
                VideoDetails(
                    video_id=vid,
                    title="old",
                    description=None,
                    channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
                    channel_title=None,
                    published_at=None,
                    duration_seconds=None,
                    duration_iso=None,
                    category_id=None,
                    collected_at=clock() - timedelta(days=29),
                ),
                provider_id=pid,
                expires_at=clock() + timedelta(days=1),
                persist_description=True,
            )
    videos = load_fixture("videos.json")
    fake_youtube.queue("videos", json_response(200, {"items": [videos["items"][0]]}))
    summary = await make_service(
        youtube_registry(provider), session_factory, clock
    ).refresh_expiring_videos("youtube_data_api", horizon=timedelta(days=2))
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.extra == {"due": 2, "deleted_unavailable": 1}
    with transaction(session_factory) as s:
        refreshed = s.get(VideoRow, "vidAAAAAAA1")
        assert s.get(VideoRow, "gone0000001") is None
    assert refreshed is not None
    assert refreshed.title == "AI agents & you"
    assert refreshed.metadata_expires_at == clock() + timedelta(days=30)


async def test_malformed_youtube_items_are_recorded_as_rejections(
    make_youtube: MakeYouTube,
    fake_youtube: FakeYouTube,
    session_factory: SessionFactory,
    clock: MutableClock,
) -> None:
    body = load_fixture("search_page2.json")
    body["items"].append({"id": {"kind": "youtube#video"}, "snippet": {}})
    fake_youtube.queue("search", json_response(200, body))
    summary = await make_service(
        youtube_registry(make_youtube()), session_factory, clock
    ).ingest_video_search(
        "youtube_data_api",
        VideoSearchRequest(query="x"),
        fetch_details=False,
        fetch_channels=False,
    )
    assert summary.records_rejected == 1
    assert summary.rejected_by_reason == {"malformed_provider_item": 1}
    with transaction(session_factory) as s:
        reasons = s.execute(sa.select(RejectedRecordRow.reason_code)).scalars().all()
    assert [r.value for r in reasons] == ["malformed_provider_item"]


def test_httpx_mock_transport_is_used(fake_youtube: FakeYouTube) -> None:
    assert isinstance(fake_youtube.transport(), httpx.MockTransport)


async def test_unexpected_provider_exception_is_recorded_not_lost(
    session_factory: SessionFactory, clock: MutableClock
) -> None:
    def explode(request: SearchInterestRequest) -> list[float]:
        if request.query == "bad":
            raise RuntimeError("bug in provider adapter (token=abc123)")
        return [10.0]

    reg = ProviderRegistry()
    reg.register(StaticProvider(explode, clock))
    summary = await make_service(reg, session_factory, clock).ingest_search_interest(
        interest("good", "bad", "x" * 201, provider="static")
    )
    assert summary.status is IngestionStatus.PARTIAL
    outcomes = {f.unit[:5]: f for f in summary.fetches}
    assert outcomes["bad"].error_type == "internal_error"
    assert "abc123" not in (outcomes["bad"].error_message or "")  # redacted
    assert outcomes["x" * 5].error_type == "invalid_request"  # over-long query
    assert summary.records_stored == 1
    assert run_row(session_factory, summary.run_id).status is IngestionStatus.PARTIAL
