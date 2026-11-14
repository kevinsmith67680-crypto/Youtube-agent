"""Twitch settings, read from ``providers.twitch.params`` in ``config/discovery.yaml``.

Credentials come from the environment (``TWITCH_CLIENT_ID`` / ``TWITCH_CLIENT_SECRET``),
never from this file. Retention defaults to 24 hours: the Twitch Developer Services
Agreement allows caching Twitch data for 24 hours unless Twitch authorises longer storage
in writing. ``retention.extended_storage_authorized`` must be set (asserting that written
authorisation exists) before ``retention.hours`` may exceed 24.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trend_agent.discovery.config import ConfigError

POLICY_CACHE_HOURS = 24


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientSettings(_Strict):
    daily_request_budget: int = Field(default=8_000, ge=1)
    max_concurrency: int = Field(default=4, ge=1, le=16)
    page_size: int = Field(default=100, ge=1, le=100)
    # Ratelimit-Remaining at or below this: wait for Ratelimit-Reset (if soon) or stop.
    rate_limit_reserve: int = Field(default=20, ge=0, le=400)
    max_rate_limit_wait_seconds: float = Field(default=15.0, ge=0, le=120)
    # Refresh the app token when it expires within this many seconds.
    token_refresh_margin_seconds: int = Field(default=3600, ge=60)
    # Twitch asks apps to validate tokens at start-up and hourly.
    token_validate_interval_seconds: int = Field(default=3600, ge=300)


class CollectionSettings(_Strict):
    top_games: int = Field(default=100, ge=1, le=500)  # games read per top-games poll
    top_stream_pages: int = Field(default=3, ge=1, le=20)  # pages of 100 live streams
    game_stream_pages: int = Field(default=5, ge=1, le=50)  # per monitored game
    languages: tuple[str, ...] = ()  # ISO 639-1 codes for top streams; empty = all
    stream_title_chars: int = Field(default=140, ge=0, le=300)


class CadenceSettings(_Strict):
    top_games_minutes: int = Field(default=15, ge=5)
    top_streams_minutes: int = Field(default=10, ge=5)
    high_priority_game_minutes: int = Field(default=10, ge=5)
    low_priority_game_minutes: int = Field(default=45, ge=10)


class MonitorSettings(_Strict):
    # Games always monitored (exact Twitch names); resolved through Get Games.
    games: tuple[str, ...] = ()
    max_monitored_games: int = Field(default=12, ge=0, le=100)
    auto_monitor: bool = True  # emerging games join the low-priority tier for 24 hours
    auto_monitor_hours: int = Field(default=24, ge=1, le=24)
    # Categories Twitch lists in its game rankings that are not games.
    non_game_categories: tuple[str, ...] = (
        "Just Chatting", "IRL", "Music", "Art", "Talk Shows & Podcasts", "Sports",
        "Special Events", "Science & Technology", "Software and Game Development", "ASMR",
        "Pools, Hot Tubs, and Beaches", "Travel & Outdoors", "Food & Drink",
        "Makers & Crafting", "Beauty & Body Art", "Fitness & Health",
        "Animals, Aquariums, and Zoos", "Politics", "Crypto", "Virtual Casino", "Slots",
    )  # fmt: skip


class AttentionWeights(_Strict):
    """Attention score (0-100) weights. Heuristic and versioned, not validated outcomes."""

    rank_improvement: float = Field(default=0.25, ge=0)
    viewer_growth: float = Field(default=0.25, ge=0)
    stream_growth: float = Field(default=0.10, ge=0)
    acceleration: float = Field(default=0.10, ge=0)
    sustained: float = Field(default=0.15, ge=0)
    new_in_top: float = Field(default=0.15, ge=0)
    volatility_penalty: float = Field(default=0.10, ge=0)


class AttentionSettings(_Strict):
    min_interval_minutes: int = Field(default=9, ge=1)
    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    rising_pct_per_hour: float = Field(default=15.0, gt=0)  # viewers, comparable samples
    falling_pct_per_hour: float = Field(default=-15.0, lt=0)
    rank_climb_positions: int = Field(default=5, ge=1)  # within the window
    new_in_top_min_prior_snapshots: int = Field(default=3, ge=1)
    # CUSUM on standardised log-changes (Phase 4 primitive, Twitch-specific parameters)
    cusum_k: float = Field(default=0.5, gt=0)
    cusum_h: float = Field(default=3.0, gt=0)
    cusum_z_clip: float = Field(default=2.5, gt=0)
    min_intervals_for_sustained: int = Field(default=4, ge=3)
    weights: AttentionWeights = AttentionWeights()

    @model_validator(mode="after")
    def _cusum_needs_several_periods(self) -> AttentionSettings:
        if self.cusum_z_clip - self.cusum_k >= self.cusum_h:
            raise ValueError("attention: cusum_z_clip - cusum_k must be < cusum_h")
        return self


class PromotionSettings(_Strict):
    enabled: bool = True
    min_attention_score: float = Field(default=45.0, ge=0, le=100)
    max_per_run: int = Field(default=3, ge=0, le=20)
    require_igdb_id: bool = True  # only catalog entries IGDB knows as games
    cooldown_hours: int = Field(default=24, ge=1)


class RetentionSettings(_Strict):
    hours: int = Field(default=POLICY_CACHE_HOURS, ge=1)
    extended_storage_authorized: bool = False

    @model_validator(mode="after")
    def _policy(self) -> RetentionSettings:
        if self.hours > POLICY_CACHE_HOURS and not self.extended_storage_authorized:
            raise ValueError(
                f"retention.hours > {POLICY_CACHE_HOURS} needs written authorisation from "
                "Twitch (set retention.extended_storage_authorized: true only if you have it)"
            )
        return self


class TwitchSettings(_Strict):
    helix_base_url: str = "https://api.twitch.tv/helix"
    auth_base_url: str = "https://id.twitch.tv/oauth2"
    client: ClientSettings = ClientSettings()
    collection: CollectionSettings = CollectionSettings()
    cadence: CadenceSettings = CadenceSettings()
    monitor: MonitorSettings = MonitorSettings()
    attention: AttentionSettings = AttentionSettings()
    promotion: PromotionSettings = PromotionSettings()
    retention: RetentionSettings = RetentionSettings()


def load_twitch_settings(params: dict[str, Any]) -> TwitchSettings:
    try:
        settings = TwitchSettings.model_validate(params or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(
            f"discovery.yaml: providers.twitch.params.{where}: {first['msg']}"
        ) from None
    for url in (settings.helix_base_url, settings.auth_base_url):
        if not url.startswith("https://"):
            raise ConfigError("providers.twitch.params: API base URLs must use https://")
    return settings
