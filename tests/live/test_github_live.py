"""Optional live tests against the real GitHub REST API.

Never part of the default run: the ``live`` marker is deselected in pyproject.toml, and the
tests skip unless ``TREND_AGENT_RUN_LIVE_TESTS=1``. A token is optional (``GITHUB_TOKEN``);
without one the public unauthenticated limits apply.

    TREND_AGENT_RUN_LIVE_TESTS=1 GITHUB_TOKEN=... pytest -m live tests/live/test_github_live.py

Cost: GET /rate_limit (free), 1 search request, 2 core requests. Set
``TREND_AGENT_GITHUB_LIVE_REPO`` (default ``ollama/ollama``) to the repository whose
releases are read.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from tests.helpers import RUN_LIVE
from trend_agent.discovery.models import Category
from trend_agent.github.analysis import analyse_release, assess_repository
from trend_agent.github.client import GitHubClient, github_quota_policy
from trend_agent.github.config import GitHubSettings
from trend_agent.providers.base import RequestContext
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaManager
from trend_agent.utils.time import utc_now

LIVE_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("TREND_AGENT_GITHUB_TOKEN")
LIVE_REPO = os.environ.get("TREND_AGENT_GITHUB_LIVE_REPO", "ollama/ollama")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not RUN_LIVE, reason="live tests need TREND_AGENT_RUN_LIVE_TESTS=1"),
]


def live_client() -> GitHubClient:
    return GitHubClient(
        http_client=httpx.AsyncClient(timeout=20),
        token=SecretStr(LIVE_TOKEN) if LIVE_TOKEN else None,
        quota=QuotaManager(github_quota_policy(10, 3), InMemoryQuotaStore(), utc_now),
        retry=RetryPolicy(max_attempts=2, rng=random.Random(0)),
        clock=utc_now,
        user_agent="youtube-trend-agent-live-test",
        max_pages=1,
    )


async def test_live_rate_limit_and_one_search() -> None:
    client = live_client()
    ctx = RequestContext()
    limits = await client.rate_limit(ctx)
    assert "core" in limits
    assert "search" in limits
    since = (datetime.now(UTC) - timedelta(days=14)).date()
    result = await client.search_repositories(
        f"topic:llm created:>={since} fork:false", ctx, per_page=5, max_pages=1
    )
    assert result.pages_fetched == 1
    assert len(result.repositories) <= 5
    now = utc_now()
    for repo in result.repositories:
        assert repo.html_url.startswith("https://github.com/")
        assert repo.created_at.date() >= since
        assess_repository(repo, Category.AI, GitHubSettings(), now)  # must not raise


async def test_live_repository_and_releases() -> None:
    client = live_client()
    ctx = RequestContext()
    repo, _ = await client.get_repository(LIVE_REPO, ctx)
    assert repo is not None
    assert repo.full_name.lower() == LIVE_REPO.lower()
    releases = await client.list_releases(LIVE_REPO, ctx, per_page=3)
    for release in releases.releases:
        assert not release.draft
        analyse_release(release)  # quoting only; never raises on real notes
