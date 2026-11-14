"""Provider capability readiness and bounded live smoke tests.

Readiness combines three independent facts and never infers one from another:

1. **Configuration** - the provider's own ``status()`` (no I/O): enabled, credentials and
   settings present, access not restricted.
2. **Live verification** - the most recent bounded smoke test recorded in
   ``provider_checks``. A configured provider that has never answered a real request is
   ``CONFIGURED_UNVERIFIED``, not ``READY``.
3. **Compliance approvals** - what the operator must have in place (terms accepted,
   access granted, retention set). These are listed, never assumed; code cannot verify a
   legal agreement.

A smoke test spends at most one provider request (YouTube: ``videoCategories.list``, one
quota unit, counted in the normal quota ledger) and only for providers that report
AVAILABLE; it is skipped otherwise. It never runs as part of the default test suite.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from trend_agent.config import Settings
from trend_agent.ops.signals import SignalClass, classify
from trend_agent.providers.base import Provider, RequestContext
from trend_agent.providers.errors import ProviderError
from trend_agent.schemas.enums import ProviderStatus, ProviderType


class ReadinessLevel(StrEnum):
    READY = "READY"  # available and a live smoke test succeeded recently
    CONFIGURED_UNVERIFIED = "CONFIGURED_UNVERIFIED"  # available, never verified live
    DEGRADED = "DEGRADED"  # available but the latest live check failed / quota / rate limit
    NOT_CONFIGURED = "NOT_CONFIGURED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    DISABLED = "DISABLED"
    SYNTHETIC_ONLY = "SYNTHETIC_ONLY"


# Credentials each provider needs (attribute on Settings) - reported as set/missing only.
_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "youtube_data_api": ("youtube_api_key",),
    "youtube_discovery": ("youtube_api_key",),
    "google_trends": ("google_trends_credentials_file",),
    "google_trends_discovery": ("google_trends_credentials_file",),
    "reddit": ("reddit_client_id", "reddit_client_secret"),
    "github": (),  # optional GITHUB_TOKEN raises the rate limit only
    "hackernews": (),
    "hackernews_firebase": (),  # official API: no key
    "feeds": (),
    "curated_seeds": (),
}

# What the operator must have obtained before relying on the provider in production.
APPROVALS: dict[str, tuple[str, ...]] = {
    "youtube_data_api": (
        "Accept the YouTube API Services Terms of Service and Developer Policies for the "
        "Google Cloud project that owns the key",
        "Restrict the API key to the YouTube Data API v3 (and to server IPs if possible)",
        "Complete a YouTube API Services compliance review before storing statistics or "
        "showing derived metrics to anyone but the operator (see api_compliance_audit.md)",
        "Request a quota extension through Google's audit form if 10,000 units/day is not "
        "enough; never multiply keys or projects",
    ),
    "youtube_discovery": ("Same approvals as youtube_data_api (it uses the same key)",),
    "google_trends": (
        "Admission to the Google Trends API alpha and acceptance of its terms",
        "A verified transport implementing the alpha contract supplied by Google",
        "An operator-set retention period (TREND_AGENT_GOOGLE_TRENDS_RETENTION_DAYS) "
        "consistent with the access terms",
    ),
    "google_trends_discovery": ("Same approvals as google_trends",),
    "reddit": (
        "A registered Reddit OAuth application and acceptance of the Reddit Data API terms; "
        "commercial use requires a separate agreement with Reddit",
    ),
    "github": (
        "GitHub Terms of Service and API terms for automated access to public data",
        "Use a read-only token (fine-grained, no repository permissions) or none; never "
        "rotate tokens to evade rate limits",
    ),
    "hackernews": ("HN Search API (Algolia): public, no key; respect fair use",),
    "hackernews_firebase": (
        "Official Hacker News Firebase API: public, no key; no documented rate limit, so "
        "poll at the configured bounded intervals",
    ),
    "feeds": ("Each feed publisher's terms; poll no faster than the configured interval",),
    "curated_seeds": (),
}


@dataclass(frozen=True)
class CheckRecord:
    """The latest smoke-test record for a provider (from ``provider_checks``)."""

    checked_at: datetime
    ok: bool
    status: str
    detail: str
    last_success_at: datetime | None = None


@dataclass(frozen=True)
class ProviderReadiness:
    name: str
    display_name: str
    signal_classes: tuple[SignalClass, ...]
    synthetic: bool
    enabled: bool
    status: ProviderStatus
    level: ReadinessLevel
    detail: str
    missing_configuration: tuple[str, ...]
    credentials: str
    last_check: dict[str, Any] | None
    approvals_required: tuple[str, ...]
    usage_restrictions: tuple[str, ...] = field(default=())

    @property
    def usable_for_real_data(self) -> bool:
        return not self.synthetic and self.level in (
            ReadinessLevel.READY,
            ReadinessLevel.CONFIGURED_UNVERIFIED,
        )

    @property
    def live_verified(self) -> bool:
        return not self.synthetic and self.level is ReadinessLevel.READY

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signal_classes"] = [c.value for c in self.signal_classes]
        d["status"] = self.status.value
        d["level"] = self.level.value
        d["usable_for_real_data"] = self.usable_for_real_data
        d["live_verified"] = self.live_verified
        return d


def _credential_state(settings: Settings, name: str) -> str:
    attrs = _CREDENTIALS.get(name)
    if attrs is None:
        return "unknown"
    if not attrs:
        return "not_required"
    present = [getattr(settings, a, None) not in (None, "") for a in attrs]
    if all(present):
        return "set"
    return "partial" if any(present) else "missing"


def _level(
    status: ProviderStatus,
    synthetic: bool,
    enabled: bool,
    check: CheckRecord | None,
    now: datetime,
    max_check_age: timedelta,
) -> ReadinessLevel:
    if synthetic:
        return ReadinessLevel.SYNTHETIC_ONLY
    if not enabled or status is ProviderStatus.DISABLED:
        return ReadinessLevel.DISABLED
    if status is ProviderStatus.NOT_CONFIGURED:
        return ReadinessLevel.NOT_CONFIGURED
    if status in (ProviderStatus.ACCESS_RESTRICTED, ProviderStatus.UNSUPPORTED):
        return ReadinessLevel.ACCESS_RESTRICTED
    if status is not ProviderStatus.AVAILABLE:
        return ReadinessLevel.DEGRADED
    if check is None:
        return ReadinessLevel.CONFIGURED_UNVERIFIED
    if not check.ok:
        return ReadinessLevel.DEGRADED
    if now - check.checked_at > max_check_age:
        return ReadinessLevel.CONFIGURED_UNVERIFIED
    return ReadinessLevel.READY


def assess(
    providers: Iterable[Provider],
    settings: Settings,
    *,
    checks: Mapping[str, CheckRecord],
    now: datetime,
    max_check_age: timedelta = timedelta(days=7),
) -> list[ProviderReadiness]:
    """Readiness of every provider; no network I/O."""
    out = []
    for p in providers:
        d = p.descriptor
        rep = p.status()
        synthetic = d.provider_type is ProviderType.SYNTHETIC
        check = checks.get(d.name)
        level = _level(rep.status, synthetic, d.enabled, check, now, max_check_age)
        out.append(
            ProviderReadiness(
                name=d.name,
                display_name=d.display_name,
                signal_classes=classify(d),
                synthetic=synthetic,
                enabled=d.enabled,
                status=rep.status,
                level=level,
                detail=rep.detail,
                missing_configuration=rep.missing_configuration,
                credentials="not_required" if synthetic else _credential_state(settings, d.name),
                last_check=(
                    {
                        "checked_at": check.checked_at.isoformat(),
                        "ok": check.ok,
                        "status": check.status,
                        "detail": check.detail,
                        "last_success_at": (
                            check.last_success_at.isoformat() if check.last_success_at else None
                        ),
                    }
                    if check is not None
                    else None
                ),
                approvals_required=() if synthetic else APPROVALS.get(d.name, ()),
                usage_restrictions=d.usage_restrictions,
            )
        )
    return sorted(out, key=lambda r: (r.synthetic, r.name))


def available_classes(
    readiness: Sequence[ProviderReadiness], *, verified_only: bool = False
) -> set[SignalClass]:
    """Signal classes at least one real provider can supply now."""
    out: set[SignalClass] = set()
    for r in readiness:
        ok = r.live_verified if verified_only else r.usable_for_real_data
        if ok:
            out.update(r.signal_classes)
    return out


# ---------------------------------------------------------------------- smoke tests
@dataclass(frozen=True)
class SmokeResult:
    provider: str
    attempted: bool
    ok: bool
    status: str
    detail: str
    latency_ms: int | None = None
    units_spent: int = 0
    signal_classes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def smoke_test(
    provider: Provider,
    *,
    region: str = "US",
    discovery_sources: Sequence[Any] = (),
) -> SmokeResult:
    """One minimal real request, only if the provider reports AVAILABLE."""
    d = provider.descriptor
    classes = tuple(c.value for c in classify(d))
    if d.provider_type is ProviderType.SYNTHETIC:
        return SmokeResult(d.name, False, False, "synthetic", "synthetic provider: not a live "
                           "source", signal_classes=classes)  # fmt: skip
    rep = provider.status()
    if rep.status is not ProviderStatus.AVAILABLE:
        return SmokeResult(
            d.name, False, False, rep.status.value, f"not attempted: {rep.detail}",
            signal_classes=classes,
        )  # fmt: skip
    started = time.perf_counter()
    try:
        units = await _probe(provider, region=region, discovery_sources=discovery_sources)
    except ProviderError as exc:
        return SmokeResult(
            d.name, True, False, exc.provider_status.value, f"{exc.error_type}: {exc.message}",
            int((time.perf_counter() - started) * 1000), 0, classes,
        )  # fmt: skip
    except NotImplementedError as exc:
        return SmokeResult(d.name, False, False, "no_probe", str(exc), signal_classes=classes)
    except Exception as exc:  # a smoke test must report, never crash the command
        return SmokeResult(
            d.name, True, False, "error", f"{type(exc).__name__}: {exc}",
            int((time.perf_counter() - started) * 1000), 0, classes,
        )  # fmt: skip
    return SmokeResult(
        d.name, True, True, "available", "live request succeeded",
        int((time.perf_counter() - started) * 1000), units, classes,
    )  # fmt: skip


async def _probe(provider: Provider, *, region: str, discovery_sources: Sequence[Any]) -> int:
    from trend_agent.providers.youtube import YouTubeDataApiProvider

    if isinstance(provider, YouTubeDataApiProvider):
        ctx = RequestContext()
        await provider.video_categories(region, ctx)
        return 1  # videoCategories.list costs one unit (counted by the provider's quota)
    from trend_agent.discovery.providers.github import GitHubDiscoveryProvider

    if isinstance(provider, GitHubDiscoveryProvider):
        from trend_agent.github.service import connection_test

        report = await connection_test(provider.client)
        if not report["ok"]:
            raise report["error"]
        return int(report["search_requests"])  # GET /rate_limit is free; one search
    from trend_agent.discovery.providers.hackernews_firebase import (
        HackerNewsDiscoveryProvider,
    )

    if isinstance(provider, HackerNewsDiscoveryProvider):
        ctx = RequestContext()
        await provider.client.max_item(ctx)
        ids = await provider.client.story_ids("topstories", ctx)
        if ids:
            await provider.client.item(ids[0], ctx)
        return 2 + bool(ids)  # maxitem + one list + one item (no key, local budget only)
    discover = getattr(provider, "discover", None)
    supports = getattr(provider, "supports", None)
    if discover is not None and supports is not None:
        from trend_agent.discovery.models import DiscoveryRequest

        for source in discovery_sources:
            if source.enabled and source.provider == provider.descriptor.name and supports(source):
                await discover(DiscoveryRequest(source=source, max_items=1), RequestContext())
                return 1
        raise NotImplementedError("no enabled discovery source is configured for this provider")
    raise NotImplementedError("no bounded probe is defined for this provider")
