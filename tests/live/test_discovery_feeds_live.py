"""Optional live test: fetch one real public release feed (PyPI) through the feed provider.

Never part of the default run (``live`` marker deselected; skipped unless
``TREND_AGENT_RUN_LIVE_TESTS=1``). Uses no credentials and no paid quota: one HTTP GET of
a public RSS feed that PyPI documents for package releases.

    TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_discovery_feeds_live.py
"""

from __future__ import annotations

import httpx
import pytest

from tests.helpers import RUN_LIVE, MutableClock
from trend_agent.discovery.models import (
    Category,
    DiscoveryRequest,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import HttpFetcher, request_quota
from trend_agent.discovery.providers.feeds import FeedDiscoveryProvider
from trend_agent.providers.base import RequestContext
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaLedger
from trend_agent.utils.time import utc_now

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not RUN_LIVE, reason="live tests need TREND_AGENT_RUN_LIVE_TESTS=1"),
]


async def test_pypi_release_feed_live() -> None:
    clock = MutableClock(utc_now())
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        provider = FeedDiscoveryProvider(
            fetcher=HttpFetcher(
                provider_name="feeds",
                client=client,
                quota=request_quota("feeds", 5, InMemoryQuotaStore(), clock),
                retry=RetryPolicy(max_attempts=2),
                clock=clock,
            ),
            user_agent="trend-agent-live-test/0.2",
            enabled=True,
            retention_days=90,
            clock=clock,
        )
        source = DiscoverySource(
            key="feed:pypi:httpx",
            provider="feeds",
            name="httpx releases",
            source_type=SourceType.FEED,
            category=Category.TECHNOLOGY,
            reference="https://pypi.org/rss/project/httpx/releases.xml",
            evidence_type=EvidenceType.CODE_RELEASE,
            entity_hint="HTTPX",
        )
        result = await provider.discover(
            DiscoveryRequest(source=source, max_items=5), RequestContext(ledger=QuotaLedger())
        )
    assert result.items
    assert all(i.evidence_type is EvidenceType.CODE_RELEASE for i in result.items)
    assert all(i.source_reference.startswith("https://pypi.org/") for i in result.items)
