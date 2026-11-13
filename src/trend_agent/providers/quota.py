"""Local, quota-aware request accounting.

The quota tracked here is a *local estimate*. The provider's server-side records are
authoritative: requests made by other processes/projects sharing a key, retried requests
the server did or did not bill, and invalid requests (which Google still bills) can make
the two diverge. When the server reports exhaustion, the bucket is marked exhausted for
the rest of the window regardless of the local estimate.

Kept under ``providers`` (rather than ``ingestion``) because it is enforced in the provider
transport layer; placing it in ``ingestion`` would make providers depend on ingestion.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trend_agent.providers.errors import QuotaExhaustedError, RequestBudgetExceededError
from trend_agent.utils.time import Clock, local_day_window, utc_now


@dataclass(frozen=True)
class QuotaBucket:
    name: str
    daily_limit: int
    safety_margin_pct: float = 0.0

    @property
    def effective_limit(self) -> int:
        """Daily limit minus the configured safety margin (rounded down)."""
        return math.floor(self.daily_limit * (100.0 - self.safety_margin_pct) / 100.0)


@dataclass(frozen=True)
class EndpointCost:
    bucket: str
    cost: int


@dataclass(frozen=True)
class QuotaPolicy:
    provider_name: str
    reset_timezone: str
    buckets: Mapping[str, QuotaBucket]
    endpoint_costs: Mapping[str, EndpointCost]

    def cost_of(self, endpoint: str) -> EndpointCost:
        try:
            cost = self.endpoint_costs[endpoint]
        except KeyError:
            # Unknown endpoints are never treated as free.
            raise ValueError(f"no quota cost declared for endpoint {endpoint!r}") from None
        if cost.bucket not in self.buckets:
            raise ValueError(f"endpoint {endpoint!r} references unknown bucket {cost.bucket!r}")
        return cost


@dataclass(frozen=True)
class QuotaWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class BucketUsage:
    provider: str
    bucket: str
    window_start: datetime
    window_end: datetime
    used: int
    effective_limit: int
    daily_limit: int
    server_exhausted: bool

    @property
    def remaining(self) -> int:
        return 0 if self.server_exhausted else max(0, self.effective_limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


class QuotaStore(Protocol):
    """Persistence for quota counters. Implementations must make ``try_consume`` atomic."""

    def try_consume(
        self, *, provider: str, bucket: str, window: QuotaWindow, limit: int, cost: int
    ) -> tuple[bool, int]:
        """Atomically add ``cost`` if ``used + cost <= limit`` and not exhausted.

        Returns ``(granted, used_after)``.
        """
        ...

    def mark_exhausted(self, *, provider: str, bucket: str, window: QuotaWindow) -> None: ...

    def get_usage(self, *, provider: str, bucket: str, window: QuotaWindow) -> tuple[int, bool]:
        """Return ``(used, server_exhausted)`` for the window (``(0, False)`` if absent)."""
        ...


class InMemoryQuotaStore:
    """Process-local store, used in tests and when no database is configured."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str, datetime], list[int | bool]] = {}

    def _row(self, provider: str, bucket: str, window: QuotaWindow) -> list[int | bool]:
        return self._state.setdefault((provider, bucket, window.start), [0, False])

    def try_consume(
        self, *, provider: str, bucket: str, window: QuotaWindow, limit: int, cost: int
    ) -> tuple[bool, int]:
        with self._lock:
            row = self._row(provider, bucket, window)
            used, exhausted = int(row[0]), bool(row[1])
            if exhausted or used + cost > limit:
                return False, used
            row[0] = used + cost
            return True, used + cost

    def mark_exhausted(self, *, provider: str, bucket: str, window: QuotaWindow) -> None:
        with self._lock:
            self._row(provider, bucket, window)[1] = True

    def get_usage(self, *, provider: str, bucket: str, window: QuotaWindow) -> tuple[int, bool]:
        with self._lock:
            row = self._state.get((provider, bucket, window.start), [0, False])
            return int(row[0]), bool(row[1])


@dataclass
class QuotaLedger:
    """Per-operation accounting plus an optional request budget (units per bucket)."""

    budget: Mapping[str, int] | None = None
    used: dict[str, int] = field(default_factory=dict)
    requests: int = 0

    def ensure_within_budget(self, bucket: str, cost: int) -> None:
        if self.budget is None or bucket not in self.budget:
            return
        if self.used.get(bucket, 0) + cost > self.budget[bucket]:
            raise RequestBudgetExceededError(
                f"request budget for bucket {bucket!r} exhausted "
                f"({self.used.get(bucket, 0)}/{self.budget[bucket]} units used)"
            )

    def record(self, bucket: str, cost: int) -> None:
        self.used[bucket] = self.used.get(bucket, 0) + cost
        self.requests += 1

    @property
    def total_units(self) -> int:
        return sum(self.used.values())


class QuotaManager:
    def __init__(self, policy: QuotaPolicy, store: QuotaStore, clock: Clock = utc_now) -> None:
        self.policy = policy
        self._store = store
        self._clock = clock

    def current_window(self) -> QuotaWindow:
        start, end = local_day_window(self._clock(), self.policy.reset_timezone)
        return QuotaWindow(start=start, end=end)

    def reserve(self, endpoint: str, ledger: QuotaLedger | None = None) -> None:
        """Reserve quota for one request or raise. Must be called before every attempt."""
        cost = self.policy.cost_of(endpoint)
        bucket = self.policy.buckets[cost.bucket]
        window = self.current_window()
        if ledger is not None:
            ledger.ensure_within_budget(cost.bucket, cost.cost)
        granted, used = self._store.try_consume(
            provider=self.policy.provider_name,
            bucket=cost.bucket,
            window=window,
            limit=bucket.effective_limit,
            cost=cost.cost,
        )
        if not granted:
            raise QuotaExhaustedError(
                f"estimated quota for bucket {cost.bucket!r} exhausted "
                f"({used}/{bucket.effective_limit} usable units, daily limit "
                f"{bucket.daily_limit}); resets at {window.end.isoformat()}",
                bucket=cost.bucket,
                resets_at=window.end,
                source="local_estimate",
                provider=self.policy.provider_name,
            )
        if ledger is not None:
            ledger.record(cost.bucket, cost.cost)

    def record_server_exhaustion(self, endpoint: str) -> QuotaWindow:
        cost = self.policy.cost_of(endpoint)
        window = self.current_window()
        self._store.mark_exhausted(
            provider=self.policy.provider_name, bucket=cost.bucket, window=window
        )
        return window

    def usage(self, bucket_name: str) -> BucketUsage:
        bucket = self.policy.buckets[bucket_name]
        window = self.current_window()
        used, exhausted = self._store.get_usage(
            provider=self.policy.provider_name, bucket=bucket_name, window=window
        )
        return BucketUsage(
            provider=self.policy.provider_name,
            bucket=bucket_name,
            window_start=window.start,
            window_end=window.end,
            used=used,
            effective_limit=bucket.effective_limit,
            daily_limit=bucket.daily_limit,
            server_exhausted=exhausted,
        )

    def status(self) -> list[BucketUsage]:
        return [self.usage(name) for name in self.policy.buckets]
