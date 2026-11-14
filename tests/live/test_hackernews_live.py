"""Optional live tests against the official Hacker News Firebase API.

Never part of the default run: the ``live`` marker is deselected in pyproject.toml, and the
tests skip unless ``TREND_AGENT_RUN_LIVE_TESTS=1``. No credentials are needed.

    TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_hackernews_live.py

Cost: about 20 GET requests (maxitem, updates, five lists, a few items), bounded by a local
budget of 40. Nothing is stored.
"""

from __future__ import annotations

import random

import httpx
import pytest

from tests.helpers import RUN_LIVE
from trend_agent.discovery.models import DiscoveryRequest, DiscoverySource, SourceType
from trend_agent.discovery.providers.hackernews_firebase import HackerNewsDiscoveryProvider
from trend_agent.hackernews.client import HackerNewsClient, hn_quota_policy
from trend_agent.hackernews.config import HackerNewsSettings
from trend_agent.hackernews.models import LISTS
from trend_agent.hackernews.service import connection_test
from trend_agent.providers.base import RequestContext
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaManager
from trend_agent.utils.time import utc_now

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not RUN_LIVE, reason="live tests need TREND_AGENT_RUN_LIVE_TESTS=1"),
]


def live_client(budget: int = 40) -> HackerNewsClient:
    return HackerNewsClient(
        http_client=httpx.AsyncClient(timeout=20),
        quota=QuotaManager(hn_quota_policy(budget), InMemoryQuotaStore(), utc_now),
        retry=RetryPolicy(max_attempts=2, rng=random.Random(0)),
        clock=utc_now,
        user_agent="youtube-trend-agent-live-test",
        max_concurrency=4,
    )


async def test_live_connection_covers_every_endpoint() -> None:
    report = await connection_test(live_client())
    assert report["error"] is None, report.get("error_detail")
    assert report["ok"] is True
    assert set(report["endpoints"].values()) == {"ok"}
    assert set(report["lists"]) == set(LISTS)
    assert report["max_item"] > 40_000_000
    for s in report["sample"]:
        assert s["discussion_url"] == f"https://news.ycombinator.com/item?id={s['story_id']}"


async def test_live_new_stories_discovery_is_bounded_and_labelled() -> None:
    client = live_client()
    provider = HackerNewsDiscoveryProvider(
        client,
        settings=HackerNewsSettings(),
        enabled=True,
        api_enabled=True,
        retention_days=90,
        clock=utc_now,
    )
    source = DiscoverySource(
        key="hn:list:newstories",
        provider="hackernews_firebase",
        name="live",
        source_type=SourceType.COMMUNITY,
        category="ai",  # type: ignore[arg-type]
        reference="newstories",
        params={"max_items": 10},
    )
    result = await provider.discover(DiscoveryRequest(source=source), RequestContext())
    assert client.requests_sent <= 11  # one list + at most ten items
    assert result.etag is not None
    assert result.etag.startswith("hn:last=")
    for item in result.items:
        assert item.metadata["signal_class"] == "COMMUNITY_ACTIVITY"
        assert item.source_reference.startswith("https://news.ycombinator.com/item?id=")
        assert item.category_hint is not None
        assert item.category_hint.value != "other"
