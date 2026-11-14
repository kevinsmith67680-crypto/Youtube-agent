"""GitHub provider settings, read from ``providers.github.params`` in ``discovery.yaml``.

Search *sources* (what to look for) live in ``config/sources/github_searches.yaml`` and
monitored repositories in ``config/sources/repositories.yaml``, like every other source.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import Category


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiSettings(_Strict):
    per_page: int = Field(default=30, ge=1, le=100)
    max_pages: int = Field(default=1, ge=1, le=10)
    release_page_size: int = Field(default=10, ge=1, le=100)
    # Local daily budgets (UTC). GitHub allows 5,000 core requests/hour and 30 searches/
    # minute with a token (60/hour and 10/minute without); these stay far below that.
    core_daily_budget: int = Field(default=1500, ge=1)
    search_daily_budget: int = Field(default=300, ge=1)
    # Stop before GitHub's live limit reaches zero (other tools may share the token).
    min_remaining_core: int = Field(default=25, ge=0)
    min_remaining_search: int = Field(default=2, ge=0)
    dedup_ttl_seconds: int = Field(default=600, ge=0, le=86400)


class StrategySettings(_Strict):
    """A search strategy: how a base query is narrowed at fetch time."""

    created_within_days: int | None = Field(default=None, ge=1, le=3650)
    pushed_within_days: int | None = Field(default=None, ge=1, le=3650)
    min_stars: int = Field(default=0, ge=0)
    sort: str = Field(default="stars", pattern="^(stars|forks|updated|best_match)$")
    exclude_forks: bool = True
    exclude_archived: bool = True


class QualitySettings(_Strict):
    min_description_chars: int = Field(default=20, ge=0, le=300)
    min_relevance: float = Field(default=0.34, ge=0, le=1)
    # A burst of stars on a days-old repository with almost no forks is flagged (never
    # rewarded): it can be genuine, bought or automated; GitHub data cannot tell.
    star_anomaly_min_stars: int = Field(default=300, ge=1)
    star_anomaly_max_age_days: int = Field(default=3, ge=1)
    star_anomaly_max_fork_ratio: float = Field(default=0.005, ge=0, le=1)
    # A very young, very small repository gaining stars quickly (a common pattern of
    # star-farmed lure repositories) is flagged too; it can also be a genuine launch.
    young_star_max_age_days: float = Field(default=2.0, gt=0)
    young_star_min_rate_per_day: float = Field(default=75.0, gt=0)
    young_star_max_size_kb: int = Field(default=1000, ge=1)
    # Excluded: many stars, almost no forks, little code. Organic projects observed live
    # had forks at 4-10% of stars; a farmed wave on 2026-11-14 had ~1,200 stars, 0-2 forks.
    fake_star_min_stars: int = Field(default=200, ge=1)
    fake_star_max_fork_ratio: float = Field(default=0.01, ge=0, le=1)
    fake_star_max_size_kb: int = Field(default=2000, ge=1)
    # Excluded: >= cohort_min_size results created within cohort_window_hours of each other
    # whose star counts all lie within cohort_star_tolerance of their median.
    cohort_min_size: int = Field(default=3, ge=2)
    cohort_window_hours: float = Field(default=6.0, gt=0)
    cohort_star_tolerance: float = Field(default=0.1, gt=0, le=1)
    cohort_min_stars: int = Field(default=100, ge=1)
    # Names too generic to identify a project in a search query ("how to use studio").
    generic_names: tuple[str, ...] = (
        "test", "demo", "app", "project", "code", "repo", "main", "agent", "agents", "bot",
        "ai", "llm", "gpt", "model", "models", "tool", "tools", "src", "lib", "web", "api",
        "sdk", "cli", "studio", "harness", "one", "two", "three", "hub", "core", "kit", "box",
        "lab", "labs", "note", "notes", "chat", "assistant", "server", "client", "engine",
        "platform", "workspace", "playground", "dashboard", "template", "starter",
        "boilerplate", "example", "examples", "sample", "samples", "config", "dotfiles",
        "scripts", "utils", "docs", "website", "blog", "portfolio", "homework", "course",
        "tutorial", "workshop", "copilot", "data", "dataset", "notebook", "notebooks",
    )  # fmt: skip


class MomentumSettings(_Strict):
    # Two observations closer than this are not compared (a rate over minutes is noise).
    min_interval_hours: float = Field(default=6.0, gt=0)
    max_observations: int = Field(default=60, ge=2, le=1000)


class GitHubSettings(_Strict):
    api: ApiSettings = ApiSettings()
    strategies: dict[str, StrategySettings] = Field(
        default_factory=lambda: {
            "emerging": StrategySettings(created_within_days=21, min_stars=2, sort="stars"),
            "active": StrategySettings(pushed_within_days=7, min_stars=500, sort="updated"),
        }
    )
    quality: QualitySettings = QualitySettings()
    momentum: MomentumSettings = MomentumSettings()
    relevance_terms: dict[Category, tuple[str, ...]] = Field(default_factory=dict)


def load_github_settings(params: dict[str, Any]) -> GitHubSettings:
    try:
        return GitHubSettings.model_validate(params or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(
            f"discovery.yaml: providers.github.params.{where}: {first['msg']}"
        ) from None
