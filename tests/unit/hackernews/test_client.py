"""Hacker News Firebase client: endpoints, parsing, missing/dead records, errors, limits."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from tests.hackernews_fixtures import NOW, FakeHN, body, make_client
from tests.helpers import MutableClock, SleepRecorder
from trend_agent.hackernews.models import LISTS, HNItem, parse_item
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderTimeoutError,
    QuotaExhaustedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)


def ctx() -> RequestContext:
    return RequestContext()


@pytest.mark.parametrize("name", LISTS)
async def test_each_story_list_is_retrieved(name: str) -> None:
    fake = FakeHN()
    client = make_client(fake)
    ids = await client.story_ids(name, ctx())
    assert ids == fake.lists[name]
    req = fake.calls(f"/{name}.json")[0]
    assert req.headers["accept"] == "application/json"
    assert req.headers["user-agent"] == "trend-agent-tests/0"


async def test_unknown_list_and_bad_paths_are_rejected_before_any_request() -> None:
    fake = FakeHN()
    client = make_client(fake)
    with pytest.raises(InvalidRequestError):
        await client.story_ids("jobstories; DROP", ctx())
    with pytest.raises(InvalidRequestError):
        await client.get_json("/../secret.json", ctx(), endpoint="list")
    with pytest.raises(InvalidRequestError):
        await client.item(0, ctx())
    assert fake.requests == []


async def test_item_parsing_and_skip_reasons() -> None:
    fake = FakeHN()
    client = make_client(fake)
    item, problem = await client.item(1002, ctx())
    assert problem is None
    assert item is not None
    assert item.title == "Qwen 4 released with open weights"
    assert item.score == 300
    assert item.descendants == 150
    assert item.published_at == NOW - timedelta(hours=3)
    assert item.discussion_url == "https://news.ycombinator.com/item?id=1002"
    assert item.skip_reason() is None
    batch = await client.items([1001, 1010, 1011, 1012, 1013, 99999, 1001], ctx())
    assert set(batch.items) == {1001}
    assert batch.skipped == {
        1010: "dead",
        1011: "deleted",
        1012: "not_a_story:job",
        1013: "not_a_story:comment",
        99999: "missing",  # the API returns null for unknown IDs
    }
    assert batch.failed == {}
    # an ID listed twice is fetched once
    assert len(fake.calls("/item/1001.json")) == 1


def test_parse_item_handles_null_malformed_and_unknown_fields() -> None:
    assert parse_item(None) == (None, "missing")
    assert parse_item([1, 2]) == (None, "malformed:not_an_object")
    item, problem = parse_item({"id": "abc", "type": "story"})
    assert item is None
    assert problem == "malformed:id"
    item, problem = parse_item({"id": 5, "type": "story", "title": "x", "time": 1, "new_field": 1})
    assert problem is None
    assert isinstance(item, HNItem)
    assert HNItem(id=5, type="story", title="  ", time=1).skip_reason() == "missing_title"
    assert HNItem(id=5, type="story", title="x").skip_reason() == "missing_time"


async def test_empty_and_null_lists() -> None:
    fake = FakeHN()
    fake.lists["askstories"] = []
    fake.override["/beststories.json"] = body(None)
    client = make_client(fake)
    assert await client.story_ids("askstories", ctx()) == []
    assert await client.story_ids("beststories", ctx()) == []


async def test_malformed_payloads_are_typed_errors() -> None:
    fake = FakeHN()
    fake.override["/newstories.json"] = httpx.Response(200, json={"ids": [1]})
    fake.override["/topstories.json"] = httpx.Response(200, content=b"<html>oops</html>")
    fake.override["/updates.json"] = httpx.Response(200, json=[1, 2])
    fake.override["/maxitem.json"] = httpx.Response(200, json="12")
    client = make_client(fake)
    for call in (
        client.story_ids("newstories", ctx()),
        client.story_ids("topstories", ctx()),
        client.updates(ctx()),
        client.max_item(ctx()),
    ):
        with pytest.raises(MalformedResponseError):
            await call


async def test_updates_and_maxitem() -> None:
    fake = FakeHN()
    fake.updates = [1001, -3, 1002]
    client = make_client(fake)
    assert await client.updates(ctx()) == {1001, 1002}
    assert await client.max_item(ctx()) == max(fake.items)


async def test_timeouts_are_retried_then_reported() -> None:
    fake = FakeHN()
    fake.override["/newstories.json"] = httpx.ReadTimeout("slow")
    sleeper = SleepRecorder()
    client = make_client(fake, attempts=3, sleeper=sleeper)
    with pytest.raises(ProviderTimeoutError):
        await client.story_ids("newstories", ctx())
    assert len(fake.calls("/newstories.json")) == 3
    assert len(sleeper.delays) == 2  # backoff between attempts, never a busy loop
    assert isinstance(client.last_error, ProviderTimeoutError)


async def test_transient_server_error_recovers() -> None:
    fake = FakeHN()
    responses = iter([httpx.Response(503), httpx.Response(200, json=[1, 2, 3])])
    fake.override["/topstories.json"] = lambda r: next(responses)
    client = make_client(fake, attempts=3)
    assert await client.story_ids("topstories", ctx()) == [1, 2, 3]
    assert client.last_error is None
    assert client.last_success_at == NOW


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, headers={"retry-after": "30"}), RateLimitedError),
        (httpx.Response(403), AccessRestrictedError),
        (httpx.Response(500), TemporarilyUnavailableError),
        (httpx.Response(404), InvalidRequestError),
    ],
)
async def test_http_errors_map_to_typed_errors(response: httpx.Response, error: type) -> None:
    fake = FakeHN()
    fake.override["/askstories.json"] = response
    client = make_client(fake, attempts=1)
    with pytest.raises(error):
        await client.story_ids("askstories", ctx())


async def test_proxy_denial_is_reported_as_network_policy() -> None:
    fake = FakeHN()
    fake.override["/maxitem.json"] = httpx.ProxyError("403 Forbidden")
    client = make_client(fake, attempts=1)
    with pytest.raises(AccessRestrictedError) as exc:
        await client.max_item(ctx())
    assert exc.value.reason == "network_policy"


async def test_partial_item_failures_are_recorded_per_item() -> None:
    fake = FakeHN()
    fake.override["/item/1002.json"] = httpx.Response(500)
    fake.override["/item/1004.json"] = httpx.ConnectError("reset")
    client = make_client(fake, attempts=1)
    batch = await client.items([1001, 1002, 1004, 1005], ctx())
    assert set(batch.items) == {1001, 1005}
    assert batch.failed == {1002: "temporarily_unavailable", 1004: "temporarily_unavailable"}


async def test_oversized_response_is_rejected() -> None:
    fake = FakeHN()
    fake.override["/newstories.json"] = httpx.Response(
        200, content=b"[" + b"1," * 1_100_000 + b"1]"
    )
    client = make_client(fake, attempts=1)
    with pytest.raises(MalformedResponseError):
        await client.story_ids("newstories", ctx())


async def test_local_daily_budget_is_enforced() -> None:
    fake = FakeHN()
    client = make_client(fake, budget=3)
    batch = await client.items([1001, 1002, 1004, 1005], ctx())
    assert len(batch.items) == 3
    assert list(batch.failed.values()) == ["quota_exhausted"]
    with pytest.raises(QuotaExhaustedError):
        await client.story_ids("newstories", ctx())
    assert len(fake.requests) == 3


async def test_cache_and_in_flight_dedup_avoid_repeat_requests() -> None:
    fake = FakeHN()
    clock = MutableClock(NOW)
    client = make_client(fake, clock=clock, item_cache=timedelta(minutes=2))
    await asyncio.gather(*(client.item(1001, ctx()) for _ in range(5)))
    assert len(fake.calls("/item/1001.json")) == 1
    await client.item(1001, ctx())
    assert len(fake.calls("/item/1001.json")) == 1  # cached
    clock.advance(timedelta(minutes=3))
    await client.item(1001, ctx())
    assert len(fake.calls("/item/1001.json")) == 2  # expired -> fetched again


async def test_concurrency_is_bounded() -> None:
    fake = FakeHN()
    active = 0
    peak = 0
    real = fake.handler

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return real(request)

    client = make_client(fake, concurrency=2)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(slow))  # type: ignore[assignment]
    await client.items([1001, 1002, 1003, 1004, 1005, 1006], ctx())
    assert peak <= 2
