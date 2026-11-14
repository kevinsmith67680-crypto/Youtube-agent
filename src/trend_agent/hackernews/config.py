"""Hacker News provider settings, read from ``providers.hackernews_firebase.params``.

Polling cadence per list lives on the sources (``config/sources/hackernews.yaml``,
``min_poll_interval_minutes``) like every other source; this file holds client limits,
classification terms, ranking weights, refresh tiers and momentum thresholds.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import Category


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientSettings(_Strict):
    # The API documents no rate limit; that is not permission to hammer it.
    daily_request_budget: int = Field(default=20_000, ge=1)
    max_concurrency: int = Field(default=6, ge=1, le=32)
    item_cache_seconds: int = Field(default=120, ge=0, le=3600)
    list_cache_seconds: int = Field(default=60, ge=0, le=3600)
    max_items_per_list: int = Field(default=60, ge=1, le=500)


class ClassifierSettings(_Strict):
    # one specific term in the title (score 1.0 -> relevance 0.33) is enough
    min_relevance: float = Field(default=0.3, ge=0, le=1)
    # A second category within this fraction of the first marks the story ambiguous
    # (both are kept; nothing is forced into a category to fill an allocation).
    ambiguity_ratio: float = Field(default=0.8, gt=0, le=1)
    secondary_ratio: float = Field(default=0.5, gt=0, le=1)
    terms: dict[Category, tuple[str, ...]] = Field(default_factory=dict)
    domains: dict[Category, tuple[str, ...]] = Field(default_factory=dict)


class PriorityWeights(_Strict):
    """Story priority (0-100) weights. Heuristic, not validated against outcomes."""

    relevance: float = Field(default=0.25, ge=0)
    novelty: float = Field(default=0.10, ge=0)
    recency: float = Field(default=0.15, ge=0)
    discussion_activity: float = Field(default=0.15, ge=0)
    launch_significance: float = Field(default=0.15, ge=0)
    corroboration: float = Field(default=0.08, ge=0)
    entity_verification: float = Field(default=0.07, ge=0)
    source_reliability: float = Field(default=0.05, ge=0)


class RefreshSettings(_Strict):
    high_priority_minutes: int = Field(default=15, ge=5)
    normal_priority_minutes: int = Field(default=60, ge=5)
    low_priority_minutes: int = Field(default=240, ge=5)
    high_priority_threshold: float = Field(default=60.0, ge=0, le=100)
    normal_priority_threshold: float = Field(default=35.0, ge=0, le=100)
    stop_after_hours: int = Field(default=72, ge=1)
    max_per_run: int = Field(default=60, ge=1, le=500)
    # A tracked story missing from /updates.json is still refreshed after this many
    # of its intervals (an unchanged story needs no request before then).
    stale_interval_multiplier: int = Field(default=4, ge=1)


class MomentumSettings(_Strict):
    min_interval_minutes: int = Field(default=10, ge=1)
    rising_score_per_hour: float = Field(default=20.0, gt=0)
    rising_comments_per_hour: float = Field(default=15.0, gt=0)


class HackerNewsSettings(_Strict):
    base_url: str = "https://hacker-news.firebaseio.com/v0"
    client: ClientSettings = ClientSettings()
    classifier: ClassifierSettings = ClassifierSettings()
    priority: PriorityWeights = PriorityWeights()
    refresh: RefreshSettings = RefreshSettings()
    momentum: MomentumSettings = MomentumSettings()
    list_reliability: dict[str, float] = Field(
        default_factory=lambda: {
            "beststories": 0.8,
            "topstories": 0.75,
            "showstories": 0.7,
            "newstories": 0.6,
            "askstories": 0.5,
            "refresh": 0.6,
        }
    )


def load_hackernews_settings(params: dict[str, Any]) -> HackerNewsSettings:
    try:
        settings = HackerNewsSettings.model_validate(params or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(
            f"discovery.yaml: providers.hackernews_firebase.params.{where}: {first['msg']}"
        ) from None
    if not settings.base_url.startswith("https://"):
        raise ConfigError("providers.hackernews_firebase.params.base_url must use https://")
    return settings
