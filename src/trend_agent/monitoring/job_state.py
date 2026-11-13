"""Job outcome classification and retry policy (pure functions).

* transient errors (network, 5xx, timeouts, storage) -> RETRY_WAIT with exponential
  backoff and full jitter, bounded by ``max_attempts``
* rate limits -> RETRY_WAIT no earlier than the provider's Retry-After
* quota exhaustion -> DEFERRED until the quota window resets (not a failed attempt)
* authorization / capability / validation errors -> FAILED immediately (never retried)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.providers.errors import (
    AccessRestrictedError,
    CapabilityNotSupportedError,
    InvalidRequestError,
    MalformedResponseError,
    ProviderDisabledError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderNotFoundError,
    QuotaExhaustedError,
    RateLimitedError,
    RequestBudgetExceededError,
)
from trend_agent.schemas.monitoring import ErrorCategory, JobStatus


def classify(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, QuotaExhaustedError | RequestBudgetExceededError):
        return ErrorCategory.QUOTA
    if isinstance(exc, RateLimitedError):
        return ErrorCategory.RATE_LIMITED
    if isinstance(exc, AccessRestrictedError):  # includes InvalidCredentialsError
        return ErrorCategory.AUTHORIZATION
    if isinstance(
        exc,
        CapabilityNotSupportedError
        | ProviderNotConfiguredError
        | ProviderDisabledError
        | ProviderNotFoundError,
    ):
        return ErrorCategory.CAPABILITY
    if isinstance(exc, InvalidRequestError | MalformedResponseError):
        return ErrorCategory.VALIDATION
    if isinstance(exc, ProviderError):
        return ErrorCategory.TRANSIENT if exc.retryable else ErrorCategory.VALIDATION
    return ErrorCategory.INTERNAL


PERMANENT = frozenset(
    {ErrorCategory.AUTHORIZATION, ErrorCategory.CAPABILITY, ErrorCategory.VALIDATION}
)


@dataclass(frozen=True)
class RetryDecision:
    status: JobStatus
    retry_at: datetime | None
    deferred: bool
    reason: str


def backoff_delay(
    attempt: int, base: timedelta, maximum: timedelta, rng: random.Random
) -> timedelta:
    """Full-jitter exponential backoff: uniform(0, min(max, base * 2^(attempt-1)))."""
    ceiling = min(maximum.total_seconds(), base.total_seconds() * (2 ** max(attempt - 1, 0)))
    return timedelta(seconds=rng.uniform(0.0, ceiling))


def decide_retry(
    exc: BaseException,
    *,
    attempt: int,
    max_attempts: int,
    now: datetime,
    base: timedelta,
    maximum: timedelta,
    rng: random.Random,
) -> RetryDecision:
    category = classify(exc)
    if category is ErrorCategory.QUOTA:
        resets = getattr(exc, "resets_at", None)
        retry_at = resets if isinstance(resets, datetime) and resets > now else now + maximum
        return RetryDecision(JobStatus.DEFERRED, retry_at, True, "quota exhausted until reset")
    if category in PERMANENT:
        return RetryDecision(JobStatus.FAILED, None, False, f"permanent {category.value} error")
    if attempt >= max_attempts:
        return RetryDecision(JobStatus.FAILED, None, False, "max attempts reached")
    delay = backoff_delay(attempt, base, maximum, rng)
    retry_after_s = getattr(exc, "retry_after", None)
    if isinstance(retry_after_s, int | float) and retry_after_s > 0:
        delay = max(delay, timedelta(seconds=float(retry_after_s)))
    return RetryDecision(JobStatus.RETRY_WAIT, now + delay, False, f"{category.value}; retrying")
