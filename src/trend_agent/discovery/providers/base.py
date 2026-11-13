"""Discovery provider interface and shared HTTP plumbing.

Discovery providers reuse the Phase 1 machinery: ``ProviderDescriptor`` and status
reports, the typed error taxonomy, ``QuotaManager`` (persisted local request caps) and
retry with jittered backoff. They return :class:`DiscoveryResult` objects whose items are
*source material*, never measurements.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from trend_agent.discovery.models import (
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    SourceType,
)
from trend_agent.providers.base import Provider, RequestContext
from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
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
from trend_agent.schemas.enums import (
    Capability,
    DataNature,
    MeasurementType,
    ProviderStatus,
    ProviderType,
    RecordKind,
    TimeGranularity,
)
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderDescriptor,
    ProviderStatusReport,
    RetentionRule,
)
from trend_agent.utils.time import Clock

REQUEST_BUCKET = "requests"
REQUEST_ENDPOINT = "request"
DEFAULT_MAX_BYTES = 5_000_000


@runtime_checkable
class DiscoveryProvider(Provider, Protocol):
    def supports(self, source: DiscoverySource) -> bool:
        """Can this provider handle the source's type/reference?"""
        ...

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        """Is there local quota for one fetch of this source? (No network I/O.)"""
        ...

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult: ...


def request_quota(
    provider_name: str, daily_limit: int, store: QuotaStore, clock: Clock
) -> QuotaManager:
    """A single daily request bucket (UTC reset) for providers without a unit model."""
    policy = QuotaPolicy(
        provider_name=provider_name,
        reset_timezone="UTC",
        buckets={REQUEST_BUCKET: QuotaBucket(REQUEST_BUCKET, daily_limit, 0.0)},
        endpoint_costs={REQUEST_ENDPOINT: EndpointCost(REQUEST_BUCKET, 1)},
    )
    return QuotaManager(policy, store, clock)


def afford(quota: QuotaManager, bucket: str, cost: int) -> tuple[bool, str]:
    try:
        usage = quota.usage(bucket)
    except Exception as exc:  # quota store unavailable: let the fetch path decide
        return True, f"quota state unavailable ({type(exc).__name__})"
    if usage.remaining < cost:
        return False, (
            f"quota: '{bucket}' has {usage.remaining} of {usage.effective_limit} left today "
            f"(needs {cost}); resets {usage.window_end.isoformat()}"
        )
    return True, ""


def _retry_after(response: httpx.Response, now: datetime) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            value = float(header)
            return value if math.isfinite(value) and value >= 0 else None
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(header) - now).total_seconds())
            except (TypeError, ValueError):
                return None
    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(0.0, int(reset) - now.timestamp())
    return None


class HttpFetcher:
    """GET with quota reservation, typed error mapping and a response-size cap."""

    def __init__(
        self,
        *,
        provider_name: str,
        client: httpx.AsyncClient,
        quota: QuotaManager,
        retry: RetryPolicy,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.provider_name = provider_name
        self._client = client
        self.quota = quota
        self._retry = retry
        self._clock = clock
        self._sleep = sleep
        self._max_bytes = max_bytes

    async def get(
        self,
        url: str,
        ctx: RequestContext,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_not_modified: bool = False,
        endpoint: str = REQUEST_ENDPOINT,
    ) -> httpx.Response:
        async def attempt() -> httpx.Response:
            self.quota.reserve(endpoint, ctx.ledger)
            try:
                response = await self._client.get(
                    url, params=dict(params or {}), headers=dict(headers or {})
                )
            except httpx.ProxyError:
                # An egress proxy refused the destination: a policy decision, not a blip.
                raise AccessRestrictedError(
                    f"network policy denied access to {httpx.URL(url).host}",
                    provider=self.provider_name,
                ) from None
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    f"request timed out ({type(exc).__name__})", provider=self.provider_name
                ) from None
            except httpx.TransportError as exc:
                raise TemporarilyUnavailableError(
                    f"network error ({type(exc).__name__})", provider=self.provider_name
                ) from None
            return self._check(response, allow_not_modified)

        return await call_with_retries(
            attempt, self._retry, op_name=f"{self.provider_name}.get", sleep=self._sleep
        )

    async def get_json(
        self,
        url: str,
        ctx: RequestContext,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self.get(url, ctx, params=params, headers=headers)
        try:
            return response.json()
        except ValueError:
            raise MalformedResponseError(
                "response is not valid JSON",
                provider=self.provider_name,
                http_status=response.status_code,
            ) from None

    def _check(self, response: httpx.Response, allow_not_modified: bool) -> httpx.Response:
        status = response.status_code
        if len(response.content) > self._max_bytes:
            raise MalformedResponseError(
                f"response larger than {self._max_bytes} bytes", provider=self.provider_name
            )
        if 200 <= status < 300 or (status == 304 and allow_not_modified):
            return response
        common: dict[str, Any] = {"provider": self.provider_name, "http_status": status}
        text = f"HTTP {status} from {response.request.url.host}"
        retry_after = _retry_after(response, self._clock().astimezone(UTC))
        if status == 401:
            raise InvalidCredentialsError(text, **common)
        if status == 429 or (
            status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise RateLimitedError(f"{text} (rate limited)", retry_after=retry_after, **common)
        if status == 403:
            raise AccessRestrictedError(text, **common)
        if status >= 500:
            raise TemporarilyUnavailableError(text, retry_after=retry_after, **common)
        raise InvalidRequestError(text, **common)


def source_types(*types: SourceType) -> frozenset[SourceType]:
    return frozenset(types)


def discovery_descriptor(
    *,
    name: str,
    display_name: str,
    version: str,
    provider_type: ProviderType,
    capabilities: frozenset[Capability],
    requires_authentication: bool,
    enabled: bool,
    retention_days: int | None,
    usage_restrictions: tuple[str, ...],
    documentation_urls: tuple[str, ...] = (),
    measurement_types: frozenset[MeasurementType] = frozenset(),
    data_nature: DataNature = DataNature.ACTUAL,
) -> ProviderDescriptor:
    """Descriptor for a discovery provider, with a retention rule for its stored items."""
    return ProviderDescriptor(
        name=name,
        display_name=display_name,
        version=version,
        provider_type=provider_type,
        capabilities=capabilities,
        measurement_types=measurement_types,
        search_properties=frozenset(),
        supported_regions=None,
        supported_granularities=frozenset({TimeGranularity.POINT}),
        requires_authentication=requires_authentication,
        historical_data_available=False,
        data_nature=data_nature,
        enabled=enabled,
        retention=(
            RetentionRule(
                record_kind=RecordKind.DISCOVERY_ITEM,
                persist_allowed=True,
                max_retention=timedelta(days=retention_days) if retention_days else None,
                refresh_required=False,
                notes="Discovery source material (titles, links, short excerpts).",
            ),
        ),
        usage_restrictions=usage_restrictions,
        documentation_urls=documentation_urls,
    )


def simple_status(
    name: str,
    enabled: bool,
    detail: str,
    checked_at: datetime,
    capability: Capability = Capability.TREND_DISCOVERY,
    status: ProviderStatus | None = None,
    missing: tuple[str, ...] = (),
) -> ProviderStatusReport:
    resolved = status or (ProviderStatus.AVAILABLE if enabled else ProviderStatus.DISABLED)
    return ProviderStatusReport(
        provider_name=name,
        status=resolved,
        detail=detail if enabled else "Disabled by discovery configuration.",
        capabilities=(CapabilityStatus(capability=capability, status=resolved, detail=detail),),
        missing_configuration=missing,
        checked_at=checked_at,
    )
