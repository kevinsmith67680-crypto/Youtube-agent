"""Deterministic Twitch fixtures: a scriptable fake of id.twitch.tv and the Helix API.

No test touches the network. :class:`FakeTwitch` issues app tokens, validates them, serves
``/helix/games/top``, ``/helix/games`` and ``/helix/streams`` from in-memory state (with
cursors, rate-limit headers and configurable failures) and records every request.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr

from tests.helpers import MutableClock, SleepRecorder
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaManager
from trend_agent.twitch.auth import AppTokenManager
from trend_agent.twitch.client import TwitchClient, twitch_quota_policy

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
CLIENT_ID = "testclientid0000000000000000"
CLIENT_SECRET = "testsecret00000000000000000000"  # pragma: allowlist secret
HELIX = "https://api.twitch.tv/helix"
AUTH = "https://id.twitch.tv/oauth2"

Override = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


def body(value: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(value).encode(),
        headers={"content-type": "application/json", **headers},
    )


def game(gid: str, name: str, igdb: str = "") -> dict[str, Any]:
    return {
        "id": gid,
        "name": name,
        "box_art_url": f"https://static-cdn.jtvnw.net/ttv-boxart/{gid}-{{width}}x{{height}}.jpg",
        "igdb_id": igdb,
    }


def stream(
    sid: str,
    login: str,
    gid: str,
    gname: str,
    viewers: int,
    *,
    lang: str = "en",
    title: str = "playing",
    started: datetime = NOW - timedelta(hours=2),
) -> dict[str, Any]:
    return {
        "id": sid,
        "user_id": f"u{sid}",
        "user_login": login,
        "user_name": login.title(),
        "game_id": gid,
        "game_name": gname,
        "type": "live",
        "title": title,
        "tags": None,
        "viewer_count": viewers,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "language": lang,
        "thumbnail_url": "https://static-cdn.jtvnw.net/x-{width}x{height}.jpg",
        "tag_ids": [],
        "is_mature": False,
    }


JUST_CHATTING = game("509658", "Just Chatting")
FORTNITE = game("33214", "Fortnite", "1905")
NEWGAME = game("777001", "Starfall Tactics", "424242")
LOL = game("21779", "League of Legends", "115")
MYSTERY = game("888002", "Unlisted Category")  # no IGDB id: not treated as a game


class FakeTwitch:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.top: list[dict[str, Any]] = [JUST_CHATTING, FORTNITE, LOL, MYSTERY]
        self.catalog: dict[str, dict[str, Any]] = {
            g["id"]: g for g in (JUST_CHATTING, FORTNITE, NEWGAME, LOL, MYSTERY)
        }
        self.streams: list[dict[str, Any]] = [
            stream("1", "alpha", "509658", "Just Chatting", 5000),
            stream("2", "bravo", "33214", "Fortnite", 3000),
            stream("3", "charlie", "21779", "League of Legends", 2500, lang="ko"),
            stream("4", "delta", "33214", "Fortnite", 1200, lang="de"),
            stream("5", "echo", "777001", "Starfall Tactics", 300),
        ]
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.expires_in = 5_000_000
        self.rate_remaining = 799
        self.override: dict[str, Override] = {}
        self.page_shift: int = 0  # simulate re-ordering between pages (duplicates)

    def set_viewers(self, sid: str, viewers: int) -> None:
        for s in self.streams:
            if s["id"] == sid:
                s["viewer_count"] = viewers

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def revoke_all(self) -> None:
        self.valid_tokens.clear()

    def _limits(self) -> dict[str, str]:
        return {
            "ratelimit-limit": "800",
            "ratelimit-remaining": str(self.rate_remaining),
            "ratelimit-reset": str(int(NOW.timestamp()) + 60),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.override:
            o = self.override[path]
            if isinstance(o, Exception):
                raise o
            return o(request) if callable(o) and not isinstance(o, httpx.Response) else o
        if path == "/oauth2/token":
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            # the secret travels in the form body, never in the URL
            assert "client_secret" not in str(request.url)
            if form.get("client_id") != [CLIENT_ID] or form.get("client_secret") != [CLIENT_SECRET]:
                return body({"status": 403, "message": "invalid client secret"}, 403)
            if form.get("grant_type") != ["client_credentials"]:
                return body({"status": 400, "message": "invalid grant"}, 400)
            self.tokens_issued += 1
            token = f"apptoken{self.tokens_issued:022d}"
            self.valid_tokens.add(token)
            return body(
                {"access_token": token, "expires_in": self.expires_in, "token_type": "bearer"}
            )
        if path == "/oauth2/validate":
            token = request.headers.get("authorization", "").removeprefix("OAuth ")
            if token not in self.valid_tokens:
                return body({"status": 401, "message": "invalid access token"}, 401)
            return body({"client_id": CLIENT_ID, "scopes": [], "expires_in": self.expires_in})
        assert request.method == "GET", "the Helix client must be read-only"
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.headers.get("client-id") != CLIENT_ID or token not in self.valid_tokens:
            return body(
                {"error": "Unauthorized", "status": 401, "message": "Invalid OAuth token"}, 401
            )
        self.rate_remaining = max(0, self.rate_remaining - 1)
        q = parse_qs(request.url.query.decode())
        first = int(q.get("first", ["20"])[0])
        offset = int(q.get("after", ["0"])[0])
        if path == "/helix/games/top":
            page = self.top[offset : offset + first]
            nxt = offset + first
            pag = {"cursor": str(nxt)} if nxt < len(self.top) else {}
            return body({"data": page, "pagination": pag}, **self._limits())
        if path == "/helix/games":
            out = [self.catalog[i] for i in q.get("id", []) if i in self.catalog]
            names = {n.casefold(): n for n in q.get("name", [])}
            out += [g for g in self.catalog.values() if g["name"] in names.values()]
            return body({"data": out}, **self._limits())
        if path == "/helix/streams":
            items = [s for s in self.streams if s["type"] == "live"]
            if q.get("game_id"):
                items = [s for s in items if s["game_id"] in q["game_id"]]
            if q.get("language"):
                items = [s for s in items if s["language"] in q["language"]]
            items.sort(key=lambda s: -s["viewer_count"])
            start = max(0, offset - (self.page_shift if offset else 0))
            page = items[start : start + first]
            nxt = offset + first
            pag = {"cursor": str(nxt)} if nxt < len(items) else {}
            return body({"data": page, "pagination": pag}, **self._limits())
        return body({"error": "Not Found", "status": 404, "message": ""}, 404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_client(
    fake: FakeTwitch,
    *,
    clock: MutableClock | None = None,
    client_id: str | None = CLIENT_ID,
    client_secret: str | None = CLIENT_SECRET,
    budget: int = 1000,
    attempts: int = 2,
    page_size: int = 100,
    reserve: int = 20,
    max_wait: float = 15.0,
    sleeper: SleepRecorder | None = None,
) -> TwitchClient:
    clock = clock or MutableClock(NOW)
    http = httpx.AsyncClient(transport=fake.transport())
    retry = RetryPolicy(max_attempts=attempts, base_delay=0.01, max_delay=5.0, rng=random.Random(3))
    sleeper = sleeper or SleepRecorder()
    tokens = AppTokenManager(
        http_client=http,
        client_id=SecretStr(client_id) if client_id else None,
        client_secret=SecretStr(client_secret) if client_secret else None,
        clock=clock,
        retry=retry,
        auth_base_url=AUTH,
        sleep=sleeper,
    )
    return TwitchClient(
        http_client=http,
        tokens=tokens,
        quota=QuotaManager(twitch_quota_policy(budget), InMemoryQuotaStore(), clock),
        retry=retry,
        clock=clock,
        base_url=HELIX,
        user_agent="trend-agent-tests/0",
        sleep=sleeper,
        page_size=page_size,
        rate_limit_reserve=reserve,
        max_rate_limit_wait=max_wait,
    )
