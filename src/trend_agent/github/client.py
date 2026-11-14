"""Read-only GitHub REST API client.

* **Authentication.** ``Authorization: Bearer <token>`` when a token is configured,
  otherwise unauthenticated public access. The token is held as a ``SecretStr``, sent
  only in that header and never logged, stored, or returned by any method.
* **Versioning.** ``Accept: application/vnd.github+json`` and
  ``X-GitHub-Api-Version`` (configurable, default 2026-03-10).
* **Read-only.** Only ``GET`` is implemented; no write scope is ever needed.
* **Rate limits.** ``x-ratelimit-*`` headers are tracked per resource (``core``,
  ``search``). Calls stop *before* the live limit is exhausted. A primary limit (403/429
  with ``x-ratelimit-remaining: 0``) waits for ``x-ratelimit-reset``; a secondary limit
  honours ``retry-after`` (or waits at least a minute). Long waits are never slept
  through: the call fails with ``RateLimitedError`` and the scheduler retries later.
* **Local budgets.** Every attempt reserves from a persisted daily budget per bucket
  (``core``, ``search``) set well below GitHub's own limits.
* **Conditional requests.** ETags are kept per URL; a 304 reuses the cached body.
* **Deduplication.** Identical concurrent requests share one HTTP call, and identical
  requests within ``dedup_ttl`` reuse the previous response.
* **Bounded pagination.** ``Link: rel="next"`` is followed up to ``max_pages`` and only
  on the API host.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import SecretStr

from trend_agent.github.models import (
    GitHubRelease,
    GitHubRepository,
    RepositorySearchPage,
    parse_items,
)
from trend_agent.logging import get_logger, log_event
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.http import RetryPolicy, Sleep, call_with_retries
from trend_agent.providers.quota import (
    EndpointCost,
    QuotaBucket,
    QuotaManager,
    QuotaPolicy,
    QuotaStore,
)
from trend_agent.utils.time import Clock

PROVIDER_NAME = "github"
API_VERSIONS = ("2026-03-10", "2022-11-28")
DEFAULT_API_VERSION = "2026-03-10"
MAX_BYTES = 5_000_000

# Endpoint names used for local budgets and rate-limit resources.
EP_SEARCH = "search.repositories"
EP_REPO = "repos.get"
EP_RELEASES = "repos.releases"
EP_RATE_LIMIT = "rate_limit"
BUCKET_CORE = "core"
BUCKET_SEARCH = "search"
BUCKET_META = "meta"  # GET /rate_limit does not count against GitHub's limit
_RESOURCE = {EP_SEARCH: "search", EP_REPO: "core", EP_RELEASES: "core", EP_RATE_LIMIT: "meta"}

# owner: GitHub login rules; repo: no "." / ".." segments (no path traversal).
REPO_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$")
_LINK = re.compile(r'<([^>]+)>;\s*rel="([a-z]+)"')
_SECONDARY = re.compile(r"secondary rate limit|abuse detection", re.IGNORECASE)
MAX_QUERY_CHARS = 256  # GitHub search: queries longer than 256 characters are rejected

_log = get_logger(__name__)


def github_quota_policy(core_daily: int, search_daily: int) -> QuotaPolicy:
    return QuotaPolicy(
        provider_name=PROVIDER_NAME,
        reset_timezone="UTC",
        buckets={
            BUCKET_CORE: QuotaBucket(BUCKET_CORE, core_daily, 0.0),
            BUCKET_SEARCH: QuotaBucket(BUCKET_SEARCH, search_daily, 0.0),
            BUCKET_META: QuotaBucket(BUCKET_META, 1000, 0.0),
        },
        endpoint_costs={
            EP_SEARCH: EndpointCost(BUCKET_SEARCH, 1),
            EP_REPO: EndpointCost(BUCKET_CORE, 1),
            EP_RELEASES: EndpointCost(BUCKET_CORE, 1),
            EP_RATE_LIMIT: EndpointCost(BUCKET_META, 1),
        },
    )


@dataclass(frozen=True)
class RateLimitState:
    """GitHub's own view of a rate-limit resource, from the latest response headers."""

    resource: str
    limit: int
    remaining: int
    used: int
    reset_at: datetime
    observed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "limit": self.limit,
            "remaining": self.remaining,
            "used": self.used,
            "reset_at": self.reset_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass
class GitHubResponse:
    status: int
    data: Any
    etag: str | None = None
    not_modified: bool = False
    from_cache: bool = False
    next_url: str | None = None
    resource: str | None = None


