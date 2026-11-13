"""Phase 6 configuration (``config/forecasting.yaml``), validated strictly.

Every threshold here is a documented operating point. Category shares (AI 45 %,
technology 25 %, gaming 30 %) are resource-allocation priorities, not demand estimates.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trend_agent.discovery.config import ConfigError, resolve_config_dir
from trend_agent.schemas.enums import TimeGranularity

#: Bump when target definitions, feature computation or model logic change meaning.
FORECAST_VERSION = "forecast-1.0.0"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Targets(_Strict):
    """Independent label definitions (never the detector's own alert status)."""

    baseline_window_hours: dict[TimeGranularity, int] = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 168, TimeGranularity.DAY: 672},
        description="trailing baseline window per granularity",
    )
    min_baseline_observations: dict[TimeGranularity, int] = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 48, TimeGranularity.DAY: 10}
    )
    breakout_relative: float = Field(default=0.5, gt=0, description="rise above baseline")
    breakout_absolute_index: float = Field(
        default=10.0, ge=0, description="minimum rise in index points (0-100 indices)"
    )
    breakout_noise_k: float = Field(
        default=2.0, ge=0, description="minimum rise in robust SDs of the baseline window"
    )
    breakout_persistence: dict[TimeGranularity, int] = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 3, TimeGranularity.DAY: 1},
        description="consecutive observations at or above the threshold",
    )
    continuation_tail_fraction: float = Field(default=0.25, gt=0, le=1)
    min_label_coverage: float = Field(
        default=0.75, gt=0, le=1, description="share of future periods observed for a label"
    )
    peak_window_hours: int = Field(default=72, ge=6)
    peak_boundary_tolerance: float = Field(
        default=0.97, gt=0, le=1, description="last value within this share of the max = censored"
    )
    trajectory_offset: float = Field(default=1.0, gt=0, description="log((y+c)/(y0+c))")


class Eligibility(_Strict):
    min_history_hours: dict[TimeGranularity, int] = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 168, TimeGranularity.DAY: 28 * 24}
    )
    max_missing_rate: float = Field(default=0.3, ge=0, le=1)
    max_staleness_hours: dict[TimeGranularity, int] = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 24, TimeGranularity.DAY: 96}
    )
    forecastable_measurements: tuple[str, ...] = (
        "direct_youtube_search_interest",
        "external_search_interest",
        "video_activity",
        "community_activity",
    )


class Splits(_Strict):
    train_fraction: float = Field(default=0.6, gt=0, lt=1)
    calibration_fraction: float = Field(default=0.2, gt=0, lt=1)
    unseen_group_fraction: float = Field(
        default=0.2, ge=0, lt=1, description="topic groups held out of training entirely"
    )
    rolling_folds: int = Field(default=3, ge=1, le=10)
    embargo_extra_hours: int = Field(
        default=48, ge=0, description="added to the horizon: covers publication lags"
    )
    seed: int = 20261113

    @model_validator(mode="after")
    def _fractions(self) -> Splits:
        if self.train_fraction + self.calibration_fraction >= 1:
            raise ValueError("train + calibration fractions must leave a test period")
        return self


class LightGBMSettings(_Strict):
    """Conservative defaults; the grid is deliberately small (no excessive tuning)."""

    base: dict[str, float | int | str] = Field(
        default_factory=lambda: {  # type: ignore[arg-type,unused-ignore]
            "learning_rate": 0.05,
            "num_leaves": 15,
            "max_depth": 4,
            "min_data_in_leaf": 40,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "lambda_l2": 2.0,
            "min_gain_to_split": 0.0,
            "verbosity": -1,
            "num_threads": 2,
            "deterministic": "true",
            "force_col_wise": "true",
        }
    )
    grid: tuple[dict[str, float | int], ...] = (
        {"num_leaves": 7, "min_data_in_leaf": 60},
        {"num_leaves": 15, "min_data_in_leaf": 40},
        {"num_leaves": 31, "min_data_in_leaf": 20},
    )
    max_rounds: int = Field(default=400, ge=10)
    early_stopping_rounds: int = Field(default=30, ge=5)
    min_training_examples: int = Field(default=300, ge=10)
    min_training_positives: int = Field(default=20, ge=1)


class Calibration(_Strict):
    min_positives: int = Field(default=25, ge=1)
    min_negatives: int = Field(default=25, ge=1)
    isotonic_min_examples: int = Field(default=600, ge=10)
    isotonic_min_positives: int = Field(default=60, ge=1)
    bins: int = Field(default=10, ge=3, le=50)


class Uncertainty(_Strict):
    interval_level: float = Field(default=0.8, gt=0.5, lt=0.999)
    ood_quantiles: tuple[float, float] = (0.005, 0.995)
    ood_margin_iqr: float = Field(default=0.5, ge=0, description="margin beyond the range")
    ood_limited_share: float = Field(default=0.05, ge=0, le=1)
    ood_reject_share: float = Field(default=0.2, ge=0, le=1)


class Promotion(_Strict):
    """Explicit acceptance criteria. A model failing any of them is never promoted."""

    min_test_examples: int = Field(default=200, ge=1)
    min_test_positives: int = Field(default=20, ge=1)
    min_brier_skill: float = Field(
        default=0.02, description="1 - Brier/Brier(climatology) on the untouched test period"
    )
    max_ece: float = Field(default=0.08, gt=0, le=1)
    bootstrap_samples: int = Field(default=400, ge=50)
    ci_level: float = Field(default=0.9, gt=0.5, lt=1)
    precision_at_k: int = Field(default=50, ge=1)
    max_precision_drop: float = Field(default=0.05, ge=0, le=1)
    max_overfit_gap: float = Field(
        default=0.25, ge=0, description="train PR-AUC minus test PR-AUC above this = overfit"
    )


