"""Retry with exponential backoff and full jitter, for retryable provider errors only."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from trend_agent.logging import get_logger, log_event
from trend_agent.providers.errors import ProviderError

Sleep = Callable[[float], Awaitable[None]]

_log = get_logger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    rng: random.Random = field(default_factory=random.Random, compare=False)

    def backoff(self, attempt: int) -> float:
        """Full-jitter delay before retry number ``attempt`` (1-based)."""
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return self.rng.uniform(0.0, ceiling)


async def call_with_retries[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    op_name: str,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Run ``operation``; retry only on ``ProviderError.retryable`` errors.

    Permanent errors (invalid request, bad credentials, quota exhaustion, malformed
    responses) propagate immediately. A server-provided ``Retry-After`` larger than the
    policy's ``max_delay`` is not honoured by waiting; the error propagates instead.
    """
    attempt = 1
    while True:
        try:
            return await operation()
        except ProviderError as exc:
            if not exc.retryable or attempt >= policy.max_attempts:
                raise
            delay = policy.backoff(attempt)
            if exc.retry_after is not None:
                if exc.retry_after > policy.max_delay:
                    raise
                delay = max(delay, exc.retry_after)
            log_event(
                _log,
                "provider.retry",
                logging.WARNING,
                op=op_name,
                attempt=attempt,
                delay_seconds=round(delay, 3),
                error_type=exc.error_type,
                http_status=exc.http_status,
            )
            await sleep(delay)
            attempt += 1
