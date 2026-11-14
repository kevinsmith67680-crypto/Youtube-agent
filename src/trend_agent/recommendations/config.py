"""Phase 7 configuration (``config/recommendations.yaml``), validated strictly.

Ranking weights are experimental engineering choices, versioned with the scoring version;
they are not scientifically optimised. Every value has an in-code default.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.recommendation import ProductionProfile

RECOMMENDATION_VERSION = "recommendations-1.0.0"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ranking(_Strict):
    version: str = "ranking-1.0"
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "demand_evidence": 0.25,
            "content_gap": 0.20,
            "intent_alignment": 0.15,
            "forecast_opportunity": 0.15,
            "creator_relevance": 0.10,
            "production_feasibility": 0.10,
            "concept_originality": 0.05,
        }
    )
    missing_policy: dict[str, str] = Field(
        default_factory=lambda: {
            # exclude = drop from the weighted mean and report it (never zero, never max)
            "demand_evidence": "exclude",
            "content_gap": "exclude",
            "intent_alignment": "exclude",
            "forecast_opportunity": "exclude",
            "creator_relevance": "exclude",
            "production_feasibility": "exclude",
            "concept_originality": "exclude",
        }
    )
    min_available_weight: float = Field(default=0.6, gt=0, le=1)
    require_demand_evidence: bool = True
    evidence_quality_factor: dict[str, float] = Field(
        default_factory=lambda: {
            "APPROVED": 1.0,
            "APPROVED_WITH_UNCERTAINTY": 0.85,
        }
    )
    gap_evidence_factor: dict[str, float] = Field(
        default_factory=lambda: {"strong": 1.0, "moderate": 0.92, "weak": 0.8}
    )

    @model_validator(mode="after")
    def _check(self) -> Ranking:
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("ranking weights must sum to 1")
        if set(self.missing_policy) != set(self.weights):
            raise ValueError("missing_policy must name every weighted component")
        if any(v not in ("exclude", "neutral") for v in self.missing_policy.values()):
            raise ValueError("missing_policy values are 'exclude' or 'neutral'")
        return self


class Generation(_Strict):
    max_concepts_per_opportunity: int = Field(default=5, ge=1, le=5)
    max_opportunities_per_run: int = Field(default=25, ge=1, le=500)
    eligible_statuses: tuple[str, ...] = ("candidate",)
    research_hypotheses: bool = Field(
        default=True,
        description="insufficient-evidence opportunities yield one labelled research "
        "candidate instead of nothing",
    )
    min_opportunity_score: float | None = Field(default=None, ge=0, le=100)
    max_forecast_age_hours: float = Field(default=36.0, gt=0)
    max_report_age_hours: float = Field(default=24.0 * 7, gt=0)
    hooks_per_concept: int = Field(default=3, ge=1, le=6)
    max_title_chars: int = Field(default=70, ge=30, le=100)
    hard_max_title_chars: int = Field(default=100, ge=40, le=160)
    hook_words: tuple[int, int] = (8, 45)  # ~5-15 seconds at 150-180 words/minute


class ProfileHours(_Strict):
    hours: float = Field(gt=0)
    description: str


class Feasibility(_Strict):
    profiles: dict[ProductionProfile, ProfileHours] = Field(
        default_factory=lambda: {
            ProductionProfile.QUICK_RESPONSE: ProfileHours(hours=2, description="quick response"),
            ProductionProfile.STANDARD_TUTORIAL: ProfileHours(hours=8, description="tutorial"),
            ProductionProfile.DEEP_TECHNICAL_VIDEO: ProfileHours(
                hours=24, description="deep technical video"
            ),
        }
    )
    # Phase 6 opportunity-window profiles each production profile corresponds to
    window_profile: dict[ProductionProfile, str] = Field(
        default_factory=lambda: {
            ProductionProfile.QUICK_RESPONSE: "QUICK_REACTION",
            ProductionProfile.STANDARD_TUTORIAL: "STANDARD_TUTORIAL",
            ProductionProfile.DEEP_TECHNICAL_VIDEO: "DEEP_DIVE",
        }
    )
    unknown_requirement_hours: float = Field(default=0.75, ge=0)
    verified_result_testing_factor: float = Field(default=1.5, ge=1)


class Diversity(_Strict):
    title_similarity_merge: float = Field(default=0.6, gt=0, le=1)
    max_per_opportunity: int = Field(default=3, ge=1, le=5)
    max_per_format_family_per_topic: int = Field(default=1, ge=1, le=5)
    max_share_per_category: float = Field(default=0.6, gt=0, le=1)


class LLM(_Strict):
    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    max_output_tokens: int = Field(default=4096, ge=256, le=32000)
    max_attempts: int = Field(default=2, ge=1, le=3)
    usd_per_million_input_tokens: float | None = Field(
        default=4.0, description="list price for the configured model; set your contract price"
    )
    usd_per_million_output_tokens: float | None = Field(default=20.0)
    daily_token_budget: int = Field(default=400_000, ge=0)
    max_concurrency: int = Field(default=2, ge=1, le=8)


class Alerts(_Strict):
    min_score_new_recommendation: float = Field(default=70.0, ge=0, le=100)
    min_opportunity_score: float = Field(default=65.0, ge=0, le=100)
    score_upgrade_delta: float = Field(default=10.0, gt=0)
    cooldown_hours: float = Field(default=24.0, ge=0)
    max_delivery_attempts: int = Field(default=3, ge=1, le=10)
    alert_on_synthetic: bool = Field(
        default=False, description="never true in production; demo mode alerts stay in-app"
    )


class Api(_Strict):
    page_size_default: int = Field(default=25, ge=1, le=200)
    page_size_max: int = Field(default=100, ge=1, le=500)
    rate_limit_per_minute: int = Field(default=240, ge=1)
    generate_rate_limit_per_hour: int = Field(default=12, ge=1)
    max_generate_opportunities: int = Field(default=10, ge=1, le=100)


class RecommendationConfig(_Strict):
    version: int = 1
    config_version: str = "recommendations-v1"
    ranking: Ranking = Ranking()
    generation: Generation = Generation()
    feasibility: Feasibility = Feasibility()
    diversity: Diversity = Diversity()
    llm: LLM = LLM()
    alerts: Alerts = Alerts()
    api: Api = Api()


class LoadedRecommendationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path | None
    config: RecommendationConfig
    digest: str

    @property
    def version_tag(self) -> str:
        return f"{self.config.config_version}@{self.digest}"


def load_recommendation_config(directory: str | Path | None = None) -> LoadedRecommendationConfig:
    root = resolve_config_dir(directory)
    path = root / "recommendations.yaml"
    if not path.is_file():
        raise ConfigError(f"recommendation configuration not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    try:
        config = RecommendationConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None
    return LoadedRecommendationConfig(
        path=path, config=config, digest=hashlib.sha256(raw.encode()).hexdigest()[:12]
    )


def default_recommendation_config() -> LoadedRecommendationConfig:
    return LoadedRecommendationConfig(path=None, config=RecommendationConfig(), digest="defaults")
