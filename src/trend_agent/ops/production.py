"""Production configuration guard.

``check(settings)`` lists configuration problems. With ``TREND_AGENT_ENVIRONMENT=production``
(or ``prod``) the long-running services call :func:`enforce` at start-up and refuse to run
while any ``error`` remains: development secrets, a missing or weak API token, demo mode,
a missing verification signing key. Warnings are printed but do not block.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from trend_agent.config import DEFAULT_DATABASE_URL, Settings
from trend_agent.verification.registry import PRODUCTION_ENVIRONMENTS

DEV_PASSWORDS = frozenset({"trend_agent_dev", "postgres", "password", "changeme", "example"})
MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class ConfigProblem:
    severity: str  # error | warning
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class ProductionConfigError(RuntimeError):
    def __init__(self, problems: list[ConfigProblem]) -> None:
        self.problems = problems
        super().__init__(
            "refusing to start in production: "
            + "; ".join(f"{p.code}: {p.detail}" for p in problems)
        )


def is_production(settings: Settings) -> bool:
    return settings.environment.strip().lower() in PRODUCTION_ENVIRONMENTS


def check(settings: Settings) -> list[ConfigProblem]:
    out: list[ConfigProblem] = []
    url = settings.database_url.get_secret_value()
    parts = urlsplit(url)
    if url == DEFAULT_DATABASE_URL or (parts.password or "") in DEV_PASSWORDS:
        out.append(ConfigProblem(
            "error", "dev_database_credentials",
            "TREND_AGENT_DATABASE_URL uses the development default or a well-known password",
        ))  # fmt: skip
    host = (parts.hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1", "postgres", "db") or host.endswith(
        ".internal"
    )
    if not local and "sslmode=" not in (parts.query or ""):
        out.append(ConfigProblem(
            "warning", "database_tls",
            f"database host {host or '?'} without sslmode= in the URL; require TLS for a "
            "database reached over a network you do not control",
        ))  # fmt: skip
    token = settings.api_token.get_secret_value() if settings.api_token else ""
    if not token:
        out.append(ConfigProblem(
            "error", "api_token_missing",
            "TREND_AGENT_API_TOKEN is not set: private data and mutations would be unavailable",
        ))  # fmt: skip
    elif len(token) < MIN_TOKEN_LENGTH:
        out.append(ConfigProblem(
            "error", "api_token_weak",
            f"TREND_AGENT_API_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters",
        ))  # fmt: skip
    if settings.verification_signing_key is None:
        out.append(ConfigProblem(
            "error", "signing_key_missing",
            "TREND_AGENT_VERIFICATION_SIGNING_KEY is not set (the development key is public)",
        ))  # fmt: skip
    if settings.demo_mode:
        out.append(ConfigProblem(
            "error", "demo_mode",
            "TREND_AGENT_DEMO_MODE=true serves synthetic data; never in production",
        ))  # fmt: skip
    for origin in settings.cors_origins():
        o = urlsplit(origin)
        if o.scheme != "https" or (o.hostname or "") in ("localhost", "127.0.0.1"):
            out.append(ConfigProblem(
                "warning", "cors_origin",
                f"CORS origin {origin} is not an https public origin",
            ))  # fmt: skip
    if settings.log_format.value != "json":
        out.append(ConfigProblem(
            "warning", "log_format", "use TREND_AGENT_LOG_FORMAT=json for log aggregation"
        ))  # fmt: skip
    if settings.mock_provider_enabled:
        out.append(ConfigProblem(
            "warning", "mock_provider_enabled",
            "the synthetic mock provider is enabled; its data is labelled synthetic and kept "
            "apart, but production deployments should set TREND_AGENT_MOCK_PROVIDER_ENABLED="
            "false",
        ))  # fmt: skip
    if settings.youtube_persist_statistics:
        out.append(ConfigProblem(
            "warning", "youtube_statistics_persisted",
            "YouTube statistics persistence is enabled; confirm the compliance review in "
            "docs/api_compliance_audit.md",
        ))  # fmt: skip
    return out


def enforce(settings: Settings) -> list[ConfigProblem]:
    """Raise in production while any error remains; return the warnings."""
    problems = check(settings)
    if is_production(settings):
        errors = [p for p in problems if p.severity == "error"]
        if errors:
            raise ProductionConfigError(errors)
    return [p for p in problems if p.severity == "warning"]
