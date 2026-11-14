"""Twitch discovery provider: emerging games into the Phase 2 pipeline.

A source is one game the Twitch monitor found attracting attention
(``twitch:game:<twitch game id>``, generated at run time, never configured by hand). A
fetch re-verifies the game through ``GET /helix/games?id=`` and returns one
``game_catalog`` item: the game's official catalog name (the entity's source hint, which is
the only name this evidence verifies), its Twitch and IGDB IDs, and the attention summary
that triggered the promotion. Phase 2 then resolves the name against existing entities and
generates keyword hypotheses from its game templates.

Nothing here claims why attention rose: no patch notes, events or features are inferred.
Non-game categories ("Just Chatting", ...) and, by default, catalog entries without an
IGDB ID are excluded and counted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from trend_agent.discovery.models import (
    Category,
    DiscoveryItem,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySource,
    EvidenceType,
    SourceType,
)
from trend_agent.discovery.providers.base import afford, discovery_descriptor
from trend_agent.providers.base import RequestContext
from trend_agent.providers.errors import (
    InvalidCredentialsError,
    InvalidRequestError,
    ProviderDisabledError,
    ProviderNotConfiguredError,
    RateLimitedError,
)
from trend_agent.schemas.enums import Capability, ProviderStatus, ProviderType
from trend_agent.schemas.provider import (
    CapabilityStatus,
    ProviderDescriptor,
    ProviderStatusReport,
)
from trend_agent.twitch.client import BUCKET, TwitchClient
from trend_agent.twitch.config import TwitchSettings
from trend_agent.twitch.models import PROVIDER_NAME, SIGNAL_CLASS, SIGNAL_LABEL, category_kind
from trend_agent.utils.time import Clock

NAME = PROVIDER_NAME
CAPABILITIES = frozenset(
    {
        Capability.TREND_DISCOVERY,
        Capability.TOP_GAMES,
        Capability.GAME_LOOKUP,
        Capability.ACTIVE_STREAMS,
        Capability.GAME_STREAMS,
        Capability.VIEWER_OBSERVATIONS,
        Capability.GAME_RANKING_HISTORY,
        Capability.ATTENTION_MOMENTUM,
    }
)
SOURCE_PREFIX = "twitch:game:"


def game_source(
    game_id: str, name: str, attention: dict[str, Any], cooldown_hours: int
) -> DiscoverySource:
    """The run-time source used to promote one emerging game."""
    return DiscoverySource(
        key=f"{SOURCE_PREFIX}{game_id}",
        provider=NAME,
        name=f"Twitch game {name}"[:200],
        source_type=SourceType.API_QUERY,
        category=Category.GAMING,
        reference=game_id,
        evidence_type=EvidenceType.GAME_CATALOG,
        reliability=0.6,
        min_poll_interval_minutes=cooldown_hours * 60,
        verified=True,
        params={"kind": "game", "attention": attention},
    )


class TwitchDiscoveryProvider:
    NAME = NAME
    ACCESS_COOLDOWN = timedelta(minutes=30)

    def __init__(
        self,
        client: TwitchClient,
        *,
        settings: TwitchSettings,
        enabled: bool,
        api_enabled: bool,
        retention_days: int | None,
        clock: Clock,
    ) -> None:
        self.client = client
        self.settings = settings
        self._api_enabled = api_enabled
        self._enabled = enabled and api_enabled
        self._clock = clock
        self._descriptor = discovery_descriptor(
            name=NAME,
            display_name="Twitch Helix API (games and live streams)",
            version="1.0.0",
            provider_type=ProviderType.OFFICIAL_API,
            capabilities=CAPABILITIES,
            requires_authentication=True,
            enabled=self._enabled,
            retention_days=retention_days,
            usage_restrictions=(
                "Livestream activity on Twitch (external gaming attention): never YouTube "
                "search interest, search volume or demand.",
                "Twitch Developer Services Agreement: Twitch data is cached for 24 hours "
                "unless Twitch authorises longer storage in writing; no re-distribution.",
                "App access token (client credentials) for public endpoints only; the secret "
                "and the token are never logged, stored or sent to the browser.",
            ),
            documentation_urls=("https://dev.twitch.tv/docs/api/reference/",),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> ProviderStatusReport:
        now = self._clock()
        tokens = self.client.tokens
        err = self.client.last_error or tokens.last_error
        at = self.client.last_error_at or now
        missing: tuple[str, ...] = ()
        if not self._api_enabled:
            status, detail = ProviderStatus.DISABLED, "TWITCH_API_ENABLED is false."
        elif not self._enabled:
            status, detail = ProviderStatus.DISABLED, "Disabled by discovery configuration."
        elif not tokens.configured:
            status = ProviderStatus.NOT_CONFIGURED
            detail = "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (app access token)."
            missing = ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
        elif isinstance(err, InvalidCredentialsError) and now - at < self.ACCESS_COOLDOWN:
            status = ProviderStatus.ACCESS_RESTRICTED
            detail = f"Twitch rejected the credentials: {err.message}"
        elif (
            isinstance(err, RateLimitedError)
            and err.retry_after
            and now - at < timedelta(seconds=err.retry_after)
        ):
            status = ProviderStatus.RATE_LIMITED
            detail = f"rate limited until the bucket resets ({err.message})"
        elif err is not None:
            status = ProviderStatus.AVAILABLE
            detail = f"last request failed at {at:%H:%M} UTC ({err.error_type}); retried later"
        else:
            status = ProviderStatus.AVAILABLE
            detail = "App access token via client credentials; public endpoints only."
        return ProviderStatusReport(
            provider_name=NAME,
            status=status,
            detail=detail,
            capabilities=tuple(
                CapabilityStatus(capability=c, status=status, detail=detail)
                for c in sorted(CAPABILITIES, key=lambda c: c.value)
            ),
            missing_configuration=missing,
            checked_at=now,
        )

    async def aclose(self) -> None:
        return None

    def supports(self, source: DiscoverySource) -> bool:
        return (
            source.provider == NAME
            and source.source_type is SourceType.API_QUERY
            and source.reference.isdigit()
            and source.params.get("kind") == "game"
        )

    def can_afford(self, source: DiscoverySource) -> tuple[bool, str]:
        return afford(self.client.quota, BUCKET, 1)

    async def discover(self, request: DiscoveryRequest, ctx: RequestContext) -> DiscoveryResult:
        if not self._enabled:
            raise ProviderDisabledError("Twitch discovery is disabled", provider=NAME)
        if not self.client.tokens.configured:
            raise ProviderNotConfiguredError(
                "Twitch credentials are missing",
                missing=("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"),
                provider=NAME,
            )
        source = request.source
        if not self.supports(source):
            raise InvalidRequestError(f"unsupported source {source.key!r}", provider=NAME)
        now = self._clock()
        games, skipped = await self.client.games(ctx, ids=[source.reference])
        warnings: list[str] = []
        if skipped:
            warnings.append(
                "records skipped: " + ", ".join(f"{k} x{v}" for k, v in skipped.items())
            )
        game = next((g for g in games if g.id == source.reference), None)
        if game is None:
            warnings.append("game not found by ID (renamed, merged or removed on Twitch)")
            return DiscoveryResult(
                provider_name=NAME, source_key=source.key, items=(), warnings=tuple(warnings)
            )
        kind = category_kind(game.name, game.igdb_id, self.settings.monitor.non_game_categories)
        if kind == "non_game" or (kind != "game" and self.settings.promotion.require_igdb_id):
            warnings.append(f"not used: {kind} category ({game.name})")
            return DiscoveryResult(
                provider_name=NAME, source_key=source.key, items=(), warnings=tuple(warnings)
            )
        attention = source.params.get("attention") or {}
        item = DiscoveryItem(
            provider_name=NAME,
            source_key=source.key,
            source_reference=f"{SOURCE_PREFIX}{game.id}",
            evidence_type=EvidenceType.GAME_CATALOG,
            title=game.name,
            text=None,
            url=None,
            publisher="Twitch game catalog",
            author=None,
            published_at=None,
            collected_at=now,
            category_hint=Category.GAMING,
            entity_hint=game.name,
            entity_hint_type="game",
            metadata={
                "signal_class": SIGNAL_CLASS,
                "signal_label": SIGNAL_LABEL,
                # a newly attention-getting title is a discovery, never an evergreen seed
                "entity_hint_origin": "discovered",
                "twitch": {
                    "kind": "game_catalog",
                    "game_id": game.id,
                    "igdb_id": game.igdb_id,
                    "name": game.name,
                    "verified_via": "GET /helix/games?id=",
                    "verified_at": now.isoformat(),
                    "attention": attention,
                    "basis": "Twitch catalog verifies the name and ID only; the attention "
                    "summary is livestream activity, not search demand, and no cause "
                    "(update, event, launch) is inferred from it",
                },
            },
        )
        return DiscoveryResult(
            provider_name=NAME, source_key=source.key, items=(item,), warnings=tuple(warnings)
        )
