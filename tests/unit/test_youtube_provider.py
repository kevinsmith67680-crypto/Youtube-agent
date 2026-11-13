"""YouTube Data API provider tests - all HTTP is served by httpx.MockTransport."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tests.helpers import (
    TEST_API_KEY,
    FakeYouTube,
    SleepRecorder,
    json_response,
    load_fixture,
)
from trend_agent.providers.base import RequestContext, VideoDataProvider
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderDisabledError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitedError,
    RequestBudgetExceededError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaLedger
from trend_agent.providers.youtube import YouTubeDataApiProvider
from trend_agent.schemas.enums import Capability, MeasurementType, ProviderStatus, RecordKind
from trend_agent.schemas.provider import PopularVideosRequest, VideoSearchOrder, VideoSearchRequest

Make = Callable[..., YouTubeDataApiProvider]


def ok(name: str) -> httpx.Response:
    return json_response(200, load_fixture(name))


def ctx() -> RequestContext:
    return RequestContext(ledger=QuotaLedger())


# ------------------------------------------------------------------------- search


async def test_search_success_with_pagination(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.queue("search", ok("search_page1.json"), ok("search_page2.json"))
    provider = make_youtube()
    context = ctx()
    request = VideoSearchRequest(
        query="AI agents", region="gb", language="en", max_results=10, order=VideoSearchOrder.DATE
    )
    response = await provider.search_videos(request, context)

    assert isinstance(provider, VideoDataProvider)
    assert [r.video_id for r in response.results] == ["vidAAAAAAA1", "vidAAAAAAA2", "vidAAAAAAA3"]
    assert [r.rank for r in response.results] == [1, 2, 3]
    assert [r.page for r in response.results] == [1, 1, 2]
    first = response.results[0]
    assert first.title == "AI agents & you: what's next"  # HTML entities decoded
    assert first.channel_title == "Channel & Co"
    assert first.url == "https://www.youtube.com/watch?v=vidAAAAAAA1"
    assert first.published_at is not None
    assert first.published_at.tzinfo is not None
    assert first.thumbnails["high"].width == 480
    assert response.pages_fetched == 2
    assert response.more_available is False
    assert response.measurement_type is MeasurementType.VIDEO_ACTIVITY
    assert response.approx_matching_videos == 1_000_000
    # One search-bucket call per page; nothing from the default bucket.
    assert context.ledger.used == {"search": 2}

    first_req, second_req = fake_youtube.calls("search")
    assert first_req.url.params["q"] == "AI agents"
    assert first_req.url.params["regionCode"] == "GB"
    assert first_req.url.params["relevanceLanguage"] == "en"
    assert first_req.url.params["type"] == "video"
    assert first_req.url.params["order"] == "date"
    assert first_req.url.params["maxResults"] == "10"
    assert "pageToken" not in first_req.url.params
    assert second_req.url.params["pageToken"] == "CAIQAA"


async def test_api_key_is_sent_in_header_never_in_url(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.queue("search", ok("search_page2.json"))
    response = await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())
    request = fake_youtube.requests[0]
    assert request.headers["X-Goog-Api-Key"] == TEST_API_KEY
    assert TEST_API_KEY not in str(request.url)
    assert TEST_API_KEY not in response.source_reference
    assert "key=" not in response.source_reference


async def test_pagination_is_bounded_by_max_pages(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("search", ok("search_page1.json"))  # always has nextPageToken
    context = ctx()
    response = await make_youtube(max_pages=2).search_videos(
        VideoSearchRequest(query="x", max_results=500), context
    )
    assert len(fake_youtube.calls("search")) == 2
    assert response.more_available is True
    assert context.ledger.used == {"search": 2}


async def test_pagination_stops_at_requested_limit(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("search", ok("search_page1.json"))
    response = await make_youtube().search_videos(
        VideoSearchRequest(query="x", max_results=1), ctx()
    )
    assert len(response.results) == 1
    assert len(fake_youtube.calls("search")) == 1
    assert fake_youtube.calls("search")[0].url.params["maxResults"] == "1"


async def test_published_range_is_sent_as_rfc3339(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    from datetime import UTC, datetime

    fake_youtube.queue("search", ok("search_page2.json"))
    await make_youtube().search_videos(
        VideoSearchRequest(
            query="x",
            published_after=datetime(2026, 1, 1, tzinfo=UTC),
            published_before=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        ctx(),
    )
    params = fake_youtube.requests[0].url.params
    assert params["publishedAfter"] == "2026-01-01T00:00:00Z"
    assert params["publishedBefore"] == "2026-02-01T00:00:00Z"


# ------------------------------------------------------------------------- credentials


async def test_missing_credentials_fail_without_any_request(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    provider = make_youtube(api_key=None)
    with pytest.raises(ProviderNotConfiguredError) as info:
        await provider.search_videos(VideoSearchRequest(query="x"), ctx())
    assert info.value.missing == ("TREND_AGENT_YOUTUBE_API_KEY",)
    assert fake_youtube.requests == []
    status = provider.status()
    assert status.status is ProviderStatus.NOT_CONFIGURED
    assert status.missing_configuration == ("TREND_AGENT_YOUTUBE_API_KEY",)


async def test_disabled_provider(make_youtube: Make, fake_youtube: FakeYouTube) -> None:
    provider = make_youtube(enabled=False)
    assert provider.status().status is ProviderStatus.DISABLED
    with pytest.raises(ProviderDisabledError):
        await provider.get_videos(["vidAAAAAAA1"], ctx())
    assert fake_youtube.requests == []


async def test_invalid_credentials_are_not_retried(
    make_youtube: Make, fake_youtube: FakeYouTube, sleeper: SleepRecorder
) -> None:
    # Real body captured from the API for an invalid key (HTTP 400, API_KEY_INVALID).
    fake_youtube.default("search", json_response(400, load_fixture("error_invalid_key.json")))
    with pytest.raises(InvalidCredentialsError) as info:
        await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())
    assert info.value.http_status == 400
    assert "API_KEY_INVALID" in (info.value.reason or "")
    assert len(fake_youtube.requests) == 1
    assert sleeper.delays == []


async def test_api_not_enabled_is_access_restricted(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("videos", json_response(403, load_fixture("error_api_not_enabled.json")))
    with pytest.raises(AccessRestrictedError):
        await make_youtube().get_videos(["vidAAAAAAA1"], ctx())
    assert len(fake_youtube.requests) == 1


# ------------------------------------------------------------------------- rate limit / quota


async def test_rate_limit_is_retried_with_backoff(
    make_youtube: Make, fake_youtube: FakeYouTube, sleeper: SleepRecorder
) -> None:
    fake_youtube.queue(
        "search",
        json_response(429, {"error": {"code": 429, "message": "slow"}}, {"Retry-After": "1"}),
        json_response(403, load_fixture("error_rate_limit.json")),
        ok("search_page2.json"),
    )
    context = ctx()
    response = await make_youtube().search_videos(VideoSearchRequest(query="x"), context)
    assert len(response.results) == 1
    assert len(sleeper.delays) == 2
    assert sleeper.delays[0] >= 1.0  # Retry-After honoured
    # Every attempt is a billable request and is accounted for.
    assert context.ledger.used == {"search": 3}


async def test_rate_limit_exhausting_attempts_raises_rate_limited(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("search", json_response(403, load_fixture("error_rate_limit.json")))
    with pytest.raises(RateLimitedError):
        await make_youtube(max_attempts=3).search_videos(VideoSearchRequest(query="x"), ctx())
    assert len(fake_youtube.requests) == 3


async def test_server_quota_exhaustion_is_final_and_blocks_further_requests(
    make_youtube: Make, fake_youtube: FakeYouTube, sleeper: SleepRecorder
) -> None:
    fake_youtube.default("videos", json_response(403, load_fixture("error_quota_exceeded.json")))
    provider = make_youtube()
    with pytest.raises(QuotaExhaustedError) as info:
        await provider.get_videos(["vidAAAAAAA1"], ctx())
    assert info.value.source == "server"
    assert info.value.bucket == "default"
    assert info.value.resets_at is not None
    assert sleeper.delays == []  # quota exhaustion is never retried
    assert len(fake_youtube.requests) == 1

    # The bucket is now locally marked exhausted: no further HTTP request is made.
    with pytest.raises(QuotaExhaustedError) as again:
        await provider.popular_videos(PopularVideosRequest(region="GB"), ctx())
    assert again.value.source == "local_estimate"
    assert len(fake_youtube.requests) == 1
    status = provider.status()
    assert status.capability_status(Capability.VIDEO_STATISTICS) is ProviderStatus.QUOTA_EXHAUSTED
    assert status.capability_status(Capability.VIDEO_SEARCH) is ProviderStatus.AVAILABLE


async def test_local_quota_exhaustion_stops_requests_mid_pagination(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("search", ok("search_page1.json"))  # every page has a next token
    # 4 search calls/day with 0% margin; each search wants up to 3 pages.
    provider = make_youtube(youtube_search_daily_call_limit=4, youtube_quota_safety_margin_pct=0)
    first = await provider.search_videos(VideoSearchRequest(query="x", max_results=100), ctx())
    assert first.pages_fetched == 3
    with pytest.raises(QuotaExhaustedError) as info:
        await provider.search_videos(VideoSearchRequest(query="y", max_results=100), ctx())
    assert info.value.source == "local_estimate"
    assert len(fake_youtube.calls("search")) == 4  # 4th allowed, 5th refused before sending
    with pytest.raises(QuotaExhaustedError):
        await provider.search_videos(VideoSearchRequest(query="z"), ctx())
    assert len(fake_youtube.calls("search")) == 4


async def test_request_budget_stops_pagination(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("search", ok("search_page1.json"))
    context = RequestContext(ledger=QuotaLedger(budget={"search": 1}))
    with pytest.raises(RequestBudgetExceededError):
        await make_youtube().search_videos(VideoSearchRequest(query="x", max_results=100), context)
    assert len(fake_youtube.requests) == 1


async def test_quota_store_is_shared_across_provider_instances(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    store = InMemoryQuotaStore()
    fake_youtube.default("videoCategories", ok("categories.json"))
    await make_youtube(store=store).video_categories("GB", ctx())
    await make_youtube(store=store).video_categories("GB", ctx())
    assert make_youtube(store=store).quota.usage("default").used == 2


# ------------------------------------------------------------------------- transient failures


async def test_timeout_is_retried_then_typed(
    make_youtube: Make, fake_youtube: FakeYouTube, sleeper: SleepRecorder
) -> None:
    fake_youtube.default("search", httpx.ReadTimeout("timed out"))
    context = ctx()
    with pytest.raises(ProviderTimeoutError) as info:
        await make_youtube(max_attempts=3).search_videos(VideoSearchRequest(query="x"), context)
    assert info.value.retryable
    assert len(fake_youtube.requests) == 3
    assert len(sleeper.delays) == 2
    assert context.ledger.used == {"search": 3}


async def test_network_error_then_success(make_youtube: Make, fake_youtube: FakeYouTube) -> None:
    fake_youtube.queue("videoCategories", httpx.ConnectError("refused"), ok("categories.json"))
    categories = await make_youtube().video_categories("GB", ctx())
    assert [c.category_id for c in categories] == ["1", "28"]


async def test_server_errors_are_retried(make_youtube: Make, fake_youtube: FakeYouTube) -> None:
    fake_youtube.queue(
        "videos",
        json_response(503, load_fixture("error_backend.json")),
        json_response(500, {"error": {"code": 500, "message": "boom"}}),
        ok("videos.json"),
    )
    response = await make_youtube().get_videos(["vidAAAAAAA1", "vidAAAAAAA2"], ctx())
    assert len(response.videos) == 2


async def test_persistent_server_error_raises_temporarily_unavailable(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("videos", json_response(503, load_fixture("error_backend.json")))
    with pytest.raises(TemporarilyUnavailableError):
        await make_youtube(max_attempts=2).get_videos(["vidAAAAAAA1"], ctx())
    assert len(fake_youtube.requests) == 2


async def test_invalid_parameter_is_permanent(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("videos", json_response(400, load_fixture("error_invalid_parameter.json")))
    with pytest.raises(InvalidRequestError):
        await make_youtube().popular_videos(PopularVideosRequest(region="GB"), ctx())
    assert len(fake_youtube.requests) == 1


# ------------------------------------------------------------------------- malformed responses


async def test_non_json_body_is_malformed_and_not_retried(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("search", httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(MalformedResponseError):
        await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())
    assert len(fake_youtube.requests) == 1


async def test_wrong_shape_is_malformed(make_youtube: Make, fake_youtube: FakeYouTube) -> None:
    fake_youtube.queue("search", json_response(200, {"items": {"not": "a list"}}))
    with pytest.raises(MalformedResponseError):
        await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())
    fake_youtube.queue("search", json_response(200, ["not", "an", "object"]))
    with pytest.raises(MalformedResponseError):
        await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())


async def test_malformed_items_are_skipped_with_warnings(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    body = load_fixture("search_page2.json")
    good = body["items"][0]
    body["items"] = [
        {"id": {"kind": "youtube#video"}, "snippet": good["snippet"]},  # no videoId
        {"id": {"kind": "youtube#channel", "channelId": "UCx"}, "snippet": good["snippet"]},
        {"id": good["id"], "snippet": {**good["snippet"], "publishedAt": "not-a-date"}},
        good,
    ]
    fake_youtube.queue("search", json_response(200, body))
    response = await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())
    assert [r.video_id for r in response.results] == ["vidAAAAAAA3"]
    assert response.results[0].rank == 1
    assert len(response.warnings) == 3


# ------------------------------------------------------------------------- videos / channels


async def test_video_details_parse_and_hidden_statistics_are_none(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.queue("videos", ok("videos.json"))
    response = await make_youtube().get_videos(
        ["vidAAAAAAA1", "vidAAAAAAA2", "vidAAAAAAA1", "gone0000001"], ctx()
    )
    by_id = {v.video_id: v for v in response.videos}
    first, second = by_id["vidAAAAAAA1"], by_id["vidAAAAAAA2"]
    assert first.duration_seconds == 933
    assert first.statistics is not None
    assert first.statistics.view_count == 12345
    assert first.statistics.like_count == 678
    assert first.tags == ("ai", "agents")
    assert second.duration_seconds == 0  # P0D
    assert second.statistics is not None
    assert second.statistics.like_count is None  # hidden, NOT zero
    assert second.statistics.comment_count is None  # disabled, NOT zero
    assert response.requested_ids == ("vidAAAAAAA1", "vidAAAAAAA2", "gone0000001")  # de-duplicated
    assert response.missing_ids == ("gone0000001",)
    request = fake_youtube.requests[0]
    assert request.url.params["id"] == "vidAAAAAAA1,vidAAAAAAA2,gone0000001"
    assert request.url.params["part"] == "snippet,contentDetails,statistics"


async def test_video_details_are_batched_by_50(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.default("videos", json_response(200, {"items": []}))
    ids = [f"v{i:010d}" for i in range(120)]
    context = ctx()
    response = await make_youtube().get_videos(ids, context)
    batches = [r.url.params["id"].split(",") for r in fake_youtube.calls("videos")]
    assert sorted(len(b) for b in batches) == [20, 50, 50]
    assert sorted(i for b in batches for i in b) == sorted(ids)
    assert context.ledger.used == {"default": 3}
    assert len(response.missing_ids) == 120


async def test_invalid_video_id_is_rejected_before_request(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    with pytest.raises(InvalidRequestError):
        await make_youtube().get_videos(["short", "a,b"], ctx())
    assert fake_youtube.requests == []


async def test_failed_batch_propagates_typed_error(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    fake_youtube.queue(
        "videos",
        json_response(200, {"items": []}),
        json_response(400, load_fixture("error_invalid_parameter.json")),
    )
    ids = [f"v{i:010d}" for i in range(60)]
    with pytest.raises(InvalidRequestError):
        await make_youtube().get_videos(ids, ctx())


async def test_channels_hidden_subscribers(make_youtube: Make, fake_youtube: FakeYouTube) -> None:
    fake_youtube.queue("channels", ok("channels.json"))
    response = await make_youtube().get_channels(
        ["UCaaaaaaaaaaaaaaaaaaaaaa", "UCbbbbbbbbbbbbbbbbbbbbbb"], ctx()
    )
    a, b = response.channels
    assert a.statistics is not None
    assert a.statistics.subscriber_count == 5000
    assert a.country == "GB"
    assert b.statistics is not None
    assert b.statistics.subscriber_count is None
    assert b.published_at is not None
    assert response.missing_ids == ()


async def test_popular_videos_chart(make_youtube: Make, fake_youtube: FakeYouTube) -> None:
    fake_youtube.queue("videos", ok("popular.json"))
    response = await make_youtube().popular_videos(
        PopularVideosRequest(region="gb", category_id="28", max_results=2), ctx()
    )
    assert [v.rank for v in response.videos] == [1, 2]
    assert response.videos[0].duration_seconds == 3723
    assert response.videos[1].statistics is not None
    assert response.videos[1].statistics.like_count is None
    params = fake_youtube.requests[0].url.params
    assert params["chart"] == "mostPopular"
    assert params["regionCode"] == "GB"
    assert params["videoCategoryId"] == "28"
    assert response.measurement_type is MeasurementType.VIDEO_ACTIVITY


# ------------------------------------------------------------------------- descriptor


def test_descriptor_declares_no_search_volume(make_youtube: Make) -> None:
    d = make_youtube().descriptor
    assert Capability.SEARCH_INTEREST not in d.capabilities
    assert d.measurement_types == frozenset({MeasurementType.VIDEO_ACTIVITY})
    assert d.requires_authentication
    assert not d.historical_data_available
    stats_rule = d.retention_for(RecordKind.VIDEO_STATISTICS)
    assert stats_rule.persist_allowed is False  # conservative default
    video_rule = d.retention_for(RecordKind.VIDEO)
    assert video_rule.max_retention is not None
    assert video_rule.max_retention.days == 30
    assert d.retention_for(RecordKind.SEARCH_INTEREST_OBSERVATION).persist_allowed is False


def test_status_available_with_key(make_youtube: Make) -> None:
    report = make_youtube().status()
    assert report.status is ProviderStatus.AVAILABLE
    assert all(c.status is ProviderStatus.AVAILABLE for c in report.capabilities)


async def test_request_rejected_for_missing_identity_is_access_restricted(
    make_youtube: Make, fake_youtube: FakeYouTube
) -> None:
    # Real body captured from the API for a request without a key (e.g. header stripped
    # by a proxy): HTTP 403 forbidden / ACCESS_DENIED. Not retried.
    fake_youtube.default("search", json_response(403, load_fixture("error_missing_key.json")))
    with pytest.raises(AccessRestrictedError) as info:
        await make_youtube().search_videos(VideoSearchRequest(query="x"), ctx())
    assert info.value.http_status == 403
    assert len(fake_youtube.requests) == 1
