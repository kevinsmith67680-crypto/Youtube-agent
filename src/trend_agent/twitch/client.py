"""Read-only client for the Twitch Helix API (public games and streams endpoints).

Endpoints (https://dev.twitch.tv/docs/api/reference/):
``GET /games/top`` (``first`` <= 100, ``after``), ``GET /games`` (up to 100 ``id`` / ``name``
/ ``igdb_id`` values), ``GET /streams`` (``game_id`` and ``language`` up to 100 each,
``first`` <= 100, ``after``).

Every request carries ``Client-Id`` and ``Authorization: Bearer <app token>``.

* Rate limits: Twitch's token bucket is reported in ``Ratelimit-Limit``,
  ``Ratelimit-Remaining`` and ``Ratelimit-Reset`` (Unix epoch). When the remaining points fall
  to the configured reserve the client waits for the reset (if it is soon) or stops with
  ``RateLimitedError``; a 429 is retried after the reset when that is within the retry
  policy, otherwise reported. A local daily request budget caps usage on top.
* A 401 from Helix drops the cached app token and retries once with a new one; a second
  401 is reported as invalid credentials.
* Pagination: cursors are followed forward only, up to a page limit. Get Streams is ordered
  by viewers and re-orders while it is read, so duplicates are dropped by stream ID and
  streams can be missed; every collection records whether it is complete (cursor
  exhausted, no errors), partial (page limit or an error after some pages) or unknown.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trend_agent.logging import get_logger
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
from trend_agent.twitch.auth import AppTokenManager
from trend_agent.twitch.models import (
    PROVIDER_NAME,
    TwitchGame,
    TwitchStream,
    parse_game,
    parse_stream,
)
from trend_agent.utils.time import Clock

BUCKET = "requests"
EP_TOP_GAMES = "top_games"
EP_GAMES = "games"
EP_STREAMS = "streams"
MAX_BYTES = 5_000_000
MAX_FILTER_VALUES = 100  # documented maximum of id/name/game_id/language values

_log = get_logger(__name__)


def twitch_quota_policy(daily_budget: int) -> QuotaPolicy:
    return QuotaPolicy(
        provider_name=PROVIDER_NAME,
        reset_timezone="UTC",
        buckets={BUCKET: QuotaBucket(BUCKET, daily_budget, 0.0)},
        endpoint_costs={ep: EndpointCost(BUCKET, 1) for ep in (EP_TOP_GAMES, EP_GAMES, EP_STREAMS)},
    )


@dataclass
class RateLimitState:
    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    observed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass
class Collection:
    """What one paged collection returned and how complete it is."""

    pages_requested: int
    page_size: int
    pages_fetched: int = 0
    exhausted: bool = False  # the API's pagination object came back empty
    duplicates: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    error: ProviderError | None = None
    unknown_pagination: bool = False
    count_reached: bool = False  # stopped because the requested top-N was read in full

    @property
    def status(self) -> str:
        if self.unknown_pagination:
            return "unknown"
        if self.exhausted and self.error is None:
            return "complete"
        return "partial"

    @property
    def reason(self) -> str:
        if self.unknown_pagination:
            return "pagination_missing_or_malformed"
        if self.error is not None:
            return f"error_after_{self.pages_fetched}_pages:{self.error.error_type}"
        if self.count_reached:
            return "requested_count_read"
        if self.exhausted:
            return "cursor_exhausted"
        return "page_limit_reached"


@dataclass
class TopGames(Collection):
    ranked: list[tuple[int, TwitchGame]] = field(default_factory=list)


@dataclass
class Streams(Collection):
    streams: list[TwitchStream] = field(default_factory=list)
    game_ids: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()

    @property
    def viewer_sum(self) -> int:
        return sum(s.viewer_count for s in self.streams)


class _TokenRejectedError(ProviderError):
    """Internal: Helix returned 401; renew the token and try once more."""

    error_type = "token_rejected"


class TwitchClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        tokens: AppTokenManager,
        quota: QuotaManager,
        retry: RetryPolicy,
        clock: Clock,
        base_url: str = "https://api.twitch.tv/helix",
        user_agent: str = "youtube-trend-agent",
        sleep: Sleep = asyncio.sleep,
        max_concurrency: int = 4,
        page_size: int = 100,
        rate_limit_reserve: int = 20,
        max_rate_limit_wait: float = 15.0,
    ) -> None:
        self._http = http_client
        self.tokens = tokens
        self.quota = quota
        self._retry = retry
        self._clock = clock
        self._base = base_url.rstrip("/")
        self._ua = user_agent
        self._sleep = sleep
        self._max_concurrency = max_concurrency
        self.page_size = max(1, min(100, page_size))
        self._reserve = rate_limit_reserve
        self._max_wait = max_rate_limit_wait
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self.rate = RateLimitState()
        self.requests_sent = 0
        self.last_error: ProviderError | None = None
        self.last_error_at: datetime | None = None
        self.last_success_at: datetime | None = None

    @property
    def base_url(self) -> str:
        return self._base

    def _semaphore(self) -> asyncio.Semaphore:
        loop = id(asyncio.get_running_loop())
        if loop not in self._semaphores:
            self._semaphores = {loop: asyncio.Semaphore(self._max_concurrency)}
        return self._semaphores[loop]

    # ------------------------------------------------------------------ core GET
    async def _respect_bucket(self) -> None:
        r = self.rate
        if r.remaining is None or r.reset_at is None or r.remaining > self._reserve:
            return
        wait = (r.reset_at - self._clock()).total_seconds()
        if wait <= 0:
            return
        if wait > self._max_wait:
            raise RateLimitedError(
                f"Twitch rate-limit bucket nearly empty ({r.remaining} points left); "
                f"resets in {wait:.0f} s",
                provider=PROVIDER_NAME,
                retry_after=wait,
                reason="bucket_reserve",
            )
        await self._sleep(wait)

    def _track(self, response: httpx.Response) -> None:
        h = response.headers
        try:
            limit = int(h["ratelimit-limit"]) if "ratelimit-limit" in h else None
            remaining = int(h["ratelimit-remaining"]) if "ratelimit-remaining" in h else None
            reset = int(h["ratelimit-reset"]) if "ratelimit-reset" in h else None
        except ValueError:
            return
        if limit is None and remaining is None and reset is None:
            return
        self.rate = RateLimitState(
            limit=limit,
            remaining=remaining,
            reset_at=datetime.fromtimestamp(reset, tz=UTC) if reset else None,
            observed_at=self._clock(),
        )

    async def _get(
        self, path: str, params: Sequence[tuple[str, str]], ctx: RequestContext, endpoint: str
    ) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path:
            raise InvalidRequestError(f"invalid API path {path!r}", provider=PROVIDER_NAME)
        url = f"{self._base}{path}"

        async def attempt() -> dict[str, Any]:
            await self._respect_bucket()
            self.quota.reserve(endpoint, ctx.ledger)
            token = await self.tokens.bearer()
            async with self._semaphore():
                self.requests_sent += 1
                try:
                    response = await self._http.get(
                        url,
                        params=list(params),
                        headers={
                            "Client-Id": self.tokens.client_id,
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "User-Agent": self._ua,
                        },
                    )
                except httpx.ProxyError:
                    raise AccessRestrictedError(
                        "network policy denied access to api.twitch.tv",
                        provider=PROVIDER_NAME,
                        reason="network_policy",
                    ) from None
                except httpx.TimeoutException as exc:
                    raise ProviderTimeoutError(
                        f"Twitch request timed out ({type(exc).__name__})", provider=PROVIDER_NAME
                    ) from None
                except httpx.TransportError as exc:
                    raise TemporarilyUnavailableError(
                        f"network error reaching api.twitch.tv ({type(exc).__name__})",
                        provider=PROVIDER_NAME,
                        reason="network",
                    ) from None
            self._track(response)
            return self._handle(response)

        try:
            try:
                data = await call_with_retries(
                    attempt, self._retry, op_name=f"twitch.{endpoint}", sleep=self._sleep
                )
            except _TokenRejectedError:
                # expired or revoked app token: replace it once, then retry
                self.tokens.invalidate("helix_401")
                try:
                    data = await call_with_retries(
                        attempt, self._retry, op_name=f"twitch.{endpoint}", sleep=self._sleep
                    )
                except _TokenRejectedError:
                    self.tokens.invalidate("helix_401_after_renewal")
                    raise InvalidCredentialsError(
                        "Twitch rejected a freshly issued app token (HTTP 401)",
                        provider=PROVIDER_NAME,
                        http_status=401,
                        reason="token_rejected",
                    ) from None
        except ProviderError as exc:
            self.last_error = exc
            self.last_error_at = self._clock()
            raise
        self.last_error = None
        self.last_error_at = None
        self.last_success_at = self._clock()
        return data

    def _handle(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        common: dict[str, Any] = {"provider": PROVIDER_NAME, "http_status": status}
        if len(response.content) > MAX_BYTES:
            raise MalformedResponseError(f"response larger than {MAX_BYTES} bytes", **common)
        if status == 200:
            try:
                body = response.json()
            except ValueError:
                raise MalformedResponseError(
                    "response is not valid JSON", reason="schema_changed", **common
                ) from None
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise MalformedResponseError(
                    "response has no 'data' list", reason="schema_changed", **common
                )
            return body
        message = ""
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                message = payload["message"][:200]
        except ValueError:
            pass
        if status == 401:
            raise _TokenRejectedError("Twitch returned 401 for the app token", **common)
        if status == 429:
            reset = self.rate.reset_at
            wait = (reset - self._clock()).total_seconds() if reset else None
            raise RateLimitedError(
                "Twitch rate limit exceeded (HTTP 429)" + (f": {message}" if message else ""),
                retry_after=max(1.0, wait) if wait is not None else None,
                **common,
            )
        if status == 403:
            raise AccessRestrictedError(f"HTTP 403 from Twitch: {message}", **common)
        if status >= 500:
            raise TemporarilyUnavailableError(f"Twitch server error (HTTP {status})", **common)
        raise InvalidRequestError(f"HTTP {status} from Twitch: {message}", **common)

    @staticmethod
    def _cursor(body: dict[str, Any]) -> tuple[str | None, bool]:
        """(cursor, understood). An empty pagination object means there are no more pages."""
        pagination = body.get("pagination")
        if pagination is None:
            return None, True  # some lists omit it on the last page
        if not isinstance(pagination, dict):
            return None, False
        cursor = pagination.get("cursor")
        if cursor is None or cursor == "":
            return None, True
        return (cursor, True) if isinstance(cursor, str) else (None, False)

    # ------------------------------------------------------------------ endpoints
    async def top_games(self, ctx: RequestContext, *, count: int = 100) -> TopGames:
        """Get Top Games, ranked across pages (1 = most viewed). Positions only: the
        endpoint returns no viewer numbers, so none are derived from it."""
        count = max(1, count)
        pages = -(-count // self.page_size)
        result = TopGames(pages_requested=pages, page_size=self.page_size)
        seen: set[str] = set()
        cursor: str | None = None
        while result.pages_fetched < pages and len(result.ranked) < count:
            params = [("first", str(min(self.page_size, count - len(result.ranked))))]
            if cursor:
                params.append(("after", cursor))
            try:
                body = await self._get("/games/top", params, ctx, EP_TOP_GAMES)
            except ProviderError as exc:
                if result.pages_fetched == 0:
                    raise
                result.error = exc
                break
            result.pages_fetched += 1
            for raw in body["data"]:
                game, problem = parse_game(raw)
                if game is None:
                    result.skipped[problem or "invalid"] += 1
                    continue
                if game.id in seen:
                    result.duplicates += 1
                    continue
                seen.add(game.id)
                result.ranked.append((len(result.ranked) + 1, game))
                if len(result.ranked) >= count:
                    break
            cursor, understood = self._cursor(body)
            if not understood:
                result.unknown_pagination = True
                break
            if cursor is None or not body["data"]:
                result.exhausted = True
                break
        if len(result.ranked) >= count and result.error is None and not result.unknown_pagination:
            # the requested top-N was read in full: complete *for that N*
            result.exhausted = True
            result.count_reached = True
        return result

    async def games(
        self,
        ctx: RequestContext,
        *,
        ids: Sequence[str] = (),
        names: Sequence[str] = (),
        igdb_ids: Sequence[str] = (),
    ) -> tuple[list[TwitchGame], Counter[str]]:
        """Get Games by ID, exact name or IGDB ID (chunks of at most 100 values)."""
        values = [("id", v) for v in dict.fromkeys(i for i in ids if i and i.strip())]
        values += [("name", v) for v in dict.fromkeys(n for n in names if n and n.strip())]
        values += [("igdb_id", v) for v in dict.fromkeys(i for i in igdb_ids if i and i.strip())]
        if not values:
            raise InvalidRequestError(
                "Get Games needs an id, name or igdb_id", provider=PROVIDER_NAME
            )
        out: dict[str, TwitchGame] = {}
        skipped: Counter[str] = Counter()
        for start in range(0, len(values), MAX_FILTER_VALUES):
            body = await self._get(
                "/games", values[start : start + MAX_FILTER_VALUES], ctx, EP_GAMES
            )
            for raw in body["data"]:
                game, problem = parse_game(raw)
                if game is None:
                    skipped[problem or "invalid"] += 1
                else:
                    out.setdefault(game.id, game)
        return list(out.values()), skipped

    async def streams(
        self,
        ctx: RequestContext,
        *,
        game_ids: Sequence[str] = (),
        languages: Sequence[str] = (),
        max_pages: int = 1,
        first: int | None = None,
    ) -> Streams:
        """Get Streams (live only), most-viewed first, following cursors up to ``max_pages``.

        Duplicates across pages (the list re-orders as viewers come and go) are dropped by
        stream ID; the first occurrence is kept.
        """
        gids = tuple(dict.fromkeys(g for g in game_ids if g and g.strip()))
        langs = tuple(dict.fromkeys(lang.lower() for lang in languages if lang and lang.strip()))
        if len(gids) > MAX_FILTER_VALUES or len(langs) > MAX_FILTER_VALUES:
            raise InvalidRequestError(
                "at most 100 game_id / language values", provider=PROVIDER_NAME
            )
        size = max(1, min(100, first or self.page_size))
        result = Streams(
            pages_requested=max(1, max_pages), page_size=size, game_ids=gids, languages=langs
        )
        seen: set[str] = set()
        cursor: str | None = None
        while result.pages_fetched < result.pages_requested:
            params: list[tuple[str, str]] = [("first", str(size)), ("type", "live")]
            params += [("game_id", g) for g in gids]
            params += [("language", lang) for lang in langs]
            if cursor:
                params.append(("after", cursor))
            try:
                body = await self._get("/streams", params, ctx, EP_STREAMS)
            except ProviderError as exc:
                if result.pages_fetched == 0:
                    raise
                result.error = exc
                break
            result.pages_fetched += 1
            for raw in body["data"]:
                stream, problem = parse_stream(raw)
                if stream is None:
                    result.skipped[problem or "invalid"] += 1
                    continue
                if stream.id in seen:
                    result.duplicates += 1
                    continue
                seen.add(stream.id)
                result.streams.append(stream)
            cursor, understood = self._cursor(body)
            if not understood:
                result.unknown_pagination = True
                break
            if cursor is None or not body["data"]:
                result.exhausted = True
                break
        return result

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        return {
            "requests_sent": self.requests_sent,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "rate_limit": self.rate.as_dict(),
            "token": self.tokens.state(),
        }


def build_client(
    *,
    http_client: httpx.AsyncClient,
    quota_store: QuotaStore,
    clock: Clock,
    retry: RetryPolicy,
    client_id: Any,
    client_secret: Any,
    helix_base_url: str,
    auth_base_url: str,
    user_agent: str,
    daily_budget: int,
    max_concurrency: int,
    page_size: int,
    rate_limit_reserve: int,
    max_rate_limit_wait: float,
    token_refresh_margin_seconds: int,
    token_validate_interval_seconds: int,
) -> TwitchClient:
    tokens = AppTokenManager(
        http_client=http_client,
        client_id=client_id,
        client_secret=client_secret,
        clock=clock,
        retry=retry,
        auth_base_url=auth_base_url,
        user_agent=user_agent,
        refresh_margin=timedelta(seconds=token_refresh_margin_seconds),
        validate_interval=timedelta(seconds=token_validate_interval_seconds),
    )
    return TwitchClient(
        http_client=http_client,
        tokens=tokens,
        quota=QuotaManager(twitch_quota_policy(daily_budget), quota_store, clock),
        retry=retry,
        clock=clock,
        base_url=helix_base_url,
        user_agent=user_agent,
        max_concurrency=max_concurrency,
        page_size=page_size,
        rate_limit_reserve=rate_limit_reserve,
        max_rate_limit_wait=max_rate_limit_wait,
    )
