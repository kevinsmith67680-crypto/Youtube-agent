"""Typed provider errors.

Every failure is classified so callers can distinguish *unsupported* from *not configured*
from *temporarily failing* from *quota exhausted*. Only errors with ``retryable = True``
are ever retried. Messages are passed through secret redaction on construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from trend_agent.logging import redact_text
from trend_agent.schemas.enums import ProviderStatus


class ProviderError(Exception):
    error_type: ClassVar[str] = "provider_error"
    provider_status: ClassVar[ProviderStatus] = ProviderStatus.TEMPORARILY_UNAVAILABLE
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        http_status: int | None = None,
        reason: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.message = redact_text(message)
        self.provider = provider
        self.http_status = http_status
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(self.message)

    def to_dict(self) -> dict[str, object]:
        return {
            "error_type": self.error_type,
            "provider_status": self.provider_status.value,
            "retryable": self.retryable,
            "message": self.message,
            "provider": self.provider,
            "http_status": self.http_status,
            "reason": self.reason,
        }


class ProviderNotFoundError(ProviderError):
    error_type = "provider_not_found"
    provider_status = ProviderStatus.NOT_CONFIGURED


class ProviderDisabledError(ProviderError):
    error_type = "provider_disabled"
    provider_status = ProviderStatus.DISABLED


class ProviderNotConfiguredError(ProviderError):
    error_type = "not_configured"
    provider_status = ProviderStatus.NOT_CONFIGURED

    def __init__(
        self, message: str, *, missing: tuple[str, ...] = (), provider: str | None = None
    ) -> None:
        super().__init__(message, provider=provider)
        self.missing = missing


class CapabilityNotSupportedError(ProviderError):
    error_type = "unsupported_capability"
    provider_status = ProviderStatus.UNSUPPORTED


class AccessRestrictedError(ProviderError):
    error_type = "access_restricted"
    provider_status = ProviderStatus.ACCESS_RESTRICTED


class InvalidCredentialsError(AccessRestrictedError):
    error_type = "invalid_credentials"


class RateLimitedError(ProviderError):
    error_type = "rate_limited"
    provider_status = ProviderStatus.RATE_LIMITED
    retryable = True


class QuotaExhaustedError(ProviderError):
    """Quota for a bucket is used up. Never retried; wait for the reset boundary."""

    error_type = "quota_exhausted"
    provider_status = ProviderStatus.QUOTA_EXHAUSTED
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        bucket: str,
        resets_at: datetime | None,
        source: str,
        provider: str | None = None,
        http_status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider, http_status=http_status, reason=reason)
        self.bucket = bucket
        self.resets_at = resets_at
        self.source = source  # "local_estimate" or "server"


class RequestBudgetExceededError(ProviderError):
    """The per-operation request budget was exhausted (daily quota may remain)."""

    error_type = "request_budget_exceeded"
    provider_status = ProviderStatus.AVAILABLE


class TemporarilyUnavailableError(ProviderError):
    error_type = "temporarily_unavailable"
    provider_status = ProviderStatus.TEMPORARILY_UNAVAILABLE
    retryable = True


class ProviderTimeoutError(TemporarilyUnavailableError):
    error_type = "timeout"


class MalformedResponseError(ProviderError):
    """The provider answered, but not in the documented shape. Not retried."""

    error_type = "malformed_response"
    provider_status = ProviderStatus.TEMPORARILY_UNAVAILABLE


class InvalidRequestError(ProviderError):
    """The request itself is invalid (bad parameter, unknown chart, ...). Not retried."""

    error_type = "invalid_request"
    provider_status = ProviderStatus.AVAILABLE