class ProductionProfile(_Strict):
    hours: float = Field(gt=0)
    description: str = ""


class Windows(_Strict):
    profiles: dict[str, ProductionProfile] = Field(
        default_factory=lambda: {
            "QUICK_REACTION": ProductionProfile(hours=2, description="short reaction video"),
            "STANDARD_TUTORIAL": ProductionProfile(hours=8, description="tutorial"),
            "DEEP_DIVE": ProductionProfile(hours=24, description="long-form analysis"),
        }
    )
    elevated_probability: float = Field(default=0.6, gt=0, lt=1)
    too_late_probability: float = Field(default=0.25, gt=0, lt=1)
    urgent_peak_hours: float = Field(default=24.0, gt=0)
    long_window_hours: float = Field(default=72.0, gt=0)
    min_analogs: int = Field(default=25, ge=5)
    analog_k: int = Field(default=60, ge=5)
    max_analogs_stored: int = Field(default=6000, ge=100)


class Drift(_Strict):
    psi_warn: float = 0.1
    psi_alert: float = 0.25
    min_recent_predictions: int = Field(default=50, ge=1)
    min_matured_labels: int = Field(default=100, ge=1)
    brier_degradation: float = Field(default=0.03, ge=0)
    base_rate_change: float = Field(default=0.5, ge=0, description="relative change")
    retrain_min_alert_features: int = Field(default=3, ge=1)


class Corpus(_Strict):
    """Synthetic training corpus (isolated experimental data, never production)."""

    hourly_series: int = Field(default=240, ge=10)
    daily_series: int = Field(default=120, ge=0)
    hourly_days: int = Field(default=56, ge=14)
    daily_days: int = Field(default=240, ge=60)
    seed: int = 6006


class ForecastingConfig(_Strict):
    version: int = 1
    config_version: str = "forecasting-v1"
    horizons_hours: tuple[int, ...] = (6, 24, 72)
    cadence_hours: dict[TimeGranularity, int] = Field(
        default_factory=lambda: {TimeGranularity.HOUR: 6, TimeGranularity.DAY: 24},
        description="prediction-time grid for training examples",
    )
    availability_mode: str = "publication"
    targets: Targets = Targets()
    eligibility: Eligibility = Eligibility()
    splits: Splits = Splits()
    lightgbm: LightGBMSettings = LightGBMSettings()
    calibration: Calibration = Calibration()
    uncertainty: Uncertainty = Uncertainty()
    promotion: Promotion = Promotion()
    windows: Windows = Windows()
    drift: Drift = Drift()
    corpus: Corpus = Corpus()
    category_allocation: dict[str, float] = Field(
        default_factory=lambda: {"ai": 0.45, "technology": 0.25, "gaming": 0.30}
    )
    artifact_dir: str = "var/forecast_models"

    @model_validator(mode="after")
    def _checks(self) -> ForecastingConfig:
        if not self.horizons_hours or any(h <= 0 for h in self.horizons_hours):
            raise ValueError("horizons must be positive hours")
        if abs(sum(self.category_allocation.values()) - 1) > 1e-6:
            raise ValueError("category allocation must sum to 1")
        if self.availability_mode not in ("publication", "agent"):
            raise ValueError("availability_mode must be publication or agent")
        if self.targets.peak_window_hours < max(self.horizons_hours):
            raise ValueError("peak window must cover the longest horizon")
        return self


class LoadedForecastingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path | None
    config: ForecastingConfig
    digest: str

    @property
    def version_tag(self) -> str:
        return f"{self.config.config_version}@{self.digest}"


def granularity_hours(g: TimeGranularity) -> float | None:
    return {
        TimeGranularity.HOUR: 1.0,
        TimeGranularity.DAY: 24.0,
        TimeGranularity.WEEK: 168.0,
    }.get(g)


def horizon_supported(horizon_hours: int, g: TimeGranularity) -> bool:
    """A horizon is supported only if it is a whole number of observation periods.

    Daily series therefore cannot support a 6-hour horizon: nothing is interpolated."""
    step = granularity_hours(g)
    if step is None:
        return False
    return horizon_hours >= step and abs(horizon_hours / step - round(horizon_hours / step)) < 1e-9


def load_forecasting_config(directory: str | Path | None = None) -> LoadedForecastingConfig:
    root = resolve_config_dir(directory)
    path = root / "forecasting.yaml"
    if not path.is_file():
        raise ConfigError(f"forecasting configuration not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data: Any = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    try:
        config = ForecastingConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ConfigError(f"{path}: {where}: {first.get('msg')}") from None
    return LoadedForecastingConfig(
        path=path, config=config, digest=hashlib.sha256(raw.encode()).hexdigest()[:12]
    )


def default_forecasting_config() -> LoadedForecastingConfig:
    return LoadedForecastingConfig(path=None, config=ForecastingConfig(), digest="defaults")


__all__ = [
    "FORECAST_VERSION",
    "ForecastingConfig",
    "LoadedForecastingConfig",
    "default_forecasting_config",
    "granularity_hours",
    "horizon_supported",
    "load_forecasting_config",
    "timedelta",
]
