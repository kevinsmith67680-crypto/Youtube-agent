"""Twitch app access token (OAuth 2.0 client credentials).

* ``POST {auth}/token`` with ``client_id``, ``client_secret`` and
  ``grant_type=client_credentials`` in the form body (never in the URL, so the secret cannot
  end up in access logs). The response carries ``access_token``, ``expires_in`` and
  ``token_type``; no refresh token is issued for this flow, so "refresh" means requesting a
  new token before the current one expires (``token_refresh_margin_seconds``).
* ``GET {auth}/validate`` (``Authorization: OAuth <token>``) at first use and then hourly, as
  Twitch requires; a 401 there or from any Helix call drops the cached token, and the next
  request obtains a new one (revoked or expired tokens recover on their own).
* The token lives only in this process's memory: never logged, never stored in the
  database, never sent to the browser. ``state()`` reports metadata only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from trend_agent.providers.errors import (
    AccessRestrictedError,
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from trend_agent.providers.http import RetryPolicy, Sleep, call_with_retries
from trend_agent.twitch.models import PROVIDER_NAME
from trend_agent.utils.time import Clock


class AppTokenManager:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        client_id: SecretStr | None,
        client_secret: SecretStr | None,
        clock: Clock,
        retry: RetryPolicy,
        auth_base_url: str = "https://id.twitch.tv/oauth2",
        user_agent: str = "youtube-trend-agent",
        refresh_margin: timedelta = timedelta(hours=1),
        validate_interval: timedelta = timedelta(hours=1),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http_client
        self._client_id = client_id
        self._secret = client_secret
        self._clock = clock
        self._retry = retry
        self._base = auth_base_url.rstrip("/")
        self._ua = user_agent
        self._margin = refresh_margin
        self._validate_every = validate_interval
        self._sleep = sleep
        self._token: SecretStr | None = None
        self._expires_at: datetime | None = None
        self._obtained_at: datetime | None = None
        self._validated_at: datetime | None = None
        self._validation: str = "not_validated"
        self._locks: dict[int, asyncio.Lock] = {}
        self.token_requests = 0
        self.validations = 0
        self.last_error: ProviderError | None = None

    def __repr__(self) -> str:  # never show the token or the secret
        return f"AppTokenManager(configured={self.configured}, has_token={self.has_token})"

    # ------------------------------------------------------------------ state
    @property
    def configured(self) -> bool:
        return bool(
            self._client_id
            and self._client_id.get_secret_value().strip()
            and self._secret
            and self._secret.get_secret_value().strip()
        )

    @property
    def client_id(self) -> str:
        if not self._client_id:
            raise ProviderNotConfiguredError(
                "TWITCH_CLIENT_ID is not set", missing=("TWITCH_CLIENT_ID",), provider=PROVIDER_NAME
            )
        return self._client_id.get_secret_value().strip()

    @property
    def has_token(self) -> bool:
        return self._token is not None

    def state(self) -> dict[str, Any]:
        """Token metadata for status pages (never the token itself)."""
        now = self._clock()
        return {
            "configured": self.configured,
            "has_token": self.has_token,
            "obtained_at": self._obtained_at.isoformat() if self._obtained_at else None,
            "expires_at": self._expires_at.isoformat() if self._expires_at else None,
            "expires_in_hours": round((self._expires_at - now).total_seconds() / 3600, 1)
            if self._expires_at
            else None,
            "last_validated_at": self._validated_at.isoformat() if self._validated_at else None,
            "validation": self._validation,
            "token_requests": self.token_requests,
            "validations": self.validations,
            "last_error": self.last_error.to_dict() if self.last_error else None,
        }

    def invalidate(self, reason: str) -> None:
        """Drop the cached token (expired, revoked or rejected); the next call gets a new one."""
        self._token = None
        self._expires_at = None
        self._validation = f"invalidated: {reason}"

    def _lock(self) -> asyncio.Lock:
        loop = id(asyncio.get_running_loop())
        if loop not in self._locks:
            self._locks = {loop: asyncio.Lock()}
        return self._locks[loop]

    # ------------------------------------------------------------------ token
    async def bearer(self) -> str:
        """A usable app access token, requesting or validating one when needed."""
        if not self.configured:
            raise ProviderNotConfiguredError(
                "Twitch client credentials are not configured",
                missing=("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"),
                provider=PROVIDER_NAME,
            )
        async with self._lock():
            now = self._clock()
            if self._token is None or (self._expires_at and self._expires_at - now <= self._margin):
                await self._request_token()
            elif self._validated_at is None or now - self._validated_at >= self._validate_every:
                await self._validate_locked()
                if not self.has_token:  # validation found it revoked: get a new one
                    await self._request_token()
            assert self._token is not None
            return self._token.get_secret_value()

    async def validate(self) -> dict[str, Any]:
        """Validate the current token now (used by status/test commands)."""
        await self.bearer()
        async with self._lock():
            await self._validate_locked(force=True)
        return self.state()

    async def _request_token(self) -> None:
        url = f"{self._base}/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self._secret.get_secret_value().strip() if self._secret else "",
            "grant_type": "client_credentials",
        }

        async def attempt() -> dict[str, Any]:
            self.token_requests += 1
            response = await self._send("POST", url, data=data)
            return self._token_response(response)

        try:
            body = await call_with_retries(
                attempt, self._retry, op_name="twitch.token", sleep=self._sleep
            )
        except ProviderError as exc:
            self.last_error = exc
            self.invalidate(exc.error_type)
            raise
        now = self._clock()
        self._token = SecretStr(str(body["access_token"]))
        self._obtained_at = now
        self._expires_at = now + timedelta(seconds=int(body["expires_in"]))
        # a freshly issued token is valid by construction; the hourly check starts now
        self._validated_at = now
        self._validation = "issued"
        self.last_error = None

    def _token_response(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status == 200:
            try:
                body = response.json()
            except ValueError:
                raise MalformedResponseError(
                    "token response is not JSON", provider=PROVIDER_NAME, http_status=status
                ) from None
            token = body.get("access_token") if isinstance(body, dict) else None
            expires = body.get("expires_in") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token or not isinstance(expires, int):
                raise MalformedResponseError(
                    "token response lacks access_token/expires_in",
                    provider=PROVIDER_NAME,
                    http_status=status,
                    reason="schema_changed",
                )
            return dict(body)
        if status in (400, 401, 403):
            # Twitch answers 400 "invalid client" / 403 "invalid client secret": the
            # credentials are wrong or revoked. Never retried; the body is not echoed.
            raise InvalidCredentialsError(
                f"Twitch rejected the client credentials (HTTP {status})",
                provider=PROVIDER_NAME,
                http_status=status,
                reason="invalid_client",
            )
        if status == 429:
            raise RateLimitedError(
                "Twitch token endpoint asked us to slow down",
                provider=PROVIDER_NAME,
                http_status=429,
            )
        if status >= 500:
            raise TemporarilyUnavailableError(
                f"Twitch token endpoint error (HTTP {status})",
                provider=PROVIDER_NAME,
                http_status=status,
            )
        raise InvalidCredentialsError(
            f"unexpected HTTP {status} from the Twitch token endpoint",
            provider=PROVIDER_NAME,
            http_status=status,
        )

    async def _validate_locked(self, *, force: bool = False) -> None:
        if self._token is None:
            return
        now = self._clock()
        if not force and self._validated_at and now - self._validated_at < self._validate_every:
            return
        self.validations += 1
        try:
            response = await self._send(
                "GET",
                f"{self._base}/validate",
                headers={"Authorization": f"OAuth {self._token.get_secret_value()}"},
            )
        except ProviderError as exc:
            # A network problem says nothing about the token: keep it, try again later.
            self._validation = f"validation_failed: {exc.error_type}"
            return
        self._validated_at = now
        if response.status_code == 401:
            self.invalidate("validate_returned_401")
            return
        if response.status_code != 200:
            self._validation = f"validation_http_{response.status_code}"
            return
        try:
            body = response.json()
        except ValueError:
            self._validation = "validation_malformed"
            return
        if isinstance(body, dict):
            if body.get("client_id") and body.get("client_id") != self.client_id:
                self.invalidate("client_id_mismatch")
                return
            expires_in = body.get("expires_in")
            if isinstance(expires_in, int):
                self._expires_at = now + timedelta(seconds=expires_in)
        self._validation = "valid"

    async def _send(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method,
                url,
                data=data,
                headers={"User-Agent": self._ua, "Accept": "application/json", **(headers or {})},
            )
        except httpx.ProxyError:
            raise AccessRestrictedError(
                "network policy denied access to id.twitch.tv",
                provider=PROVIDER_NAME,
                reason="network_policy",
            ) from None
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Twitch auth request timed out ({type(exc).__name__})", provider=PROVIDER_NAME
            ) from None
        except httpx.TransportError as exc:
            raise TemporarilyUnavailableError(
                f"network error reaching id.twitch.tv ({type(exc).__name__})",
                provider=PROVIDER_NAME,
                reason="network",
            ) from None
