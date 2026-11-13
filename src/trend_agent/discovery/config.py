"""Discovery configuration (YAML), validated with pydantic.

Layout (directory resolved by :func:`resolve_config_dir`)::

    config/
      discovery.yaml            budgets, limits, scoring weights, provider settings
      seeds/<category>.yaml     seed terms + curated entity gazetteer per category
      sources/*.yaml            feeds, channels, repositories, communities

Everything that decides *what* to look at lives here, so adding a game, AI product or
feed is a configuration change, not a code change. Seeds are starting points only: they
are never evidence of search interest.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trend_agent.discovery.models import (
    Category,
    DiscoverySource,
    EntityType,
    EvidenceType,
    SourcePool,
    SourceType,
)
from trend_agent.utils.codes import normalize_language, normalize_region

CONFIG_DIR_ENV = "TREND_AGENT_DISCOVERY_CONFIG_DIR"


class ConfigError(ValueError):
    """Raised for missing or invalid discovery configuration."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CategoryBudgets(_Strict):
    ai: float = Field(default=0.45, ge=0, le=1)
    technology: float = Field(default=0.25, ge=0, le=1)
    gaming: float = Field(default=0.30, ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self) -> CategoryBudgets:
        total = self.ai + self.technology + self.gaming
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"category budgets must sum to 1.0 (got {total:.4f})")
        return self

    def share(self, category: Category) -> float:
        return float(getattr(self, category.value, 0.0))


class RunConfig(_Strict):
    max_fetches_per_run: int = Field(default=24, ge=1, le=1000)
    max_items_per_source: int = Field(default=50, ge=1, le=500)
    lookback_days: int = Field(default=14, ge=1, le=365)
    max_item_age_days: int = Field(default=365, ge=1)
    fetch_concurrency: int = Field(default=4, ge=1, le=32)
    fetch_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_failure_backoff_multiplier: int = Field(default=16, ge=1, le=1024)


class ExpansionConfig(_Strict):
    max_depth: int = Field(default=2, ge=0, le=2, description="0 none, 1 templates, 2 +comparisons")
    max_templates_per_entity: int = Field(default=10, ge=0, le=50)
    max_comparisons_per_entity: int = Field(default=2, ge=0, le=10)
    max_keywords_per_run: int = Field(default=400, ge=1, le=10_000)
    min_entity_confidence: float = Field(default=0.6, ge=0, le=1)
    max_query_words: int = Field(default=10, ge=2, le=20)


class ClusteringConfig(_Strict):
    backend: str = Field(default="lexical", pattern=r"^(lexical|embedding)$")
    intent_similarity_threshold: float = Field(default=0.5, gt=0, le=1)
    topic_similarity_threshold: float = Field(default=0.6, gt=0, le=1)


class ScoringWeights(_Strict):
    novelty: float = 0.25
    freshness: float = 0.20
    corroboration: float = 0.20
    relevance: float = 0.15
    intent_actionability: float = 0.10
    evidence_quality: float = 0.10

    @model_validator(mode="after")
    def _sums_to_one(self) -> ScoringWeights:
        total = sum(self.model_dump().values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"scoring weights must sum to 1.0 (got {total:.4f})")
        if any(v < 0 for v in self.model_dump().values()):
            raise ValueError("scoring weights must be non-negative")
        return self


class ScoringConfig(_Strict):
    version: str = "priority-v1"
    weights: ScoringWeights = ScoringWeights()
    freshness_half_life_days: float = Field(default=7.0, gt=0)
    recency_half_life_days: float = Field(default=7.0, gt=0)
    prioritize_threshold: float = Field(default=45.0, ge=0, le=100)


class LifecycleConfig(_Strict):
    dormant_after_days: int = Field(default=30, ge=1)


class ProviderSettings(_Strict):
    enabled: bool = True
    daily_request_limit: int = Field(default=500, ge=0)
    retention_days: int | None = Field(
        default=None, ge=1, description="Expiry of stored items from this provider"
    )
    params: dict[str, Any] = Field(default_factory=dict)


class DiscoveryConfig(_Strict):
    version: int = 1
    category_budgets: CategoryBudgets = CategoryBudgets()
    exploration_share: float = Field(default=0.20, ge=0, le=1)
    regions: tuple[str, ...] = ("US",)
    languages: tuple[str, ...] = ("en",)
    run: RunConfig = RunConfig()
    expansion: ExpansionConfig = ExpansionConfig()
    clustering: ClusteringConfig = ClusteringConfig()
    scoring: ScoringConfig = ScoringConfig()
    lifecycle: LifecycleConfig = LifecycleConfig()
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)

    @field_validator("regions")
    @classmethod
    def _regions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_region(r, allow_world=False, allow_subdivision=False) for r in value)

    @field_validator("languages")
    @classmethod
    def _languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_language(lang) for lang in value)

    def provider(self, name: str) -> ProviderSettings:
        return self.providers.get(name, ProviderSettings())


