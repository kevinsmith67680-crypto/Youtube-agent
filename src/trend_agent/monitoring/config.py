"""Monitoring configuration (``config/monitoring.yaml``), validated strictly.

Everything operational lives here: budgets, activation thresholds, tiers, retry/lease
settings, provider freshness profiles, readiness requirements and data policies. The
``config_version`` is stored on every target so schedules can be traced to the rules that
produced them.
"""

from __future__ import annotations

import hashlib
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.discovery.models import CandidateStatus, Category, EntityType
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory, MonitoringTier
from trend_agent.schemas.scheduling import ProviderProfile


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Budgets(_Strict):
    ai: float = 0.45
    technology: float = 0.25
    gaming: float = 0.30

    @model_validator(mode="after")
    def _sum(self) -> Budgets:
        total = self.ai + self.technology + self.gaming
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"category budgets must sum to 1.0 (got {total:.4f})")
        return self

    def share(self, category: Category) -> float:
        return float(getattr(self, category.value, 0.0))


class Capacity(_Strict):
    max_active_targets: int = Field(default=1000, ge=1)
    max_active_proxy_targets: int = Field(default=1000, ge=0)
    max_jobs_per_cycle: int = Field(default=200, ge=1)
    max_queue_depth: int = Field(default=2000, ge=1)
    starvation_after: timedelta = timedelta(hours=72)
    planning_page_size: int = Field(default=500, ge=10)


class Activation(_Strict):
    keyword_statuses: tuple[CandidateStatus, ...] = (
        CandidateStatus.PRIORITIZED,
        CandidateStatus.MONITORING,
    )
    min_priority: float = Field(default=55.0, ge=0, le=100)
    deactivate_below: float = Field(default=40.0, ge=0, le=100)
    min_independent_origins: int = Field(default=1, ge=0)
    min_active: timedelta = timedelta(days=7)
    reactivation_cooldown: timedelta = timedelta(days=3)
    dormant_after_no_evidence: timedelta = timedelta(days=30)
    include_synthetic: bool = False

    @model_validator(mode="after")
    def _hysteresis(self) -> Activation:
        if self.deactivate_below > self.min_priority:
            raise ValueError("deactivate_below must not exceed min_priority (hysteresis)")
        return self


class TierRule(_Strict):
    min_priority: float = Field(default=0.0, ge=0, le=100)
    time_window: timedelta | None = Field(
        default=None, description="Entity must be this new (or event still running)"
    )
    recheck_after_no_data: timedelta = timedelta(hours=12)
    collect_every_periods: int = Field(default=1, ge=1, le=366)
    weight: float = Field(default=1.0, gt=0)


class Tiers(_Strict):
    critical: TierRule = TierRule(
        min_priority=75,
        time_window=timedelta(days=14),
        recheck_after_no_data=timedelta(hours=2),
        weight=4.0,
    )
    high: TierRule = TierRule(
        min_priority=65,
        time_window=timedelta(days=30),
        recheck_after_no_data=timedelta(hours=4),
        weight=3.0,
    )
    normal: TierRule = TierRule(min_priority=50, weight=2.0)
    low: TierRule = TierRule(recheck_after_no_data=timedelta(days=1), collect_every_periods=7)
    dormant: TierRule = TierRule(
        recheck_after_no_data=timedelta(days=7), collect_every_periods=28, weight=0.5
    )
    downgrade_hold: timedelta = timedelta(hours=24)

    def rule(self, tier: MonitoringTier) -> TierRule:
        rule: TierRule = getattr(self, tier.value)
        return rule


class Jobs(_Strict):
    max_attempts: int = Field(default=5, ge=1, le=50)
    retry_base: timedelta = timedelta(minutes=1)
    retry_max: timedelta = timedelta(hours=6)
    lease_duration: timedelta = timedelta(minutes=5)
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    claim_batch: int = Field(default=8, ge=1, le=500)
    stale_after_failures: int = Field(default=3, ge=1)
    unhealthy_after_failures: int = Field(default=3, ge=1)
    initial_lookback: timedelta = timedelta(days=30)
    collect_timeout: timedelta = timedelta(minutes=2)


class ReadinessProfile(_Strict):
    granularity: TimeGranularity = TimeGranularity.DAY
    min_observations: int = Field(default=14, ge=1)
    min_span: timedelta = timedelta(days=14)
    min_coverage: float = Field(default=0.8, gt=0, le=1)
    max_staleness_periods: int = Field(default=3, ge=0)
    partial_min_observations: int = Field(default=7, ge=1)


class Readiness(_Strict):
    default_profile: str = "short_term"
    profiles: dict[str, ReadinessProfile] = Field(
        default_factory=lambda: {"short_term": ReadinessProfile()}
    )
    provider_profiles: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known(self) -> Readiness:
        names = set(self.profiles)
        if self.default_profile not in names:
            raise ValueError(f"default readiness profile {self.default_profile!r} not defined")
        unknown = {v for v in self.provider_profiles.values() if v not in names}
        if unknown:
            raise ValueError(f"unknown readiness profiles: {sorted(unknown)}")
        return self


