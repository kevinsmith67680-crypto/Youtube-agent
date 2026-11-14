"""Deterministic GitHub REST API fixtures: a scriptable fake server and builders.

No test touches the network: every GitHub request goes to :class:`FakeGitHub` through
``httpx.MockTransport``. The fake records every request so tests can assert headers,
methods and how many calls were made.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from tests.helpers import MutableClock, SleepRecorder
from trend_agent.discovery.providers.github import GitHubDiscoveryProvider
from trend_agent.github.client import GitHubClient, github_quota_policy
from trend_agent.github.config import GitHubSettings
from trend_agent.providers.http import RetryPolicy
from trend_agent.providers.quota import InMemoryQuotaStore, QuotaManager

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
API = "https://api.github.com"
TOKEN = "github_pat_TESTONLY0000000000000000000000000000"

Responder = httpx.Response | Callable[[httpx.Request], httpx.Response] | Exception


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_json(
    rid: int,
    full_name: str,
    *,
    description: str | None = "An open-source framework for building AI agents with tools.",
    topics: tuple[str, ...] = ("ai-agent", "llm"),
    stars: int = 120,
    forks: int = 8,
    created: datetime = NOW - timedelta(days=5),
    pushed: datetime = NOW - timedelta(days=1),
    language: str | None = "Python",
    size: int = 420,
    **extra: Any,
) -> dict[str, Any]:
    owner, name = full_name.split("/")
    return {
        "id": rid,
        "node_id": f"R_{rid}",
        "name": name,
        "full_name": full_name,
        "private": False,
        "owner": {"login": owner, "id": rid + 10_000, "type": "Organization"},
        "html_url": f"https://github.com/{full_name}",
        "description": description,
        "fork": False,
        "archived": False,
        "disabled": False,
        "is_template": False,
        "created_at": iso(created),
        "updated_at": iso(pushed),
        "pushed_at": iso(pushed),
        "language": language,
        "topics": list(topics),
        "stargazers_count": stars,
        "forks_count": forks,
        "open_issues_count": 3,
        "size": size,
        "homepage": None,
        "default_branch": "main",
        "license": {"spdx_id": "MIT"},
        "mirror_url": None,
        "visibility": "public",
        "score": 1.0,
        **extra,
    }


def release_json(
    rid: int,
    full_name: str,
    tag: str,
    *,
    published: datetime = NOW - timedelta(days=2),
    body: str = "## Breaking changes\n- Removed the legacy `run_sync` API\n\n## New features\n"
    "- Added streaming tool calls\n- New `Client.batch` API endpoint\n",
    prerelease: bool = False,
    draft: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": rid,
        "tag_name": tag,
        "name": name if name is not None else tag,
        "html_url": f"https://github.com/{full_name}/releases/tag/{tag}",
        "draft": draft,
        "prerelease": prerelease,
        "created_at": iso(published - timedelta(hours=1)),
        "published_at": iso(published),
        "body": body,
        "author": {"login": "releaser", "id": 5},
    }


def limits(
    resource: str, remaining: int = 4000, limit: int = 5000, reset_in: int = 3600
) -> dict[str, str]:
    return {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(limit - remaining),
        "x-ratelimit-reset": str(int(NOW.timestamp()) + reset_in),
        "x-ratelimit-resource": resource,
    }


def search_page(
    items: list[dict[str, Any]], total: int | None = None, **headers: str
) -> httpx.Response:
    body = {
        "total_count": total if total is not None else len(items),
        "incomplete_results": False,
        "items": items,
    }
    return httpx.Response(200, json=body, headers={**limits("search", 28, 30), **headers})


class FakeGitHub:
    """Queue responses per path (``/search/repositories``, ``/repos/o/r``, ...)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._queues: dict[str, list[Responder]] = defaultdict(list)
        self._default: dict[str, Responder] = {}

    def on(self, path: str, *responses: Responder) -> FakeGitHub:
        self._queues[path].extend(responses)
        return self

    def always(self, path: str, response: Responder) -> FakeGitHub:
        self._default[path] = response
        return self

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "the GitHub client must be read-only"
        self.requests.append(request)
        queue = self._queues.get(request.url.path)
        item: Responder | None = queue.pop(0) if queue else self._default.get(request.url.path)
        if item is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_client(
    fake: FakeGitHub,
    *,
    token: str | None = None,
    clock: MutableClock | None = None,
    core_daily: int = 1000,
    search_daily: int = 100,
    max_pages: int = 2,
    attempts: int = 2,
    api_version: str = "2026-03-10",
    dedup_ttl: timedelta = timedelta(0),
    sleeper: SleepRecorder | None = None,
) -> GitHubClient:
    clock = clock or MutableClock(NOW)
    return GitHubClient(
        http_client=httpx.AsyncClient(transport=fake.transport()),
        token=SecretStr(token) if token else None,
        quota=QuotaManager(
            github_quota_policy(core_daily, search_daily), InMemoryQuotaStore(), clock
        ),
        retry=RetryPolicy(
            max_attempts=attempts, base_delay=0.01, max_delay=5.0, rng=random.Random(3)
        ),
        clock=clock,
        api_version=api_version,
        user_agent="trend-agent-tests/0",
        sleep=sleeper or SleepRecorder(),
        max_pages=max_pages,
        dedup_ttl=dedup_ttl,
    )


def make_provider(
    fake: FakeGitHub,
    *,
    token: str | None = None,
    clock: MutableClock | None = None,
    api_enabled: bool = True,
    settings: GitHubSettings | None = None,
    **client_kw: Any,
) -> GitHubDiscoveryProvider:
    clock = clock or MutableClock(NOW)
    return GitHubDiscoveryProvider(
        make_client(fake, token=token, clock=clock, **client_kw),
        settings=settings or GitHubSettings(),
        enabled=True,
        api_enabled=api_enabled,
        retention_days=180,
        clock=clock,
    )
