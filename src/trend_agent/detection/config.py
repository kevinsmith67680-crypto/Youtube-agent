"""Detection configuration (``config/detection.yaml``), validated strictly.

All statistical parameters live here and nowhere else; modules receive the relevant
section. ``LoadedDetectionConfig.digest`` (SHA-256 of the file) and ``config_version`` are
stored with every detector result, episode and run.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.discovery.models import EvidenceType, NoveltyKind
from trend_agent.schemas.detection import (
    BaselineReadiness,
    DetectorType,
    MultipleTestingMethod,
)
from trend_agent.schemas.enums import ScaleType, TimeGranularity
from trend_agent.schemas.evidence import AlertStage
from trend_agent.schemas.monitoring import MeasurementCategory


class ScaleFamily(StrEnum):
    INDEX = "index"  # relative interest index (0-100 or provider-consistent)
    COUNT = "count"  # counted items per period


INDEX_SCALES = frozenset({ScaleType.RELATIVE_PER_REQUEST, ScaleType.CONSISTENT_INDEX})
COUNT_SCALES = frozenset({ScaleType.ABSOLUTE_COUNT, ScaleType.ESTIMATED_COUNT})


def scale_family(scale: ScaleType) -> ScaleFamily | None:
    if scale in INDEX_SCALES:
        return ScaleFamily.INDEX
    if scale in COUNT_SCALES:
        return ScaleFamily.COUNT
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ByGranularity = dict[TimeGranularity, int]
ByFamily = dict[ScaleFamily, float]


class Preprocessing(_Strict):
    supported_granularities: tuple[TimeGranularity, ...] = (
        TimeGranularity.HOUR,
        TimeGranularity.DAY,
        TimeGranularity.WEEK,
    )
    max_history_periods: ByGranularity = Field(
        default_factory=lambda: {
            TimeGranularity.HOUR: 2160,
            TimeGranularity.DAY: 730,
            TimeGranularity.WEEK: 260,
        }
    )
    regularity_window: ByGranularity = Field(
        default_factory=lambda: {
            TimeGranularity.HOUR: 336,
            TimeGranularity.DAY: 56,
            TimeGranularity.WEEK: 26,
        }
    )
    min_recent_coverage: float = Field(default=0.5, gt=0, le=1)
    unsupported_scales: tuple[ScaleType, ...] = (ScaleType.PRESENCE, ScaleType.RANK)


class RobustBaselineConfig(_Strict):
    window_periods: ByGranularity = Field(
        default_factory=lambda: {
            TimeGranularity.HOUR: 168,
            TimeGranularity.DAY: 28,
            TimeGranularity.WEEK: 26,
        }
    )
    min_samples: int = Field(default=14, ge=3)
    provisional_min_samples: int = Field(default=7, ge=2)
    epsilon: float = Field(default=1e-9, gt=0)
    dispersion_floor_abs: ByFamily = Field(
        default_factory=lambda: {ScaleFamily.INDEX: 1.0, ScaleFamily.COUNT: 1.0}
    )
    dispersion_floor_rel: float = Field(default=0.05, ge=0, le=1)

    @model_validator(mode="after")
    def _order(self) -> RobustBaselineConfig:
        if self.provisional_min_samples > self.min_samples:
            raise ValueError("provisional_min_samples must not exceed min_samples")
        return self


class SeasonalBaselineConfig(_Strict):
    enabled: bool = True
    cycle_periods: ByGranularity = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 24, TimeGranularity.DAY: 7}
    )
    window_cycles: int = Field(default=12, ge=2)
    min_cycles: int = Field(default=6, ge=2)
    min_per_phase: int = Field(default=5, ge=2)
    level_cycles: int = Field(default=2, ge=1)
    significance_alpha: float = Field(default=0.01, gt=0, lt=1)
    min_dispersion_reduction: float = Field(default=0.10, ge=0, lt=1)

    @model_validator(mode="after")
    def _order(self) -> SeasonalBaselineConfig:
        if self.min_cycles > self.window_cycles:
            raise ValueError("seasonal.min_cycles must not exceed window_cycles")
        return self


class BaselineConfig(_Strict):
    robust: RobustBaselineConfig = RobustBaselineConfig()
    seasonal: SeasonalBaselineConfig = SeasonalBaselineConfig()


class ThresholdSet(_Strict):
    watch_z: float | None = Field(default=None, gt=0)
    alert_z: float | None = Field(default=None, gt=0)
    min_absolute_effect: ByFamily | None = None
    min_observed_level: ByFamily | None = None


class ThresholdMatch(_Strict):
    provider: str | None = None
    granularity: TimeGranularity | None = None
    category: str | None = None
    readiness: BaselineReadiness | None = None
    scale_family: ScaleFamily | None = None
    measurement: MeasurementCategory | None = None


class ThresholdOverride(_Strict):
    name: str = Field(min_length=1)
    match: ThresholdMatch
    set: ThresholdSet


class AnomalyConfig(_Strict):
    defaults: ThresholdSet = ThresholdSet(
        watch_z=3.0,
        alert_z=4.0,
        min_absolute_effect={ScaleFamily.INDEX: 8.0, ScaleFamily.COUNT: 3.0},
        min_observed_level={ScaleFamily.INDEX: 10.0, ScaleFamily.COUNT: 3.0},
    )
    overrides: tuple[ThresholdOverride, ...] = ()
    min_expected_for_relative: ByFamily = Field(
        default_factory=lambda: {ScaleFamily.INDEX: 5.0, ScaleFamily.COUNT: 2.0}
    )
    late_alert_window_periods: int = Field(default=3, ge=0)

    @model_validator(mode="after")
    def _complete_defaults(self) -> AnomalyConfig:
        d = self.defaults
        if None in (d.watch_z, d.alert_z, d.min_absolute_effect, d.min_observed_level):
            raise ValueError("anomaly.defaults must set every threshold")
        assert d.watch_z is not None
        assert d.alert_z is not None
        if d.watch_z > d.alert_z:
            raise ValueError("anomaly.defaults.watch_z must not exceed alert_z")
        return self


class ResolvedThresholds(_Strict):
    watch_z: float
    alert_z: float
    min_absolute_effect: float
    min_observed_level: float
    applied: tuple[str, ...]


class StratumContext(_Strict):
    provider: str
    granularity: TimeGranularity
    category: str | None
    readiness: BaselineReadiness
    scale_family: ScaleFamily
    measurement: MeasurementCategory


class CusumConfig(_Strict):
    k: float = Field(default=0.5, gt=0)
    h: float = Field(default=5.0, gt=0)
    z_clip: float = Field(default=3.0, gt=0)
    max_gap_periods: int = Field(default=3, ge=0)
    refractory_periods: int = Field(default=7, ge=0)
    watch_fraction: float = Field(default=0.8, gt=0, lt=1)
    target_arl0: float = Field(default=365.0, gt=1)

    @model_validator(mode="after")
    def _single_point_cannot_alarm(self) -> CusumConfig:
        if self.z_clip - self.k >= self.h:
            raise ValueError("cusum: z_clip - k must be < h (a single value must not alarm)")
        return self


class BocpdPrior(_Strict):
    kappa0: float = Field(default=1.0, gt=0)
    alpha0: float = Field(default=1.0, gt=0)


class ChangePointConfig(_Strict):
    method: str = Field(default="bocpd", pattern="^bocpd$")
    hazard_lambda: float = Field(default=100.0, gt=1)
    max_run_length: int = Field(default=200, ge=20, le=2000)
    prior: BocpdPrior = BocpdPrior()
    recent_window: int = Field(default=10, ge=2)
    min_post_points: int = Field(default=3, ge=1)
    probability_threshold: float = Field(default=0.6, gt=0, lt=1)
    watch_probability: float = Field(default=0.35, gt=0, lt=1)
    min_shift_sigma: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _order(self) -> ChangePointConfig:
        if self.min_post_points > self.recent_window:
            raise ValueError("change_point.min_post_points must be <= recent_window")
        if self.watch_probability > self.probability_threshold:
            raise ValueError("change_point.watch_probability must be <= probability_threshold")
        return self


class ColdStartConfig(_Strict):
    min_growth_points: int = Field(default=3, ge=2)
    growth_ratio: float = Field(default=1.5, gt=1)
    strong_growth_points: int = Field(default=4, ge=3)
    kendall_tau_min: float = Field(default=0.6, gt=0, le=1)
    corroboration_window: timedelta = timedelta(days=14)
    min_independent_providers: int = Field(default=2, ge=1)
    corroborating_evidence: tuple[EvidenceType, ...] = (
        EvidenceType.OFFICIAL_ANNOUNCEMENT,
        EvidenceType.CODE_RELEASE,
        EvidenceType.REPOSITORY_METADATA,
        EvidenceType.GAME_CATALOG,
        EvidenceType.SEARCH_SUGGESTION,
        EvidenceType.RELATED_QUERY,
        EvidenceType.VIDEO_TITLE,
        EvidenceType.COMMUNITY_DISCUSSION,
    )
    new_topic_novelty: tuple[NoveltyKind, ...] = (
        NoveltyKind.NEW_ENTITY,
        NoveltyKind.NEW_VERSION,
        NoveltyKind.NEW_EVENT,
    )


class SeverityWeights(_Strict):
    statistical: float = Field(default=0.5, ge=0)
    magnitude: float = Field(default=0.3, ge=0)
    relative: float = Field(default=0.2, ge=0)

    @model_validator(mode="after")
    def _sum(self) -> SeverityWeights:
        total = self.statistical + self.magnitude + self.relative
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"severity weights must sum to 1 (got {total:.4f})")
        return self


class StatisticalCombination(_Strict):
    max_weight: float = Field(default=0.75, ge=0, le=1)
    mean_weight: float = Field(default=0.25, ge=0, le=1)

    @model_validator(mode="after")
    def _sum(self) -> StatisticalCombination:
        if abs(self.max_weight + self.mean_weight - 1.0) > 1e-6:
            raise ValueError("statistical_combination weights must sum to 1")
        return self


class SeverityReferences(_Strict):
    z_cap: float = Field(default=8.0, gt=0)
    magnitude_ref: ByFamily = Field(
        default_factory=lambda: {ScaleFamily.INDEX: 30.0, ScaleFamily.COUNT: 20.0}
    )
    relative_cap: float = Field(default=4.0, gt=1)
    persistence_ref: int = Field(default=5, ge=1)


class QualityMultipliers(_Strict):
    sample: dict[str, float] = Field(default_factory=lambda: {"complete": 1.0, "low_volume": 0.7})
    baseline: dict[BaselineReadiness, float] = Field(
        default_factory=lambda: {
            BaselineReadiness.READY: 1.0,
            BaselineReadiness.PROVISIONAL: 0.8,
            BaselineReadiness.INSUFFICIENT: 0.6,
        }
    )
    freshness: dict[str, float] = Field(default_factory=dict)
    unstable_dispersion: float = Field(default=0.7, gt=0, le=1)


class SeverityConfig(_Strict):
    version: str = "severity-v1"
    weights: SeverityWeights = SeverityWeights()
    statistical_combination: StatisticalCombination = StatisticalCombination()
    references: SeverityReferences = SeverityReferences()
    quality_multipliers: QualityMultipliers = QualityMultipliers()


class MultipleTestingConfig(_Strict):
    method: MultipleTestingMethod = MultipleTestingMethod.BH
    fdr_q: float = Field(default=0.05, gt=0, lt=1)
    target_false_alerts_per_day: float = Field(default=2.0, gt=0)
    max_new_alerts_per_day: int = Field(default=25, ge=1)

    @model_validator(mode="after")
    def _method(self) -> MultipleTestingConfig:
        if self.method is MultipleTestingMethod.ALERT_BUDGET:
            raise ValueError(
                "multiple_testing.method selects the FDR procedure (benjamini_hochberg or "
                "benjamini_yekutieli); the alert budget always applies"
            )
        return self


class AlertPolicyConfig(_Strict):
    confirm_min_periods: int = Field(default=2, ge=1)
    confirm_requires_calibration: bool = True
    candidate_requires_effect: bool = True
    cold_start_max_stage: AlertStage = AlertStage.CANDIDATE
    reject_conflicting_values: bool = True

    @model_validator(mode="after")
    def _stage(self) -> AlertPolicyConfig:
        if self.cold_start_max_stage not in (AlertStage.WATCH, AlertStage.CANDIDATE):
            raise ValueError("cold_start_max_stage must be watch or candidate")
        return self


class EpisodeConfig(_Strict):
    cooldown_periods: int = Field(default=2, ge=0)
    reopen_window: timedelta = timedelta(days=7)
    resolve_z: float = Field(default=1.0, ge=0)
    resolve_periods: int = Field(default=2, ge=1)
    max_quiet_periods: int = Field(default=14, ge=1)
    stale_after_periods: int = Field(default=7, ge=1)
    downgrade_hold_periods: int = Field(default=2, ge=1)
    sustain_z: float = Field(default=3.0, gt=0)


class IntegrationConfig(_Strict):
    run_after_collection: bool = True
    notify_channel: str = Field(default="trend_agent_detection", pattern=r"^[a-z_][a-z0-9_]*$")
    worker_fallback_seconds: int = Field(default=60, ge=1)
    include_synthetic: bool = False


class CalibrationConfig(_Strict):
    null_tail_quantile: float = Field(default=0.95, gt=0.5, lt=1)
    min_null_scores: int = Field(default=2000, ge=100)


class DetectionConfig(_Strict):
    version: int = 1
    config_version: str = "detection-v1"
    algorithm_version: str = "detect-1.0.0"
    threshold_version: str = "thresholds-default"
    enabled_detectors: tuple[DetectorType, ...] = (
        DetectorType.ROBUST_ANOMALY,
        DetectorType.CUSUM,
        DetectorType.CHANGE_POINT,
        DetectorType.COLD_START,
    )
    preprocessing: Preprocessing = Preprocessing()
    baseline: BaselineConfig = BaselineConfig()
    anomaly: AnomalyConfig = AnomalyConfig()
    cusum: CusumConfig = CusumConfig()
    change_point: ChangePointConfig = ChangePointConfig()
    cold_start: ColdStartConfig = ColdStartConfig()
    severity: SeverityConfig = SeverityConfig()
    multiple_testing: MultipleTestingConfig = MultipleTestingConfig()
    alert_policy: AlertPolicyConfig = AlertPolicyConfig()
    episodes: EpisodeConfig = EpisodeConfig()
    integration: IntegrationConfig = IntegrationConfig()
    calibration: CalibrationConfig = CalibrationConfig()

    @model_validator(mode="after")
    def _detectors(self) -> DetectionConfig:
        if DetectorType.ENGINE in self.enabled_detectors:
            raise ValueError("'engine' is internal bookkeeping, not a detector")
        if DetectorType.ROBUST_ANOMALY not in self.enabled_detectors:
            raise ValueError("robust_anomaly cannot be disabled (it defines the baseline)")
        return self

    def enabled(self, detector: DetectorType) -> bool:
        return detector in self.enabled_detectors

    def thresholds(self, ctx: StratumContext) -> ResolvedThresholds:
        """Anomaly thresholds for one stratum (defaults, then matching overrides in order)."""
        d = self.anomaly.defaults
        watch, alert = d.watch_z, d.alert_z
        effect = (d.min_absolute_effect or {}).get(ctx.scale_family, 0.0)
        level = (d.min_observed_level or {}).get(ctx.scale_family, 0.0)
        applied: list[str] = []
        for o in self.anomaly.overrides:
            m = o.match
            if (
                (m.provider is not None and m.provider != ctx.provider)
                or (m.granularity is not None and m.granularity is not ctx.granularity)
                or (m.category is not None and m.category != ctx.category)
                or (m.readiness is not None and m.readiness is not ctx.readiness)
                or (m.scale_family is not None and m.scale_family is not ctx.scale_family)
                or (m.measurement is not None and m.measurement is not ctx.measurement)
            ):
                continue
            applied.append(o.name)
            watch = o.set.watch_z if o.set.watch_z is not None else watch
            alert = o.set.alert_z if o.set.alert_z is not None else alert
            if o.set.min_absolute_effect and ctx.scale_family in o.set.min_absolute_effect:
                effect = o.set.min_absolute_effect[ctx.scale_family]
            if o.set.min_observed_level and ctx.scale_family in o.set.min_observed_level:
                level = o.set.min_observed_level[ctx.scale_family]
        assert watch is not None
        assert alert is not None
        return ResolvedThresholds(
            watch_z=min(watch, alert),
            alert_z=alert,
            min_absolute_effect=effect,
            min_observed_level=level,
            applied=tuple(applied),
        )


class LoadedDetectionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path | None
    config: DetectionConfig
    digest: str

    @property
    def version_tag(self) -> str:
        """``config_version@digest`` - stored with every output."""
        return f"{self.config.config_version}@{self.digest}"


def load_detection_config(directory: str | Path | None = None) -> LoadedDetectionConfig:
    root = resolve_config_dir(directory)
    path = root / "detection.yaml"
    if not path.is_file():
        raise ConfigError(f"detection configuration not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    try:
        config = DetectionConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return LoadedDetectionConfig(path=path, config=config, digest=digest)


def default_detection_config() -> LoadedDetectionConfig:
    """In-code defaults (unit tests that do not read YAML)."""
    return LoadedDetectionConfig(path=None, config=DetectionConfig(), digest="defaults")
