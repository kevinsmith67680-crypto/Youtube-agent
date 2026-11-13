from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import MutableClock, SleepRecorder, make_settings
from trend_agent.providers.errors import (
    InvalidRequestError,
    QuotaExhaustedError,
    RateLimitedError,
    RequestBudgetExceededError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.http import RetryPolicy, call_with_retries
from trend_agent.providers.quota import (
    EndpointCost,
    InMemoryQuotaStore,
    QuotaBucket,
    QuotaLedger,
    QuotaManager,
    QuotaPolicy,
)
from trend_agent.providers.youtube import youtube_quota_policy


def small_policy(limit: int = 10, margin: float = 0.0) -> QuotaPolicy:
    return QuotaPolicy(
        provider_name="p",
        reset_timezone="America/Los_Angeles",
        buckets={
            "default": QuotaBucket("default", limit, margin),
            "search": QuotaBucket("search", 2),
        },
        endpoint_costs={"list": EndpointCost("default", 3), "search": EndpointCost("search", 1)},
    )


def test_effective_limit_applies_safety_margin() -> None:
    assert QuotaBucket("b", 10_000, 10).effective_limit == 9_000
    assert QuotaBucket("b", 100, 0).effective_limit == 100
    assert QuotaBucket("b", 7, 50).effective_limit == 3  # rounds down


def test_youtube_policy_matches_documented_defaults() -> None:
    policy = youtube_quota_policy(make_settings(youtube_quota_safety_margin_pct=0))
    assert policy.buckets["search"].daily_limit == 100
    assert policy.buckets["default"].daily_limit == 10_000
    assert policy.cost_of("search.list") == EndpointCost("search", 1)
    assert policy.cost_of("videos.list") == EndpointCost("default", 1)
    assert policy.reset_timezone == "America/Los_Angeles"


def test_unknown_endpoint_is_never_free() -> None:
    with pytest.raises(ValueError, match="no quota cost"):
        small_policy().cost_of("videos.insert")


def test_reserve_until_exhausted_raises_typed_error() -> None:
    clock = MutableClock()
    manager = QuotaManager(small_policy(limit=10), InMemoryQuotaStore(), clock)
    ledger = QuotaLedger()
    for _ in range(3):
        manager.reserve("list", ledger)
    assert ledger.used == {"default": 9}
    with pytest.raises(QuotaExhaustedError) as info:
        manager.reserve("list", ledger)  # 9 + 3 > 10
    err = info.value
    assert err.source == "local_estimate"
    assert err.bucket == "default"
    assert err.retryable is False
    assert err.resets_at == datetime(2026, 3, 2, 8, tzinfo=UTC)  # midnight PST
    assert ledger.used == {"default": 9}  # refused reservation not recorded
    assert manager.usage("default").remaining == 1


def test_quota_resets_at_pacific_midnight() -> None:
    clock = MutableClock(datetime(2026, 3, 1, 7, 59, tzinfo=UTC))  # 23:59 PST, Feb 28
    manager = QuotaManager(small_policy(limit=3), InMemoryQuotaStore(), clock)
    manager.reserve("list")
    with pytest.raises(QuotaExhaustedError):
        manager.reserve("list")
    clock.advance(timedelta(minutes=2))  # 00:01 PST: new window
    manager.reserve("list")
    assert manager.usage("default").used == 3


def test_server_exhaustion_blocks_bucket_until_reset() -> None:
    clock = MutableClock()
    manager = QuotaManager(small_policy(limit=100), InMemoryQuotaStore(), clock)
    manager.record_server_exhaustion("list")
    usage = manager.usage("default")
    assert usage.server_exhausted
    assert usage.remaining == 0
    with pytest.raises(QuotaExhaustedError):
        manager.reserve("list")
    manager.reserve("search")  # other buckets unaffected
    clock.advance(timedelta(days=1))
    manager.reserve("list")


def test_request_budget_is_enforced_before_daily_quota() -> None:
    manager = QuotaManager(small_policy(limit=100), InMemoryQuotaStore(), MutableClock())
    ledger = QuotaLedger(budget={"default": 5})
    manager.reserve("list", ledger)
    with pytest.raises(RequestBudgetExceededError):
        manager.reserve("list", ledger)
    assert manager.usage("default").used == 3  # budget refusal consumed nothing


# ---------------------------------------------------------------------------- retry


def test_full_jitter_backoff_is_bounded() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=5.0, rng=random.Random(1))
    for attempt in range(1, 8):
        ceiling = min(5.0, 2 ** (attempt - 1))
        assert all(0 <= policy.backoff(attempt) <= ceiling for _ in range(50))


async def test_retries_only_retryable_errors() -> None:
    sleeper = SleepRecorder()
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TemporarilyUnavailableError("503")
        return "ok"

    policy = RetryPolicy(max_attempts=4, base_delay=0.1, max_delay=1.0, rng=random.Random(0))
    assert await call_with_retries(flaky, policy, op_name="t", sleep=sleeper) == "ok"
    assert calls == 3
    assert len(sleeper.delays) == 2


@pytest.mark.parametrize(
    "error",
    [
        InvalidRequestError("bad"),
        QuotaExhaustedError("q", bucket="b", resets_at=None, source="server"),
    ],
)
async def test_permanent_errors_are_not_retried(error: Exception) -> None:
    sleeper = SleepRecorder()
    calls = 0

    async def failing() -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error)):
        await call_with_retries(failing, RetryPolicy(max_attempts=5), op_name="t", sleep=sleeper)
    assert calls == 1
    assert sleeper.delays == []


async def test_gives_up_after_max_attempts() -> None:
    sleeper = SleepRecorder()

    async def always() -> None:
        raise RateLimitedError("slow down")

    with pytest.raises(RateLimitedError):
        await call_with_retries(always, RetryPolicy(max_attempts=3), op_name="t", sleep=sleeper)
    assert len(sleeper.delays) == 2


async def test_retry_after_is_honoured_or_aborts_when_too_long() -> None:
    sleeper = SleepRecorder()
    attempts = 0

    async def once_limited() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitedError("wait", retry_after=2.5)
        return "ok"

    policy = RetryPolicy(max_attempts=3, base_delay=0.1, max_delay=5.0, rng=random.Random(0))
    assert await call_with_retries(once_limited, policy, op_name="t", sleep=sleeper) == "ok"
    assert sleeper.delays == [2.5]

    async def long_wait() -> None:
        raise RateLimitedError("wait", retry_after=3600)

    with pytest.raises(RateLimitedError):
        await call_with_retries(long_wait, policy, op_name="t", sleep=sleeper)
