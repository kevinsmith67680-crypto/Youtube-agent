"""Test helpers shared across test modules (non-fixture code)."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from trend_agent.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
FIXED_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
TEST_API_KEY = "AIzaTESTKEYTESTKEYTESTKEYTESTKEY123"
TEST_DB_URL = os.environ.get(
    "TREND_AGENT_TEST_DATABASE_URL",
    "postgresql+psycopg://trend_agent:trend_agent_dev@127.0.0.1:5432/trend_agent_test",
)
# Captured at import, before the autouse fixture clears TREND_AGENT_* variables.
LIVE_API_KEY = os.environ.get("TREND_AGENT_YOUTUBE_API_KEY")
RUN_LIVE = os.environ.get("TREND_AGENT_RUN_LIVE_TESTS") == "1"
REQUIRE_DB = os.environ.get("TREND_AGENT_REQUIRE_DB_TESTS") == "1"


class MutableClock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


Responder = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


class FakeYouTube:
    """Scriptable YouTube API: queue responses per endpoint path; record every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._queues: dict[str, list[Responder]] = defaultdict(list)
        self._defaults: dict[str, Responder] = {}

    def queue(self, path: str, *responses: Responder) -> FakeYouTube:
        self._queues[path].extend(responses)
        return self

    def default(self, path: str, response: Responder) -> FakeYouTube:
        self._defaults[path] = response
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.rsplit("/", 1)[-1]
        if self._queues[path]:
            item = self._queues[path].pop(0)
        elif path in self._defaults:
            item = self._defaults[path]
        else:
            return json_response(404, {"error": {"code": 404, "message": f"no fake for {path}"}})
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/" + path)]


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": TEST_DB_URL,
        "database_connect_timeout_seconds": 2,
    }
    base.update(overrides)
    return Settings(**base)


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
