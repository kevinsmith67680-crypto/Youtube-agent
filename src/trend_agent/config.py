"""Application configuration.

All settings come from environment variables (optionally loaded from a local ``.env``
file) and are prefixed with ``TREND_AGENT_``. Secrets are held as ``SecretStr`` so they
are never rendered by ``repr``/logging; use :meth:`Settings.describe` for a safe summary.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class TrendsAccessMode(StrEnum):
    """How the Google Trends provider may obtain data.

    ``DISABLED``: no access attempted; provider reports NOT_CONFIGURED.
    ``OFFICIAL_ALPHA``: the operator states they have been admitted to Google's
    limited-access Trends API alpha. The provider still reports ACCESS_RESTRICTED until a
    transport implementing the operator's documented alpha contract is installed.
    """

    DISABLED = "disabled"
    OFFICIAL_ALPHA = "official_alpha"


DEFAULT_DATABASE_URL = "postgresql+psycopg://trend_agent:trend_agent_dev@127.0.0.1:5432/trend_agent"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TREND_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General -------------------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # --- Database ------------------------------------------------------------------
    database_url: SecretStr = SecretStr(DEFAULT_DATABASE_URL)
    database_echo: bool = False
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=120)

    # --- HTTP / resilience ---------------------------------------------------------
    http_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    http_max_attempts: int = Field(default=4, ge=1, le=10)
    http_backoff_base_seconds: float = Field(default=0.5, gt=0, le=60)
    http_backoff_max_seconds: float = Field(default=30.0, gt=0, le=600)
    ingestion_max_concurrency: int = Field(default=4, ge=1, le=32)
    ingestion_fetch_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)

    # --- YouTube Data API v3 -------------------------------------------------------
    youtube_api_key: SecretStr | None = None
    youtube_enabled: bool = True
    youtube_api_base_url: str = "https://www.googleapis.com/youtube/v3"
    # Default quota allocation as documented by Google at time of writing; verify against
    # the Cloud Console for the actual project and override here.
    youtube_daily_unit_limit: int = Field(default=10_000, ge=0)
    youtube_search_daily_call_limit: int = Field(default=100, ge=0)
    youtube_quota_safety_margin_pct: float = Field(default=10.0, ge=0, lt=100)
    youtube_quota_timezone: str = "America/Los_Angeles"
    youtube_max_pages_per_request: int = Field(default=3, ge=1, le=20)
    youtube_max_ids_per_videos_call: int = Field(default=50, ge=1, le=50)
    # Compliance controls (conservative by default; see docs/compliance.md).
    youtube_persist_statistics: bool = False
    youtube_persist_descriptions: bool = True
    youtube_metadata_retention_days: int = Field(default=30, ge=1, le=30)
    youtube_statistics_retention_days: int = Field(default=30, ge=1, le=30)

    # --- Google Trends -------------------------------------------------------------
    google_trends_access_mode: TrendsAccessMode = TrendsAccessMode.DISABLED
    google_trends_credentials_file: str | None = None
    # No default: the alpha API's endpoint is supplied by Google during onboarding.
    google_trends_endpoint: str | None = None
    # None means "persistence not permitted until the operator sets a retention period
    # consistent with their access agreement".
    google_trends_retention_days: int | None = Field(default=None, ge=1)

    # --- Phase 2: keyword discovery -----------------------------------------------
    # Directory with discovery.yaml, seeds/ and sources/ (default: ./config).
    discovery_config_dir: str | None = None
    # Identifies this client to public APIs and feeds (several require a descriptive UA).
    http_user_agent: str = "youtube-trend-agent/0.2 (keyword discovery; +https://example.invalid)"
    github_token: SecretStr | None = None  # optional; raises GitHub's limit to 5,000/h
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None

    # --- Verification layer -----------------------------------------------------------
    # HMAC key for evidence IDs and bundle tokens. Required when environment=production;
    # development uses a fixed, documented key (IDs are then guessable, never secret).
    verification_signing_key: SecretStr | None = None
    # Optional LLM generator (the anthropic SDK is not a dependency; install it to use).
    anthropic_api_key: SecretStr | None = None

    # --- Phase 6: forecasting ------------------------------------------------------
    # Local directory for checksummed model artifacts (JSON + LightGBM text, never pickle).
    forecast_artifact_dir: str | None = None

    # --- Phase 7: recommendations, dashboard API, alerts ----------------------------
    # Bearer token for the dashboard API. Without it the API serves public read
    # endpoints only; private user data and every mutation answer 401/503.
    api_token: SecretStr | None = None
    # Comma-separated browser origins allowed by CORS (the dashboard's own origin only).
    api_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # Single-creator deployment: the user key that owns saved ideas, feedback and alerts.
    dashboard_user: str = "creator"
    # Serve the isolated synthetic demo instead of the database (never mixes the two).
    demo_mode: bool = False
    demo_cache_path: str | None = None
    # Optional alert channels; absent credentials mean the channel is "unconfigured".
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    alert_email_from: str | None = None
    alert_email_to: str | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # --- Synthetic (mock) provider -------------------------------------------------
    mock_provider_enabled: bool = True
    mock_seed: int = 20_260_101

    # --- Phase 8: budgets, retries, concurrency (env: TREND_AGENT_<NAME>) -------------
    # Provider units per UTC day across all providers (YouTube quota units + one per
    # request for request-counted providers). Exhaustion stops scheduled collection.
    daily_api_budget: int = Field(default=9_000, ge=0)
    # Estimated LLM spend in USD (recommendations + content analysis), per UTC day and per
    # calendar month. Exhaustion falls back to the deterministic template generator.
    daily_llm_budget: float = Field(default=5.0, ge=0)
    monthly_llm_budget: float = Field(default=60.0, ge=0)
    max_recommendations_per_run: int = Field(default=20, ge=1, le=200)
    # Content analyses of one topic (monitoring target) per rolling 24 hours.
    max_analysis_requests_per_topic: int = Field(default=2, ge=1, le=50)
    # Retries after the first attempt for retryable provider errors (permanent errors are
    # never retried).
    max_retries: int = Field(default=3, ge=0, le=9)
    # Worker processes allowed to run at once (checked through heartbeats at start-up).
    max_concurrent_workers: int = Field(default=4, ge=1, le=64)
    budget_warning_ratio: float = Field(default=0.8, gt=0, le=1)
    ops_alert_cooldown_minutes: int = Field(default=60, ge=1, le=10_080)
    # Directory with operations.yaml (schedule cadence); default ./config.
    operations_config_dir: str | None = None

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported log level: {value!r}")
        return level

    @field_validator(
        "youtube_api_key",
        "github_token",
        "reddit_client_id",
        "reddit_client_secret",
        "verification_signing_key",
        "anthropic_api_key",
        "api_token",
        "smtp_password",
        "telegram_bot_token",
        mode="before",
    )
    @classmethod
    def _blank_key_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("youtube_api_base_url", "google_trends_endpoint")
    @classmethod
    def _require_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("external API endpoints must use https://")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_backoff(self) -> Settings:
        if self.http_backoff_max_seconds < self.http_backoff_base_seconds:
            raise ValueError("http_backoff_max_seconds must be >= http_backoff_base_seconds")
        return self

    # --- Safe helpers --------------------------------------------------------------
    @property
    def youtube_configured(self) -> bool:
        return self.youtube_api_key is not None

    def redacted_database_url(self) -> str:
        return redact_url_password(self.database_url.get_secret_value())

    def describe(self) -> dict[str, Any]:
        """Configuration summary that is safe to print or log (no secret values)."""
        return {
            "environment": self.environment,
            "log_level": self.log_level,
            "log_format": self.log_format.value,
            "database_url": self.redacted_database_url(),
            "youtube_enabled": self.youtube_enabled,
            "youtube_api_key": "set" if self.youtube_configured else "missing",
            "youtube_daily_unit_limit": self.youtube_daily_unit_limit,
            "youtube_search_daily_call_limit": self.youtube_search_daily_call_limit,
            "youtube_quota_safety_margin_pct": self.youtube_quota_safety_margin_pct,
            "youtube_quota_timezone": self.youtube_quota_timezone,
            "youtube_persist_statistics": self.youtube_persist_statistics,
            "youtube_persist_descriptions": self.youtube_persist_descriptions,
            "youtube_metadata_retention_days": self.youtube_metadata_retention_days,
            "google_trends_access_mode": self.google_trends_access_mode.value,
            "google_trends_credentials_file": (
                "set" if self.google_trends_credentials_file else "missing"
            ),
            "google_trends_endpoint": "set" if self.google_trends_endpoint else "missing",
            "google_trends_retention_days": self.google_trends_retention_days,
            "mock_provider_enabled": self.mock_provider_enabled,
            "discovery_config_dir": self.discovery_config_dir or "./config",
            "github_token": "set" if self.github_token else "missing (unauthenticated limits)",
            "reddit_credentials": (
                "set" if self.reddit_client_id and self.reddit_client_secret else "missing"
            ),
            "verification_signing_key": (
                "set" if self.verification_signing_key else "missing (development key)"
            ),
            "anthropic_api_key": "set" if self.anthropic_api_key else "missing",
            "api_token": "set" if self.api_token else "missing (read-only public API)",
            "api_cors_origins": self.cors_origins(),
            "demo_mode": self.demo_mode,
            "smtp": "set" if self.smtp_host and self.alert_email_to else "missing",
            "telegram": "set" if self.telegram_bot_token and self.telegram_chat_id else "missing",
            "budgets": {
                "daily_api_budget": self.daily_api_budget,
                "daily_llm_budget_usd": self.daily_llm_budget,
                "monthly_llm_budget_usd": self.monthly_llm_budget,
                "max_recommendations_per_run": self.max_recommendations_per_run,
                "max_analysis_requests_per_topic": self.max_analysis_requests_per_topic,
                "max_retries": self.max_retries,
                "max_concurrent_workers": self.max_concurrent_workers,
            },
        }

    @property
    def retry_attempts(self) -> int:
        """Total provider attempts: the stricter of HTTP_MAX_ATTEMPTS and MAX_RETRIES + 1."""
        return max(1, min(self.http_max_attempts, self.max_retries + 1))

    def cors_origins(self) -> list[str]:
        out = []
        for o in self.api_cors_origins.split(","):
            o = o.strip().rstrip("/")
            if o and o != "*" and o.startswith(("http://", "https://")):
                out.append(o)
        return out


def redact_url_password(url: str) -> str:
    """Replace the password component of a URL with ``***``."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    user = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:***@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