@dataclass
class SearchResult:
    query: str
    total_count: int
    incomplete_results: bool
    repositories: list[GitHubRepository]
    pages_fetched: int
    more_available: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReleasesResult:
    repository: str
    releases: list[GitHubRelease]
    not_modified: bool
    etag: str | None
    warnings: list[str] = field(default_factory=list)


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - now).total_seconds())
        except (TypeError, ValueError):
            return None


class GitHubClient:
    """One instance per process; shared by the discovery provider and the CLI."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        token: SecretStr | None,
        quota: QuotaManager,
        retry: RetryPolicy,
        clock: Clock,
        base_url: str = "https://api.github.com",
        api_version: str = DEFAULT_API_VERSION,
        user_agent: str = "youtube-trend-agent",
        sleep: Sleep = asyncio.sleep,
        max_pages: int = 2,
        min_remaining: Mapping[str, int] | None = None,
        dedup_ttl: timedelta = timedelta(minutes=10),
        cache_size: int = 512,
    ) -> None:
        if api_version not in API_VERSIONS:
            raise ValueError(f"unsupported GitHub API version {api_version!r}")
        self._http = http_client
        self._token = token
        self.quota = quota
        self._retry = retry
        self._clock = clock
        self._base = base_url.rstrip("/")
        self._host = urlsplit(self._base).hostname
        self.api_version = api_version
        self._ua = user_agent
        self._sleep = sleep
        self.max_pages = max(1, max_pages)
        self._min_remaining = dict(min_remaining or {"core": 25, "search": 2})
        self._dedup_ttl = dedup_ttl
        self._cache_size = cache_size
        self._etags: OrderedDict[str, tuple[str, Any]] = OrderedDict()
        self._recent: OrderedDict[str, tuple[datetime, GitHubResponse]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[GitHubResponse]] = {}
        self.limits: dict[str, RateLimitState] = {}
        self._blocked_until: dict[str, datetime] = {}
        self.credential_state = "configured" if token is not None else "absent"
        self.last_error: ProviderError | None = None
        self.requests_sent = 0

    # ------------------------------------------------------------------ properties
    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def auth_mode(self) -> str:
        """``authenticated`` / ``unauthenticated`` / ``invalid_credentials`` (no secrets)."""
        if self.credential_state == "invalid":
            return "invalid_credentials"
        return "authenticated" if self._token is not None else "unauthenticated"

    def headers(self, etag: str | None = None) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": self._ua,
        }
        if self._token is not None:
            h["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        if etag:
            h["If-None-Match"] = etag
        return h

    def rate_limit_snapshot(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "auth_mode": self.auth_mode(),
            "api_version": self.api_version,
            "resources": {k: v.as_dict() for k, v in sorted(self.limits.items())},
            "blocked_until": {
                k: v.isoformat() for k, v in sorted(self._blocked_until.items()) if v > now
            },
            "requests_sent": self.requests_sent,
        }

    def rate_limit_ok(self, resource: str) -> tuple[bool, str]:
        """No network I/O: is the live limit (as last observed) far enough from zero?"""
        now = self._clock()
        until = self._blocked_until.get(resource)
        if until and until > now:
            return False, f"GitHub {resource} rate limit: paused until {until.isoformat()}"
        state = self.limits.get(resource)
        if (
            state
            and state.reset_at > now
            and state.remaining <= self._min_remaining.get(resource, 0)
        ):
            return False, (
                f"GitHub {resource} rate limit: {state.remaining}/{state.limit} left, "
                f"resets {state.reset_at.isoformat()}"
            )
        return True, ""

    # ------------------------------------------------------------------ core GET
    def _url(self, path: str, params: Mapping[str, str] | None) -> str:
        if not path.startswith("/") or ".." in path or "//" in path:
            raise InvalidRequestError(f"invalid API path {path!r}", provider=PROVIDER_NAME)
        query = f"?{urlencode(sorted((params or {}).items()))}" if params else ""
        return f"{self._base}{path}{query}"

    async def get(
        self,
        path: str,
        ctx: RequestContext,
        *,
        endpoint: str,
        params: Mapping[str, str] | None = None,
        etag: str | None = None,
        url: str | None = None,
    ) -> GitHubResponse:
        """GET with dedup, conditional request, budgets, rate limits and typed errors."""
        full = url or self._url(path, params)
        if url is not None and urlsplit(url).hostname != self._host:
            raise InvalidRequestError("refusing to follow a link off the API host")
        now = self._clock()
        recent = self._recent.get(full)
        if recent and now - recent[0] < self._dedup_ttl:
            cached = recent[1]
            return GitHubResponse(
                cached.status, cached.data, cached.etag, cached.not_modified, True,
                cached.next_url, cached.resource,
            )  # fmt: skip
        pending = self._inflight.get(full)
        if pending is not None:
            return await asyncio.shield(pending)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GitHubResponse] = loop.create_future()
        self._inflight[full] = future
        try:
            response = await self._get_uncached(full, ctx, endpoint=endpoint, etag=etag)
            future.set_result(response)
            self._remember(self._recent, full, (self._clock(), response))
            return response
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved: waiters re-raise it themselves
            raise
        finally:
            self._inflight.pop(full, None)

    async def _get_uncached(
        self, url: str, ctx: RequestContext, *, endpoint: str, etag: str | None
    ) -> GitHubResponse:
        resource = _RESOURCE.get(endpoint, "core")
        cached = self._etags.get(url)
        conditional = etag or (cached[0] if cached else None)

        async def attempt() -> GitHubResponse:
            ok, why = self.rate_limit_ok(resource)
            if not ok:
                until = self._blocked_until.get(resource) or self.limits[resource].reset_at
                wait = max(0.0, (until - self._clock()).total_seconds())
                raise RateLimitedError(
                    why, provider=PROVIDER_NAME, retry_after=wait, reason="rate_limit_reserve"
                )
            self.quota.reserve(endpoint, ctx.ledger)
            try:
                self.requests_sent += 1
                response = await self._http.get(url, headers=self.headers(conditional))
            except httpx.ProxyError:
                raise AccessRestrictedError(
                    f"network policy denied access to {self._host}",
                    provider=PROVIDER_NAME,
                    reason="network_policy",
                ) from None
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    f"GitHub request timed out ({type(exc).__name__})", provider=PROVIDER_NAME
                ) from None
            except httpx.TransportError as exc:
                raise TemporarilyUnavailableError(
                    f"network error reaching GitHub ({type(exc).__name__})",
                    provider=PROVIDER_NAME,
                    reason="network",
                ) from None
            return self._handle(response, url, resource, cached)

        try:
            result = await call_with_retries(
                attempt, self._retry, op_name="github.get", sleep=self._sleep
            )
        except ProviderError as exc:
            self.last_error = exc
            raise
        self.last_error = None
        return result

    def _remember(self, store: OrderedDict[str, Any], key: str, value: Any) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > self._cache_size:
            store.popitem(last=False)

    def _record_limits(self, response: httpx.Response, fallback: str) -> str:
        h = response.headers
        resource = h.get("x-ratelimit-resource") or fallback
        try:
            state = RateLimitState(
                resource=resource,
                limit=int(h["x-ratelimit-limit"]),
                remaining=int(h["x-ratelimit-remaining"]),
                used=int(h.get("x-ratelimit-used", "0")),
                reset_at=datetime.fromtimestamp(int(h["x-ratelimit-reset"]), tz=UTC),
                observed_at=self._clock(),
            )
        except (KeyError, ValueError):
            return resource
        self.limits[resource] = state
        return resource

    def _handle(
        self,
        response: httpx.Response,
        url: str,
        resource: str,
        cached: tuple[str, Any] | None,
    ) -> GitHubResponse:
        status = response.status_code
        resource = self._record_limits(response, resource)
        now = self._clock()
        if len(response.content) > MAX_BYTES:
            raise MalformedResponseError(
                f"GitHub response larger than {MAX_BYTES} bytes", provider=PROVIDER_NAME
            )
        next_url = None
        for target, rel in _LINK.findall(response.headers.get("link", "")):
            if rel == "next":
                next_url = target
        if status == 304:
            return GitHubResponse(
                304, cached[1] if cached else None, cached[0] if cached else None,
                not_modified=True, next_url=next_url, resource=resource,
            )  # fmt: skip
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError:
                raise MalformedResponseError(
                    "GitHub response is not valid JSON",
                    provider=PROVIDER_NAME,
                    http_status=status,
                    reason="schema_changed",
                ) from None
            etag = response.headers.get("etag")
            if etag:
                self._remember(self._etags, url, (etag, data))
            if self.credential_state == "invalid":
                self.credential_state = "configured"
            return GitHubResponse(status, data, etag, next_url=next_url, resource=resource)
        message = _message(response)
        common: dict[str, Any] = {"provider": PROVIDER_NAME, "http_status": status}
        retry_after = _parse_retry_after(response.headers.get("retry-after"), now)
        if status == 401:
            self.credential_state = "invalid" if self._token is not None else "absent"
            raise InvalidCredentialsError(
                "GitHub rejected the credentials (HTTP 401"
                + (f": {message}" if message else "")
                + "). Check GITHUB_TOKEN, or remove it to use public access.",
                reason="bad_credentials",
                **common,
            )
        if status in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                reset = response.headers.get("x-ratelimit-reset", "")
                until = (
                    datetime.fromtimestamp(int(reset), tz=UTC)
                    if reset.isdigit()
                    else now + timedelta(minutes=1)
                )
                self._blocked_until[resource] = until
                raise RateLimitedError(
                    f"GitHub primary rate limit for '{resource}' exhausted; resets "
                    f"{until.isoformat()}",
                    retry_after=max(0.0, (until - now).total_seconds()),
                    reason="primary_rate_limit",
                    **common,
                )
            if retry_after is not None or _SECONDARY.search(message) or status == 429:
                wait = retry_after if retry_after is not None else 60.0
                self._blocked_until[resource] = now + timedelta(seconds=wait)
                raise RateLimitedError(
                    f"GitHub secondary rate limit (HTTP {status}); retry after {wait:.0f}s",
                    retry_after=wait,
                    reason="secondary_rate_limit",
                    **common,
                )
            needed = response.headers.get("x-accepted-github-permissions")
            sso = response.headers.get("x-github-sso")
            reason = "sso_required" if sso else "insufficient_permissions"
            detail = f" (accepted permissions: {needed})" if needed else ""
            raise AccessRestrictedError(
                f"GitHub refused access (HTTP 403){detail}" + (f": {message}" if message else ""),
                reason=reason,
                **common,
            )
        if status == 404:
            raise InvalidRequestError(
                "not found on GitHub (renamed, deleted, or not public)",
                reason="not_found",
                **common,
            )
        if status == 422:
            raise InvalidRequestError(
                f"GitHub rejected the request as invalid: {message or 'validation failed'}",
                reason="validation_failed",
                **common,
            )
        if status >= 500:
            raise TemporarilyUnavailableError(
                f"GitHub server error (HTTP {status})", retry_after=retry_after, **common
            )
        raise InvalidRequestError(f"unexpected HTTP {status} from GitHub", **common)

    # ------------------------------------------------------------------ endpoints
    async def rate_limit(self, ctx: RequestContext) -> dict[str, Any]:
        """``GET /rate_limit`` (does not count against the primary limit)."""
        response = await self.get("/rate_limit", ctx, endpoint=EP_RATE_LIMIT)
        data = response.data
        resources = data.get("resources") if isinstance(data, dict) else None
        if not isinstance(resources, dict) or "core" not in resources:
            raise MalformedResponseError(
                "rate_limit response has no resources.core",
                provider=PROVIDER_NAME,
                reason="schema_changed",
            )
        now = self._clock()
        out: dict[str, Any] = {}
        for name in ("core", "search"):
            r = resources.get(name)
            if isinstance(r, dict) and {"limit", "remaining", "reset"} <= r.keys():
                state = RateLimitState(
                    name, int(r["limit"]), int(r["remaining"]), int(r.get("used", 0)),
                    datetime.fromtimestamp(int(r["reset"]), tz=UTC), now,
                )  # fmt: skip
                self.limits[name] = state
                out[name] = state.as_dict()
        return out

    async def search_repositories(
        self,
        query: str,
        ctx: RequestContext,
        *,
        sort: str | None = "stars",
        order: str = "desc",
        per_page: int = 30,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> SearchResult:
        query = " ".join(query.split())
        if not query or len(query) > MAX_QUERY_CHARS:
            raise InvalidRequestError(
                f"search query must be 1-{MAX_QUERY_CHARS} characters", provider=PROVIDER_NAME
            )
        if sort not in (None, "stars", "forks", "help-wanted-issues", "updated"):
            raise InvalidRequestError(f"unsupported sort {sort!r}", provider=PROVIDER_NAME)
        params = {"q": query, "per_page": str(max(1, min(per_page, 100))), "order": order}
        if sort:
            params["sort"] = sort
        pages = max(1, min(max_pages or self.max_pages, self.max_pages))
        repos: list[GitHubRepository] = []
        warnings: list[str] = []
        total, incomplete, fetched, url = 0, False, 0, None
        seen: set[int] = set()
        while fetched < pages:
            response = await self.get(
                "/search/repositories", ctx, endpoint=EP_SEARCH, params=params, url=url
            )
            fetched += 1
            try:
                page = RepositorySearchPage.model_validate(response.data)
            except Exception:
                raise MalformedResponseError(
                    "search response does not match the documented shape",
                    provider=PROVIDER_NAME,
                    reason="schema_changed",
                ) from None
            total, incomplete = page.total_count, incomplete or page.incomplete_results
            parsed, problems = parse_items(GitHubRepository, list(page.items), what="items")
            if page.items and len(problems) > len(page.items) // 2:
                raise MalformedResponseError(
                    f"most search items are malformed ({problems[0]})",
                    provider=PROVIDER_NAME,
                    reason="schema_changed",
                )
            warnings.extend(problems)
            for repo in parsed:
                if repo.id not in seen and not repo.private:  # duplicates across pages
                    seen.add(repo.id)
                    repos.append(repo)
            url = response.next_url
            if url is None or (max_items is not None and len(repos) >= max_items):
                break
        if incomplete:
            warnings.append("GitHub marked the search results incomplete (timeout)")
        return SearchResult(
            query=query,
            total_count=total,
            incomplete_results=incomplete,
            repositories=repos[:max_items] if max_items else repos,
            pages_fetched=fetched,
            more_available=url is not None,
            warnings=warnings,
        )

    async def get_repository(
        self, full_name: str, ctx: RequestContext
    ) -> tuple[GitHubRepository | None, bool]:
        """(repository, not_modified). ``None`` only when a 304 had no cached body."""
        _check_repo(full_name)
        response = await self.get(f"/repos/{full_name}", ctx, endpoint=EP_REPO)
        if response.data is None:
            return None, response.not_modified
        parsed, problems = parse_items(GitHubRepository, [response.data], what="repository")
        if not parsed:
            raise MalformedResponseError(
                f"repository response does not match the documented shape ({problems[0]})",
                provider=PROVIDER_NAME,
                reason="schema_changed",
            )
        return parsed[0], response.not_modified

    async def list_releases(
        self,
        full_name: str,
        ctx: RequestContext,
        *,
        per_page: int = 10,
        etag: str | None = None,
    ) -> ReleasesResult:
        """Latest releases (newest first), drafts excluded. One page only (bounded)."""
        _check_repo(full_name)
        response = await self.get(
            f"/repos/{full_name}/releases",
            ctx,
            endpoint=EP_RELEASES,
            params={"per_page": str(max(1, min(per_page, 100)))},
            etag=etag,
        )
        if response.data is None:
            return ReleasesResult(full_name, [], True, response.etag or etag)
        if not isinstance(response.data, list):
            raise MalformedResponseError(
                "releases response is not a list", provider=PROVIDER_NAME, reason="schema_changed"
            )
        parsed, problems = parse_items(GitHubRelease, response.data, what="releases")
        if response.data and len(problems) > len(response.data) // 2:
            raise MalformedResponseError(
                f"most releases are malformed ({problems[0]})",
                provider=PROVIDER_NAME,
                reason="schema_changed",
            )
        releases = [r for r in parsed if not r.draft]
        return ReleasesResult(full_name, releases, response.not_modified, response.etag, problems)


def _check_repo(full_name: str) -> None:
    if not REPO_PATTERN.match(full_name):
        raise InvalidRequestError(f"invalid repository {full_name!r}", provider=PROVIDER_NAME)


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return str(data["message"])[:300]
    return ""


def build_client(
    *,
    http_client: httpx.AsyncClient,
    token: SecretStr | None,
    quota_store: QuotaStore,
    clock: Clock,
    retry: RetryPolicy,
    base_url: str,
    api_version: str,
    user_agent: str,
    core_daily: int,
    search_daily: int,
    max_pages: int,
    min_remaining: Mapping[str, int] | None = None,
    dedup_ttl: timedelta = timedelta(minutes=10),
) -> GitHubClient:
    quota = QuotaManager(github_quota_policy(core_daily, search_daily), quota_store, clock)
    log_event(_log, "github.client", auth_mode="token" if token else "public", version=api_version)
    return GitHubClient(
        http_client=http_client,
        token=token,
        quota=quota,
        retry=retry,
        clock=clock,
        base_url=base_url,
        api_version=api_version,
        user_agent=user_agent,
        max_pages=max_pages,
        min_remaining=min_remaining,
        dedup_ttl=dedup_ttl,
    )
