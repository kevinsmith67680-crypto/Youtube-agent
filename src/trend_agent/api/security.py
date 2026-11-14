"""Authentication and rate limiting for the dashboard API.

* One bearer token (``TREND_AGENT_API_TOKEN``), compared in constant time. It is held by
  the dashboard's server-side proxy; it is never sent to or stored in the browser.
* Without a configured token the API is a local, read-only view: public reads work with
  every user-specific field removed, and private data and all writes answer 401.
* Fixed-window rate limits per client (token holder or remote address): a general request
  budget per minute and a separate, small budget for generation per hour.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request


def bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


@dataclass
class Authenticator:
    token: str | None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def check(self, request: Request) -> bool:
        presented = bearer(request)
        if not self.token or presented is None:
            return False
        return hmac.compare_digest(presented.encode(), self.token.encode())


def client_key(request: Request, authenticated: bool) -> str:
    if authenticated:
        tok = bearer(request) or ""
        return "token:" + hashlib.sha256(tok.encode()).hexdigest()[:16]
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


@dataclass
class RateLimiter:
    """Fixed-window counter (in-process; a single API worker serves one creator)."""

    limit: int
    window_seconds: float
    clock: Callable[[], float] = time.monotonic
    _counts: dict[str, tuple[float, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def hit(self, key: str) -> tuple[bool, int]:
        """Count one request. Returns (allowed, seconds until the window resets)."""
        now = self.clock()
        with self._lock:
            start, n = self._counts.get(key, (now, 0))
            if now - start >= self.window_seconds:
                start, n = now, 0
            n += 1
            self._counts[key] = (start, n)
            if len(self._counts) > 10_000:  # bound memory under address churn
                cutoff = now - self.window_seconds
                self._counts = {k: v for k, v in self._counts.items() if v[0] >= cutoff}
            retry = max(1, int(self.window_seconds - (now - start)))
            return n <= self.limit, retry