class QualityThresholds(_Strict):
    constant_run_periods: int = Field(default=14, ge=2)
    frequent_revision_ratio: float = Field(default=0.2, gt=0, le=1)
    unexpected_gap_periods: int = Field(default=3, ge=1)
    stale_after_periods: int = Field(default=3, ge=1)
    error_window: timedelta = timedelta(days=1)


class DataPolicySpec(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    version: str
    description: str
    retention_days: int | None = Field(default=None, ge=1)
    retention_from_setting: str | None = Field(
        default=None, description="Settings attribute holding the retention (None = deny)"
    )
    inherit_source_expiry: bool = False
    storage_allowed: bool = True
    requires_authorization: bool = False
    permissions: dict[str, bool] = Field(default_factory=dict)
    notes: str = ""

    @property
    def policy_id(self) -> str:
        return f"{self.id}@{self.version}"


class Compliance(_Strict):
    youtube_derived_metrics_permitted: bool = False
    youtube_statistics_monitoring: bool = False


class ProxyMonitoring(_Strict):
    enabled: bool = True
    provider: str = "discovery_evidence"


class MonitoringConfig(_Strict):
    version: int = 1
    config_version: str = "monitoring-v1"
    category_budgets: Budgets = Budgets()
    exploration_share: float = Field(default=0.20, ge=0, le=1)
    exploration_window: timedelta = timedelta(days=7)
    regions: tuple[str, ...] = ("US",)
    languages: tuple[str, ...] = ("en",)
    measurement_preferences: tuple[MeasurementCategory, ...] = (
        MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        MeasurementCategory.EXTERNAL_SEARCH_INTEREST,
    )
    provider_preferences: dict[MeasurementCategory, tuple[str, ...]] = Field(default_factory=dict)
    proxy_monitoring: ProxyMonitoring = ProxyMonitoring()
    capacity: Capacity = Capacity()
    activation: Activation = Activation()
    tiers: Tiers = Tiers()
    time_sensitive_entity_types: tuple[EntityType, ...] = (
        EntityType.GAME_EVENT,
        EntityType.GAME_UPDATE,
        EntityType.AI_MODEL,
        EntityType.AI_PRODUCT,
        EntityType.RELEASE,
    )
    jobs: Jobs = Jobs()
    readiness: Readiness = Readiness()
    quality: QualityThresholds = QualityThresholds()
    providers: dict[str, ProviderProfile] = Field(default_factory=dict)
    policies: tuple[DataPolicySpec, ...] = ()
    compliance: Compliance = Compliance()

    @model_validator(mode="after")
    def _references(self) -> MonitoringConfig:
        policy_ids = {p.id for p in self.policies}
        for name, profile in self.providers.items():
            if profile.name != name:
                raise ValueError(f"provider key {name!r} != profile name {profile.name!r}")
            if profile.retention_policy not in policy_ids:
                raise ValueError(
                    f"provider {name!r} references unknown policy {profile.retention_policy!r}"
                )
        for category, names in self.provider_preferences.items():
            missing = [n for n in names if n not in self.providers]
            if missing:
                raise ValueError(f"provider_preferences[{category.value}] unknown: {missing}")
        if self.proxy_monitoring.enabled and self.proxy_monitoring.provider not in self.providers:
            raise ValueError("proxy_monitoring.provider must be a configured provider")
        return self

    def policy(self, policy_key: str) -> DataPolicySpec:
        for p in self.policies:
            if p.id == policy_key:
                return p
        raise KeyError(policy_key)

    def readiness_profile(self, provider: str) -> tuple[str, ReadinessProfile]:
        name = self.readiness.provider_profiles.get(provider, self.readiness.default_profile)
        return name, self.readiness.profiles[name]

    def providers_for(self, category: MeasurementCategory) -> list[str]:
        explicit = self.provider_preferences.get(category)
        if explicit:
            return list(explicit)
        return [
            name
            for name, p in self.providers.items()
            if category in p.measurement_categories and p.enabled
        ]


class LoadedMonitoringConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path | None
    config: MonitoringConfig
    digest: str


def load_monitoring_config(directory: str | Path | None = None) -> LoadedMonitoringConfig:
    root = resolve_config_dir(directory)
    path = root / "monitoring.yaml"
    if not path.is_file():
        raise ConfigError(f"monitoring configuration not found: {path}")
    raw_text = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    providers = data.get("providers", {})
    if isinstance(providers, dict):
        for key, value in providers.items():
            if isinstance(value, dict):
                value.setdefault("name", key)
    try:
        config = MonitoringConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
    return LoadedMonitoringConfig(path=path, config=config, digest=digest)


def default_config() -> MonitoringConfig:
    """Configuration used by unit tests that do not read YAML."""
    return MonitoringConfig(
        providers={},
        policies=(),
        proxy_monitoring=ProxyMonitoring(enabled=False),
    )
