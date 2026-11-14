"""Optional live tests against the real Twitch Helix API.

Never part of the default run: the ``live`` marker is deselected in pyproject.toml, and the
tests skip unless ``TREND_AGENT_RUN_LIVE_TESTS=1`` and Twitch credentials are present
(``TWITCH_CLIENT_ID`` / ``TWITCH_CLIENT_SECRET``). The credentials are read from the
environment only and never printed.

    TREND_AGENT_RUN_LIVE_TESTS=1 TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=... \
        pytest -m live tests/live/test_twitch_live.py

Cost: 1 token request + about 6 Helix requests (bounded by a local budget of 12).
"""

from __future__ import annotations

import os
import random

import httpx
import pytest
from pydantic import SecretStr

from tests.helpers import RUN_LIVE
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore
from trend_agent.twitch.client import build_client
from trend_agent.twitch.service import connection_test
from trend_agent.utils.time import utc_now

CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not RUN_LIVE, reason="live tests need TREND_AGENT_RUN_LIVE_TESTS=1"),
    pytest.mark.skipif(not (CLIENT_ID and CLIENT_SECRET), reason="Twitch credentials not set"),
]


def live_client(http: httpx.AsyncClient) -> object:
    return build_client(
        http_client=http,
        quota_store=InMemoryQuotaStore(),
        clock=utc_now,
        retry=RetryPolicy(max_attempts=2, rng=random.Random(0)),
        client_id=SecretStr(CLIENT_ID or ""),
        client_secret=SecretStr(CLIENT_SECRET or ""),
        helix_base_url="https://api.twitch.tv/helix",
        auth_base_url="https://id.twitch.tv/oauth2",
        user_agent="youtube-trend-agent-live-test",
        daily_budget=12,
        max_concurrency=2,
        page_size=100,
        rate_limit_reserve=20,
        max_rate_limit_wait=5.0,
        token_refresh_margin_seconds=3600,
        token_validate_interval_seconds=3600,
    )


async def test_live_token_top_games_streams_and_fortnite() -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        client = live_client(http)
        report = await connection_test(client)  # type: ignore[arg-type]
    assert report["error"] is None, report.get("error_detail")
    assert report["ok"] is True
    assert set(report["steps"].values()) == {"ok"}
    assert report["token"]["validation"] == "valid"
    assert len(report["top_games"]) == 20
    assert [g["rank"] for g in report["top_games"]] == list(range(1, 21))
    assert report["fortnite"]["game_id"] == "33214"
    assert report["rate_limit"]["limit"] is not None
    flat = repr(report)
    assert (CLIENT_SECRET or "x" * 30) not in flat
