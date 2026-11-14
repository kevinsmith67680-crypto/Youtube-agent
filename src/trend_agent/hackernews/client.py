"""Read-only client for the official Hacker News Firebase API.

Endpoints (documented at https://github.com/HackerNews/API):
``/{newstories,topstories,beststories,showstories,askstories}.json``, ``/item/{id}.json``,
``/updates.json`` and ``/maxitem.json``. No authentication. The API documents no rate
limit; this client still bounds itself: a local daily request budget, bounded
concurrency, short TTL caches with in-flight deduplication, timeouts, and retry with
jittered backoff for transient failures only.

There is no keyword search in this API: discovery reads the official story lists and
fetches the item records it needs.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from trend_agent.hackernews.models import LISTS, HNItem, parse_item
from trend_agent.logging import get_logger
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
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

PROVIDER_NAME = "hackernews_firebase"
BUCKET = "requests"
EP_LIST = "list"
EP_ITEM = "item"
EP_UPDATES = "updates"
EP_MAXITEM = "maxitem"
MAX_BYTES = 2_000_000

_log = get_logger(__name__)


def hn_quota_policy(daily_budget: int) -> QuotaPolicy:
    return QuotaPolicy(
        provider_name=PROVIDER_NAME,
        reset_timezone="UTC",
        buckets={BUCKET: QuotaBucket(BUCKET, daily_budget, 0.0)},
        endpoint_costs={
            ep: EndpointCost(BUCKET, 1) for ep in (EP_LIST, EP_ITEM, EP_UPDATES, EP_MAXITEM)
        },
    )


@dataclass
class ItemBatch:
    """Items fetched for a set of IDs; failures are per item, never all-or-nothing."""

    items: dict[int, HNItem] = field(default_factory=dict)
    skipped: dict[int, str] = field(default_factory=dict)  # id -> reason (missing/dead/...)
    failed: dict[int, str] = field(default_factory=dict)  # id -> error type


class HackerNewsClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        quota: QuotaManager,
        retry: RetryPolicy,
        clock: Clock,
        base_url: str = "https://hacker-news.firebaseio.com/v0",
        user_agent: str = "youtube-trend-agent",
        sleep: Sleep = asyncio.sleep,
        max_concurrency: int = 6,
        item_cache: timedelta = timedelta(minutes=2),
        list_cache: timedelta = timedelta(minutes=1),
        cache_size: int = 2048,
    ) -> None:
        self._http = http_client
        self.quota = quota
        self._retry = retry
        self._clock = clock
        self._base = base_url.rstrip("/")
        self._ua = user_agent
        self._sleep = sleep
        self._max_concurrency = max_concurrency
        self._semaphores: dict[int, asyncio.Semaphore] = {}  # per event loop
        self._item_ttl = item_cache
        self._list_ttl = list_cache
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[datetime, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self.requests_sent = 0
        self.last_error: ProviderError | None = None
        self.last_error_at: datetime | None = None
        self.last_success_at: datetime | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        loop = id(asyncio.get_running_loop())
        if loop not in self._semaphores:
            self._semaphores = {loop: asyncio.Semaphore(self._max_concurrency)}
        return self._semaphores[loop]

    # ------------------------------------------------------------------ core GET
    async def get_json(self, path: str, ctx: RequestContext, *, endpoint: str) -> Any:
        if not path.startswith("/") or ".." in path or not path.endswith(".json"):
            raise InvalidRequestError(f"invalid API path {path!r}", provider=PROVIDER_NAME)
        ttl = self._item_ttl if endpoint == EP_ITEM else self._list_ttl
        now = self._clock()
        cached = self._cache.get(path)
        if cached and now - cached[0] < ttl:
            return cached[1]
        pending = self._inflight.get(path)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[path] = future
        try:
            data = await self._get(path, ctx, endpoint)
            future.set_result(data)
            self._cache[path] = (self._clock(), data)
            self._cache.move_to_end(path)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return data
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # retrieved: waiters re-raise it themselves
            raise
        finally:
            self._inflight.pop(path, None)

    async def _get(self, path: str, ctx: RequestContext, endpoint: str) -> Any:
        url = f"{self._base}{path}"

        async def attempt() -> Any:
            self.quota.reserve(endpoint, ctx.ledger)
            async with self._semaphore():
                self.requests_sent += 1
                try:
                    response = await self._http.get(
                        url, headers={"Accept": "application/json", "User-Agent": self._ua}
                    )
                except httpx.ProxyError:
                    raise AccessRestrictedError(
                        "network policy denied access to hacker-news.firebaseio.com",
                        provider=PROVIDER_NAME,
                        reason="network_policy",
                    ) from None
                except httpx.TimeoutException as exc:
                    raise ProviderTimeoutError(
                        f"Hacker News request timed out ({type(exc).__name__})",
                        provider=PROVIDER_NAME,
                    ) from None
                except httpx.TransportError as exc:
                    raise TemporarilyUnavailableError(
                        f"network error reaching Hacker News ({type(exc).__name__})",
                        provider=PROVIDER_NAME,
                        reason="network",
                    ) from None
            return self._handle(response)

        try:
            data = await call_with_retries(
                attempt, self._retry, op_name="hackernews.get", sleep=self._sleep
            )
        except ProviderError as exc:
            self.last_error = exc
            self.last_error_at = self._clock()
            raise
        self.last_error = None
        self.last_error_at = None
        self.last_success_at = self._clock()
        return data

    def _handle(self, response: httpx.Response) -> Any:
        status = response.status_code
        common: dict[str, Any] = {"provider": PROVIDER_NAME, "http_status": status}
        if len(response.content) > MAX_BYTES:
            raise MalformedResponseError(f"response larger than {MAX_BYTES} bytes", **common)
        if status == 200:
            try:
                return response.json()
            except ValueError:
                raise MalformedResponseError(
                    "response is not valid JSON", reason="schema_changed", **common
                ) from None
        retry_after = response.headers.get("retry-after")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else None
        if status == 429:
            raise RateLimitedError("Hacker News asked us to slow down", retry_after=wait, **common)
        if status in (401, 403):
            raise AccessRestrictedError(f"HTTP {status} from Hacker News", **common)
        if status >= 500:
            raise TemporarilyUnavailableError(
                f"Hacker News server error (HTTP {status})", retry_after=wait, **common
            )
        raise InvalidRequestError(f"unexpected HTTP {status} from Hacker News", **common)

    # ------------------------------------------------------------------ endpoints
    async def story_ids(self, list_name: str, ctx: RequestContext) -> list[int]:
        if list_name not in LISTS:
            raise InvalidRequestError(f"unknown story list {list_name!r}", provider=PROVIDER_NAME)
        data = await self.get_json(f"/{list_name}.json", ctx, endpoint=EP_LIST)
        if data is None:
            return []
        if not isinstance(data, list) or not all(isinstance(i, int) for i in data):
            raise MalformedResponseError(
                f"{list_name} is not a list of item IDs",
                provider=PROVIDER_NAME,
                reason="schema_changed",
            )
        return [i for i in data if i > 0]

    async def item(self, item_id: int, ctx: RequestContext) -> tuple[HNItem | None, str | None]:
        if item_id <= 0:
            raise InvalidRequestError("item IDs are positive", provider=PROVIDER_NAME)
        raw = await self.get_json(f"/item/{item_id}.json", ctx, endpoint=EP_ITEM)
        return parse_item(raw)

    async def items(self, ids: list[int], ctx: RequestContext) -> ItemBatch:
        """Fetch many items concurrently (bounded); per-item failures are recorded."""
        batch = ItemBatch()

        async def one(item_id: int) -> None:
            try:
                item, problem = await self.item(item_id, ctx)
            except ProviderError as exc:
                batch.failed[item_id] = exc.error_type
                return
            if item is None:
                batch.skipped[item_id] = problem or "missing"
                return
            reason = item.skip_reason()
            if reason:
                batch.skipped[item_id] = reason
            else:
                batch.items[item_id] = item

        await asyncio.gather(*(one(i) for i in dict.fromkeys(ids)))
        return batch

    async def updates(self, ctx: RequestContext) -> set[int]:
        data = await self.get_json("/updates.json", ctx, endpoint=EP_UPDATES)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MalformedResponseError(
                "updates has no items list", provider=PROVIDER_NAME, reason="schema_changed"
            )
        return {i for i in items if isinstance(i, int) and i > 0}

    async def max_item(self, ctx: RequestContext) -> int:
        data = await self.get_json("/maxitem.json", ctx, endpoint=EP_MAXITEM)
        if not isinstance(data, int):
            raise MalformedResponseError(
                "maxitem is not an integer", provider=PROVIDER_NAME, reason="schema_changed"
            )
        return data

    @property
    def base_url(self) -> str:
        return self._base

    def health(self) -> dict[str, Any]:
        return {
            "requests_sent": self.requests_sent,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
        }


def build_client(
    *,
    http_client: httpx.AsyncClient,
    quota_store: QuotaStore,
    clock: Clock,
    retry: RetryPolicy,
    base_url: str,
    user_agent: str,
    daily_budget: int,
    max_concurrency: int,
    item_cache_seconds: int,
    list_cache_seconds: int,
) -> HackerNewsClient:
    return HackerNewsClient(
        http_client=http_client,
        quota=QuotaManager(hn_quota_policy(daily_budget), quota_store, clock),
        retry=retry,
        clock=clock,
        base_url=base_url,
        user_agent=user_agent,
        max_concurrency=max_concurrency,
        item_cache=timedelta(seconds=item_cache_seconds),
        list_cache=timedelta(seconds=list_cache_seconds),
    )
