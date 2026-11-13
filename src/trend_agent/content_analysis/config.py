"""Phase 5 configuration (``config/content_analysis.yaml``), validated strictly.

Category shares (AI 45 %, technology 25 %, gaming 30 %) are resource-allocation priorities
for the analysis queue - not measurements of audience demand.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.content_opportunity import DemandBasis
from trend_agent.schemas.search_intent import PrimaryIntent, SubIntent

ANALYSIS_VERSION = "content-1.0.0"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Retrieval(_Strict):
    max_results_per_variant: int = Field(default=25, ge=1, le=50)
    max_variants: int = Field(default=2, ge=1, le=5)
    expand_when_relevant_below: int = Field(
        default=3, ge=0, description="fetch a further variant only if fewer relevant results"
    )
    cache_ttl: timedelta = timedelta(hours=24)
    default_region: str = "US"
    default_language: str = "en"
    fetch_details: bool = True  # videos.list: duration, language (1 unit per 50 videos)


class Relevance(_Strict):
    relevant_threshold: float = Field(default=45.0, ge=0, le=100)
    high_threshold: float = Field(default=70.0, ge=0, le=100)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "entity": 0.35,
            "intent": 0.25,
            "title": 0.20,
            "description": 0.10,
            "keywords": 0.10,
        }
    )

    @model_validator(mode="after")
    def _sum(self) -> Relevance:
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("relevance weights must sum to 1")
        return self


class Freshness(_Strict):
    recent_window: dict[str, timedelta] = Field(
        default_factory=lambda: {
            "ai": timedelta(days=60),
            "technology": timedelta(days=120),
            "gaming": timedelta(days=30),
        }
    )
    evergreen_sub_intents: tuple[SubIntent, ...] = (
        SubIntent.PRODUCT_REVIEW,
        SubIntent.HARDWARE_BENCHMARK,
        SubIntent.STRATEGY_GUIDE,
        SubIntent.SECURITY_EXPLANATION,
        SubIntent.PROGRAMMING_TUTORIAL,
    )
    time_sensitive_intents: tuple[PrimaryIntent, ...] = (PrimaryIntent.NEWS,)
    time_sensitive_sub_intents: tuple[SubIntent, ...] = (
        SubIntent.MODEL_INSTALLATION,
        SubIntent.LOCAL_MODEL_SETUP,
        SubIntent.API_INTEGRATION,
        SubIntent.RELEASE_EXPLANATION,
        SubIntent.MODEL_PRICING,
        SubIntent.VERSION_MIGRATION,
        SubIntent.SOFTWARE_INSTALLATION,
        SubIntent.ITEM_LOCATION,
        SubIntent.EVENT_GUIDE,
        SubIntent.CHALLENGE_COMPLETION,
        SubIntent.GAME_UPDATE,
        SubIntent.REWARD_UNLOCK,
        SubIntent.WEAPON_GUIDE,
    )


class Gaps(_Strict):
    min_inspected: int = Field(default=8, ge=1, description="below: INSUFFICIENT_EVIDENCE")
    adequate_relevant_current: int = Field(
        default=3, ge=1, description="relevant, current, matching results that answer the need"
    )
    low_relevance_best_below: float = Field(
        default=70.0, description="LOW_RELEVANCE when the best relevant result is below this"
    )
    version_mismatch_share: float = Field(default=0.6, gt=0, le=1)
    outdated_share: float = Field(default=0.6, gt=0, le=1)
    max_current_version_results_for_gap: int = 1
    short_form_seconds: int = 75
    format_gap_share: float = 0.7
    language_gap_share: float = 0.7
    demonstration_formats: tuple[str, ...] = ("tutorial", "demonstration", "guide")


class Scoring(_Strict):
    score_version: str = "opportunity-score-1.0"
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "demand_evidence": 0.25,
            "intent_clarity": 0.10,
            "content_gap_strength": 0.25,
            "supply_relevance": 0.15,
            "freshness_opportunity": 0.10,
            "evidence_quality": 0.15,
        }
    )
    min_available_weight: float = Field(
        default=0.6, gt=0, le=1, description="below: score unavailable"
    )
    caps: dict[DemandBasis, float] = Field(
        default_factory=lambda: {
            DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE: 100.0,
            DemandBasis.DIRECT_SEARCH_CANDIDATE: 85.0,
            DemandBasis.EXTERNAL_SEARCH_SURGE: 70.0,
            DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY: 55.0,
            DemandBasis.COLD_START_GROWTH_CANDIDATE: 50.0,
            DemandBasis.WATCH: 45.0,
            DemandBasis.INSUFFICIENT_EVIDENCE: 35.0,
            DemandBasis.MANUAL_NO_DEMAND_EVIDENCE: 50.0,
        }
    )
    demand_values: dict[DemandBasis, float] = Field(
        default_factory=lambda: {
            DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE: 1.0,
            DemandBasis.DIRECT_SEARCH_CANDIDATE: 0.75,
            DemandBasis.EXTERNAL_SEARCH_SURGE: 0.5,
            DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY: 0.35,
            DemandBasis.COLD_START_GROWTH_CANDIDATE: 0.3,
            DemandBasis.WATCH: 0.2,
            DemandBasis.INSUFFICIENT_EVIDENCE: 0.1,
        }
    )

    @model_validator(mode="after")
    def _sum(self) -> Scoring:
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("score weights must sum to 1")
        return self


class Scheduling(_Strict):
    category_allocation: dict[str, float] = Field(
        default_factory=lambda: {"ai": 0.45, "technology": 0.25, "gaming": 0.30}
    )
    basis_priority: dict[DemandBasis, float] = Field(
        default_factory=lambda: {
            DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE: 100.0,
            DemandBasis.DIRECT_SEARCH_CANDIDATE: 80.0,
            DemandBasis.EXTERNAL_SEARCH_SURGE: 60.0,
            DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY: 45.0,
            DemandBasis.COLD_START_GROWTH_CANDIDATE: 40.0,
            DemandBasis.WATCH: 20.0,
            DemandBasis.INSUFFICIENT_EVIDENCE: 5.0,
            DemandBasis.MANUAL_NO_DEMAND_EVIDENCE: 30.0,
        }
    )
    urgency_boost: dict[str, float] = Field(
        default_factory=lambda: {
            "new_ai_model": 15.0,
            "time_sensitive_game_event": 15.0,
            "technology_announcement": 10.0,
        }
    )
    reanalyse_after: timedelta = timedelta(hours=72)
    daily_search_requests: int = Field(default=40, ge=0, description="search.list calls/day")
    daily_llm_tokens: int = Field(default=200_000, ge=0)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    max_per_run: int = Field(default=10, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def _shares(self) -> Scheduling:
        if abs(sum(self.category_allocation.values()) - 1.0) > 1e-6:
            raise ValueError("category allocation must sum to 1")
        return self


class LLMSettings(_Strict):
    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    max_tokens: int = Field(default=4000, ge=256, le=64000)
    timeout_seconds: float = Field(default=90.0, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=4)
    server_side_fallbacks: bool = True
    input_usd_per_mtok: float = 4.0
    output_usd_per_mtok: float = 20.0


class Policy(_Strict):
    """Topics that are never content opportunities (cheats, exploits, account abuse)."""

    prohibited_patterns: tuple[str, ...] = (
        r"\b(aim ?bot|wall ?hack|cheat engine|mod menu|esp hack|triggerbot)\b",
        r"\b(cheats?|hacks?)\b.{0,20}\b(free|undetected|download|working)\b",
        r"\bundetected\b",
        r"\b(bypass|evade|disable)\b.{0,20}\b(anti[- ]?cheat|easy anti|vanguard|battleye)\b",
        r"\b(free|unlimited)\b.{0,15}\b(v-?bucks|robux|gems|coins|currency)\b.{0,15}"
        r"\b(generator|glitch|hack)\b",
        r"\b(hack|steal|crack)\b.{0,20}\b(account|password|login)s?\b",
        r"\b(dupe|duplication) (glitch|exploit)\b",
        r"\bexploit\b.{0,20}\b(script|executor|injector)\b",
    )


class ContentAnalysisConfig(_Strict):
    version: int = 1
    config_version: str = "content-analysis-v1"
    retrieval: Retrieval = Retrieval()
    relevance: Relevance = Relevance()
    freshness: Freshness = Freshness()
    gaps: Gaps = Gaps()
    scoring: Scoring = Scoring()
    scheduling: Scheduling = Scheduling()
    llm: LLMSettings = LLMSettings()
    policy: Policy = Policy()

    @model_validator(mode="after")
    def _consistent(self) -> ContentAnalysisConfig:
        r = self.relevance
        if r.high_threshold < r.relevant_threshold:
            raise ValueError("relevance.high_threshold must be >= relevant_threshold")
        if self.gaps.low_relevance_best_below <= r.relevant_threshold:
            # Every relevant result scores >= relevant_threshold, so the rule could never fire.
            raise ValueError("gaps.low_relevance_best_below must exceed relevant_threshold")
        return self


class LoadedContentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path | None
    config: ContentAnalysisConfig
    digest: str

    @property
    def version_tag(self) -> str:
        return f"{self.config.config_version}@{self.digest}"


def load_content_config(directory: str | Path | None = None) -> LoadedContentConfig:
    root = resolve_config_dir(directory)
    path = root / "content_analysis.yaml"
    if not path.is_file():
        raise ConfigError(f"content-analysis configuration not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    try:
        config = ContentAnalysisConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None
    return LoadedContentConfig(
        path=path, config=config, digest=hashlib.sha256(raw.encode()).hexdigest()[:12]
    )


def default_content_config() -> LoadedContentConfig:
    return LoadedContentConfig(path=None, config=ContentAnalysisConfig(), digest="defaults")
