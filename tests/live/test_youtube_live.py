"""Optional live tests against the real YouTube Data API.

These consume real quota and are NEVER part of the default run: the ``live`` marker is
deselected in pyproject.toml, and the tests additionally skip unless both
``TREND_AGENT_RUN_LIVE_TESTS=1`` and ``TREND_AGENT_YOUTUBE_API_KEY`` are set.

Run explicitly with:
    TREND_AGENT_RUN_LIVE_TESTS=1 TREND_AGENT_YOUTUBE_API_KEY=... pytest -m live tests/live

Approximate cost: 2 units from the default bucket + 1 search.list call.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from tests.helpers import LIVE_API_KEY, RUN_LIVE, make_settings
from trend_agent.providers.base import RequestContext
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaManager
from trend_agent.providers.youtube import YouTubeDataApiProvider, youtube_quota_policy
from trend_agent.schemas.provider import PopularVideosRequest, VideoSearchRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (RUN_LIVE and LIVE_API_KEY),
        reason="live tests need TREND_AGENT_RUN_LIVE_TESTS=1 and TREND_AGENT_YOUTUBE_API_KEY",
    ),
]


def live_provider() -> YouTubeDataApiProvider:
    assert LIVE_API_KEY is not None
    return YouTubeDataApiProvider(
        api_key=SecretStr(LIVE_API_KEY),
        http_client=httpx.AsyncClient(timeout=20),
        quota=QuotaManager(youtube_quota_policy(make_settings()), InMemoryQuotaStore()),
        retry=RetryPolicy(max_attempts=2),
        max_pages=1,
        owns_client=True,
    )


async def test_live_categories_and_popular() -> None:
    provider = live_provider()
    try:
        categories = await provider.video_categories("GB", RequestContext())
        assert categories
        popular = await provider.popular_videos(
            PopularVideosRequest(region="GB", max_results=5), RequestContext()
        )
        assert 0 < len(popular.videos) <= 5
        assert all(v.collected_at.tzinfo is not None for v in popular.videos)
    finally:
        await provider.aclose()


async def test_live_search_single_page() -> None:
    provider = live_provider()
    try:
        response = await provider.search_videos(
            VideoSearchRequest(query="artificial intelligence", region="GB", max_results=5),
            RequestContext(),
        )
        assert 0 < len(response.results) <= 5
        assert response.pages_fetched == 1
    finally:
        await provider.aclose()
