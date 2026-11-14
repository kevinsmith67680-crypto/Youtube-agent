"""Hacker News discovery provider: items, provenance, incremental polling, errors, status."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from tests.hackernews_fixtures import NOW, FakeHN, list_source, make_provider, story
from tests.helpers import MutableClock
from trend_agent.discovery.models import (
    Category,
    DiscoveryRequest,
    EvidenceType,
    SourceType,
)
from trend_agent.ops.signals import SEARCH_INTEREST_CLASSES, SignalClass, classify
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidRequestError,
    ProviderDisabledError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.schemas.enums import Capability, ProviderStatus


def request(name: str, **kw: object) -> DiscoveryRequest:
    return DiscoveryRequest(source=list_source(name), **kw)  # type: ignore[arg-type]


async def test_new_stories_become_labelled_items_with_full_provenance() -> None:
    fake = FakeHN()
    p = make_provider(fake)
    result = await p.discover(request("newstories"), RequestContext())
    by_id = {i.metadata["hackernews"]["story_id"]: i for i in result.items}
    # relevant stories only; off-topic, injection, dead, deleted, job, comment excluded
    assert set(by_id) == {1001, 1003, 1005, 1006, 1009}
    launch = by_id[1001]
    assert launch.provider_name == "hackernews_firebase"
    assert launch.evidence_type is EvidenceType.COMMUNITY_DISCUSSION
    assert launch.source_reference == "https://news.ycombinator.com/item?id=1001"
    assert launch.url == "https://github.com/agentry/agentry"
    assert launch.publisher == "github.com"
    assert launch.author == "author"
    assert launch.published_at == NOW - timedelta(hours=1)
    assert launch.collected_at == NOW
    assert launch.category_hint is Category.AI
    assert launch.entity_hint == "Agentry"
    assert launch.entity_hint_type == "ai_product"
    assert launch.is_synthetic is False
    meta = launch.metadata
    assert meta["signal_class"] == "COMMUNITY_ACTIVITY"
    assert meta["entity_hint_origin"] == "discovered"  # never an evergreen seed
    hn = meta["hackernews"]
    assert hn["list"] == "newstories"
    assert hn["list_label"] == "new"
    assert hn["rank"] == fake.lists["newstories"].index(1001) + 1
    assert (hn["score"], hn["comments"], hn["type"]) == (40, 12, "story")
    assert hn["linked_url"] == "https://github.com/agentry/agentry"
    assert hn["discussion_url"] == launch.source_reference
    assert hn["classification"]["kind"] == "show_hn"
    assert hn["collected_at"] == NOW.isoformat()
    # a text post links to its own discussion; HTML is stripped from the text
    ask = by_id[1009]
    assert ask.url == ask.source_reference
    assert ask.text is not None
    assert "<script" not in ask.text
    assert "javascript:" not in ask.text
    assert any("prompt_injection_suspected x1" in w for w in result.warnings)
    assert any("dead x1" in w and "deleted x1" in w for w in result.warnings)
    assert any("not_a_story:job x1" in w for w in result.warnings)


async def test_chronological_lists_are_polled_incrementally() -> None:
    fake = FakeHN()
    p = make_provider(fake)
    first = await p.discover(request("newstories"), RequestContext())
    assert first.etag == "hn:last=1013"
    fake.items[1020] = story(1020, "Show HN: Newthing \u2013 an LLM eval tool")
    fake.lists["newstories"].insert(0, 1020)
    before = len(fake.requests)
    second = await p.discover(request("newstories", etag=first.etag), RequestContext())
    assert [i.metadata["hackernews"]["story_id"] for i in second.items] == [1020]
    assert len(fake.requests) - before == 2  # the list + one new item, nothing re-fetched
    assert second.etag == "hn:last=1020"
    third = await p.discover(request("newstories", etag=second.etag), RequestContext())
    assert third.items == ()
    assert third.etag == "hn:last=1020"


async def test_ranked_lists_are_re_read_for_repeated_observations() -> None:
    fake = FakeHN()
    p = make_provider(fake)
    a = await p.discover(request("topstories"), RequestContext())
    fake.set_activity(1002, score=420, comments=180)
    b = await p.discover(request("topstories", etag=a.etag), RequestContext())
    scores = {i.metadata["hackernews"]["story_id"]: i.metadata["points"] for i in b.items}
    assert scores[1002] == 420
    assert a.etag is None
    assert b.etag is None


async def test_lookback_filter_and_max_items() -> None:
    fake = FakeHN()
    p = make_provider(fake)
    res = await p.discover(request("topstories", since=NOW - timedelta(days=2)), RequestContext())
    assert 1014 not in {i.metadata["hackernews"]["story_id"] for i in res.items}
    assert any("older_than_lookback x1" in w for w in res.warnings)
    small = await p.discover(request("topstories", max_items=2), RequestContext())
    assert len(small.items) <= 2


async def test_partial_item_failures_keep_failed_stories_retriable() -> None:
    fake = FakeHN()
    fake.override["/item/1005.json"] = httpx.Response(500)
    p = make_provider(fake, attempts=1)
    res = await p.discover(request("newstories"), RequestContext())
    ids = {i.metadata["hackernews"]["story_id"] for i in res.items}
    assert 1005 not in ids
    assert 1001 in ids
    assert any("item requests failed: temporarily_unavailable x1" in w for w in res.warnings)
    assert res.etag == "hn:last=1004"  # just below the failed story: it is retried next poll
    del fake.override["/item/1005.json"]
    retry = await p.discover(request("newstories", etag=res.etag), RequestContext())
    assert 1005 in {i.metadata["hackernews"]["story_id"] for i in retry.items}


async def test_total_outage_is_a_typed_error_and_status_reflects_it() -> None:
    fake = FakeHN()
    fake.override["/newstories.json"] = httpx.ConnectError("refused")
    p = make_provider(fake, attempts=1)
    with pytest.raises(TemporarilyUnavailableError):
        await p.discover(request("newstories"), RequestContext())
    # a transient failure never blocks planning (sources back off individually) ...
    report = p.status()
    assert report.status is ProviderStatus.AVAILABLE
    assert "last request failed" in report.detail
    del fake.override["/newstories.json"]
    for sid in fake.lists["showstories"]:
        fake.override[f"/item/{sid}.json"] = httpx.Response(503)
    with pytest.raises(TemporarilyUnavailableError, match="all 3 item requests failed"):
        await p.discover(request("showstories"), RequestContext())


async def test_disabled_provider_and_unsupported_sources() -> None:
    fake = FakeHN()
    off = make_provider(fake, api_enabled=False)
    assert off.status().status is ProviderStatus.DISABLED
    assert "TREND_AGENT_HACKERNEWS_API_ENABLED" in off.status().detail
    with pytest.raises(ProviderDisabledError):
        await off.discover(request("newstories"), RequestContext())
    assert fake.requests == []
    p = make_provider(fake)
    assert p.supports(list_source("newstories"))
    assert not p.supports(list_source("jobstories"))
    assert not p.supports(list_source("newstories", source_type=SourceType.FEED))
    with pytest.raises(InvalidRequestError):
        await p.discover(request("jobstories"), RequestContext())


async def test_access_and_rate_limit_states_expire() -> None:
    fake = FakeHN()
    clock = MutableClock(NOW)
    fake.override["/newstories.json"] = httpx.ProxyError("403 Forbidden")
    p = make_provider(fake, clock=clock, attempts=1)
    with pytest.raises(AccessRestrictedError):
        await p.discover(request("newstories"), RequestContext())
    assert p.status().status is ProviderStatus.ACCESS_RESTRICTED
    clock.advance(timedelta(minutes=31))  # ... and a refusal only pauses it for a while
    assert p.status().status is ProviderStatus.AVAILABLE
    fake.override["/newstories.json"] = httpx.Response(429)
    with pytest.raises(RateLimitedError):
        await p.discover(request("newstories"), RequestContext())
    assert p.status().status is ProviderStatus.RATE_LIMITED
    clock.advance(timedelta(minutes=16))
    assert p.status().status is ProviderStatus.AVAILABLE
    del fake.override["/newstories.json"]
    await p.discover(request("newstories"), RequestContext())
    assert p.client.last_error is None
    assert "No credentials needed" in p.status().detail


def test_budget_check_counts_list_plus_items() -> None:
    p = make_provider(FakeHN(), budget=10)
    ok, _ = p.can_afford(list_source("newstories", params={"max_items": 9}))
    assert ok
    ok, why = p.can_afford(list_source("newstories", params={"max_items": 50}))
    assert not ok
    assert why


def test_descriptor_capabilities_and_signal_class_are_not_search_interest() -> None:
    p = make_provider(FakeHN(), clock=MutableClock(NOW))
    caps = p.descriptor.capabilities
    for cap in (
        Capability.NEW_STORY_DISCOVERY,
        Capability.TOP_STORY_DISCOVERY,
        Capability.SHOW_HN_DISCOVERY,
        Capability.ASK_HN_DISCOVERY,
        Capability.STORY_METADATA,
        Capability.STORY_UPDATES,
        Capability.DISCUSSION_ACTIVITY,
    ):
        assert cap in caps
    assert p.descriptor.requires_authentication is False
    classes = classify(p.descriptor)
    assert classes == (SignalClass.COMMUNITY_ACTIVITY,)
    assert not set(classes) & set(SEARCH_INTEREST_CLASSES)
    assert p.status().status is ProviderStatus.AVAILABLE


async def test_smoke_probe_spends_three_requests_and_skips_when_disabled() -> None:
    from trend_agent.ops.providers import smoke_test

    fake = FakeHN()
    res = await smoke_test(make_provider(fake))
    assert res.attempted
    assert res.ok
    assert res.units_spent == 3
    assert [r.url.path for r in fake.requests] == [
        "/v0/maxitem.json",
        "/v0/topstories.json",
        f"/v0/item/{fake.lists['topstories'][0]}.json",
    ]
    off = FakeHN()
    res = await smoke_test(make_provider(off, api_enabled=False))
    assert not res.attempted
    assert off.requests == []