class SeedTerm(_Strict):
    term: str = Field(min_length=2, max_length=120)
    subcategory: str = "general"
    priority: int = Field(default=50, ge=0, le=100)
    entity_type: EntityType | None = None
    regions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    enabled: bool = True
    search: bool = Field(default=True, description="Use as a query for query-based providers")
    track_as_keyword: bool = Field(
        default=False, description="Also keep the seed itself as a candidate keyword"
    )


class GazetteerEntity(_Strict):
    """A curated, known entity. Used for grounding and alias resolution, never as evidence."""

    name: str = Field(min_length=1, max_length=120)
    entity_type: EntityType
    aliases: tuple[str, ...] = ()
    context_terms: tuple[str, ...] = Field(
        default=(), description="Words that disambiguate this entity from same-name ones"
    )
    subcategory: str = "general"
    priority: int = Field(default=50, ge=0, le=100)
    evergreen: bool = True
    case_sensitive_aliases: tuple[str, ...] = Field(
        default=(), description="Aliases matched case-sensitively (e.g. short acronyms)"
    )


class CategorySeeds(_Strict):
    category: Category
    seeds: tuple[SeedTerm, ...] = ()
    entities: tuple[GazetteerEntity, ...] = ()


class _SourceEntry(_Strict):
    key: str
    provider: str
    name: str
    source_type: SourceType
    category: Category
    reference: str
    pool: SourcePool = SourcePool.EXPLOITATION
    enabled: bool = True
    reliability: float = 0.7
    min_poll_interval_minutes: int = 360
    evidence_type: EvidenceType | None = None
    publisher: str | None = None
    region: str | None = None
    language: str | None = None
    entity_hint: str | None = None
    entity_hint_type: str | None = None
    verified: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class _SourcesFile(_Strict):
    sources: tuple[_SourceEntry, ...] = ()


class LoadedDiscoveryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    directory: Path | None
    config: DiscoveryConfig
    seeds: dict[Category, CategorySeeds]
    sources: tuple[DiscoverySource, ...]

    def gazetteer(self) -> list[tuple[Category, GazetteerEntity]]:
        return [(cat, entity) for cat, file in self.seeds.items() for entity in file.entities]

    def enabled_seeds(self, category: Category) -> list[SeedTerm]:
        file = self.seeds.get(category)
        return [s for s in file.seeds if s.enabled] if file else []


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:80] or "x"


def resolve_config_dir(explicit: str | Path | None = None) -> Path:
    """Find the configuration directory.

    An explicitly given directory (argument, settings or ``TREND_AGENT_DISCOVERY_CONFIG_DIR``)
    must exist: it is never silently replaced by another configuration. Without one,
    ``./config`` and then the repository's ``config/`` are tried.
    """
    named = explicit or os.environ.get(CONFIG_DIR_ENV)
    if named:
        candidates = [Path(named)]
    else:
        candidates = [Path.cwd() / "config"]
        candidates.extend(parent / "config" for parent in Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "discovery.yaml").is_file():
            return candidate
    raise ConfigError(
        "discovery configuration not found; expected config/discovery.yaml "
        f"(set {CONFIG_DIR_ENV} to its directory)"
    )


def _read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None


def _validated[T: BaseModel](model: type[T], data: Any, path: Path) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None


def load_discovery_config(directory: str | Path | None = None) -> LoadedDiscoveryConfig:
    root = resolve_config_dir(directory)
    config = _validated(
        DiscoveryConfig, _read_yaml(root / "discovery.yaml"), root / "discovery.yaml"
    )

    seeds: dict[Category, CategorySeeds] = {}
    for path in sorted((root / "seeds").glob("*.yaml")):
        file = _validated(CategorySeeds, _read_yaml(path), path)
        if file.category in seeds:
            raise ConfigError(f"{path}: duplicate seed file for category {file.category.value}")
        seeds[file.category] = file

    sources: list[DiscoverySource] = []
    for path in sorted((root / "sources").glob("*.yaml")):
        sources_file = _validated(_SourcesFile, _read_yaml(path), path)
        for entry in sources_file.sources:
            try:
                sources.append(DiscoverySource(**entry.model_dump()))
            except ValidationError as exc:
                raise ConfigError(
                    f"{path}: source {entry.key!r}: {exc.errors()[0]['msg']}"
                ) from None

    keys = [s.key for s in sources]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ConfigError(f"duplicate source keys: {', '.join(duplicates)}")
    return LoadedDiscoveryConfig(directory=root, config=config, seeds=seeds, sources=tuple(sources))
