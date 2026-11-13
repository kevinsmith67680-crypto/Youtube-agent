"""Inference: eligible, typed forecasts for one series at one cutoff (no training here).

For every configured horizon the engine:

1. builds the point-in-time features exactly as in training (same code path);
2. refuses with a typed status when the series is ineligible for that horizon;
3. uses only CHAMPION models of the same target family, granularity, horizon and data
   origin (synthetic models never serve real series, and vice versa);
4. reports a probability only if the model is calibrated and the row is in distribution;
   otherwise the score is reported as an uncalibrated score, or withheld;
5. falls back to clearly labelled baselines (persistence with an empirical interval from
   the series' own history) when no validated model exists;
6. adds the experimental peak estimate, opportunity windows and a deterministic
   explanation, and assembles the versioned ``ForecastOutput`` contract.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.forecasting import eligibility, explainability
from trend_agent.forecasting import opportunity_window as ow
from trend_agent.forecasting.baselines import damped_trend, seasonal_naive
from trend_agent.forecasting.calibration import Calibrator
from trend_agent.forecasting.config import FORECAST_VERSION, LoadedForecastingConfig
from trend_agent.forecasting.dataset_builder import _support_views, row_features
from trend_agent.forecasting.feature_engineering import FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from trend_agent.forecasting.feature_store import PointInTimeSeries, SeriesBundle, hours
from trend_agent.forecasting.model_registry import ArtifactError, Registry
from trend_agent.forecasting.peak_estimation import STATE_FEATURES, AnalogModel
from trend_agent.forecasting.target_definition import bounded_index
from trend_agent.forecasting.training import LightGBMModel, LogisticModel
from trend_agent.forecasting.uncertainty import ConformalInterval, OODReference
from trend_agent.monitoring.replay import AvailabilityMode
from trend_agent.schemas.enums import TimeGranularity
from trend_agent.schemas.forecast import (
    BreakoutForecast,
    CalibrationStatus,
    ContinuationForecast,
    DataQuality,
    DataQualityStatus,
    ForecastOutput,
    ForecastStatus,
    ModelRef,
    TrajectoryPoint,
    Uncertainty,
)
from trend_agent.schemas.model_metadata import DataOrigin, ModelMetadata, ModelStatus

_NS = uuid.UUID("5a1e6f0c-3b7d-4c55-9a7e-6f0ca5f1c006")


class _BaselineModel:
    def __init__(self, name: str) -> None:
        self.name = name

    def score(self, x: np.ndarray) -> np.ndarray:
        return np.zeros(x.shape[0])

    def contributions(self, row: np.ndarray) -> dict[str, float]:
        return {}


class _Constant:
    def __init__(self, rate: float) -> None:
        self.rate = rate

    def score(self, x: np.ndarray) -> np.ndarray:
        return np.full(x.shape[0], self.rate)

    def contributions(self, row: np.ndarray) -> dict[str, float]:
        return {}


def build_model(payload: dict[str, Any], text: str | None) -> Any:
    """Rebuild a scorer from verified parameters (never from pickled objects)."""
    m = payload["model"]
    kind = m.get("kind")
    if kind == "logistic":
        return LogisticModel.from_dict(m)
    if kind == "lightgbm":
        if text is None:
            raise ArtifactError("LightGBM artifact text missing")
        return LightGBMModel(
            tuple(m["names"]), text, int(m["best_iteration"]), m["params"], m["objective"]
        )
    if kind == "baseline":
        if "rate" in m:
            return _Constant(float(m["rate"]))
        return _BaselineModel(str(m.get("baseline", "persistence")))
    raise ArtifactError(f"unknown model kind {kind!r}")


def _ref(meta: ModelMetadata | None) -> ModelRef | None:
    if meta is None:
        return None
    return ModelRef(
        model_id=meta.model_id,
        name=meta.model_name,
        version=meta.version,
        model_type=meta.model_type.value,
        status=meta.status.value.upper(),
        data_origin=meta.data_origin.value,
        validated=meta.status
        in (ModelStatus.VALIDATED, ModelStatus.CHAMPION, ModelStatus.CHALLENGER),
    )


def issue_time(requested: datetime) -> datetime:
    """Forecasts are issued on the hour (UTC), like the training grid.

    Every request within the same hour is evaluated at the same issue time and therefore
    returns an identical forecast; a forecast an hour later predicts a different window
    (T, T + H] and is a new forecast even when no new data arrived."""
    return requested.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def prediction_id(series_id: int, issued_at: datetime, horizon: int, models: Sequence[str]) -> str:
    key = f"{series_id}|{issued_at.isoformat()}|{horizon}|{'|'.join(sorted(models))}"
    return str(uuid.uuid5(_NS, key))


@dataclass
class _Loaded:
    meta: ModelMetadata
    payload: dict[str, Any]
    model: Any


class ForecastEngine:
    def __init__(
        self,
        loaded: LoadedForecastingConfig,
        dloaded: LoadedDetectionConfig,
        registry: Registry,
    ) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.dloaded = dloaded
        self.registry = registry

    def _champion(
        self,
        family: str,
        gran: str,
        horizon: int,
        target: str,
        origin: DataOrigin,
        category: str | None,
    ) -> _Loaded | None:
        meta = self.registry.champion(
            family=family,
            granularity=gran,
            horizon=horizon,
            target_type=target,
            data_origin=origin,
            category=category,
        )
        if meta is None:
            return None
        meta, payload, text = self.registry.load(meta.model_id)
        if tuple(meta.feature_names) != FEATURE_NAMES:
            return None  # a model built on another feature schema is never used silently
        model = None if payload.get("kind") == "analog" else build_model(payload, text)
        return _Loaded(meta, payload, model)

    def forecast(
        self,
        bundle: SeriesBundle,
        as_of: datetime,
        *,
        horizons: Sequence[int] | None = None,
    ) -> list[ForecastOutput]:
        cfg = self.cfg
        meta = bundle.meta
        as_of = issue_time(as_of)
        mode = AvailabilityMode(cfg.availability_mode)
        pit = PointInTimeSeries(bundle.points, mode)
        vis, ref, row = row_features(
            bundle,
            pit,
            None,
            _support_views(bundle, mode),
            as_of,
            self.loaded,
            self.dloaded,
            strict=True,
        )
        # Data cutoff = availability time of the newest observation used (target and
        # supporting series); recorded with every forecast for audit and replay.
        used = [a for a in pit.available.values() if a <= as_of]
        for sp in _support_views(bundle, mode).values():
            used += [a for a in sp.available.values() if a <= as_of]
        cutoff = max(used) if used else as_of
        x = row.vector()
        values = {k: (None if math.isnan(v) else round(float(v), 6)) for k, v in row.values.items()}
        family = eligibility.target_family(meta.measurement)
        origin = DataOrigin.SYNTHETIC if meta.is_synthetic else DataOrigin.REAL
        gran = meta.granularity.value
        source = eligibility.source_classification(meta)
        base_limits = []
        if meta.is_synthetic:
            base_limits.append("synthetic test data: says nothing about real-world accuracy")
        if family is not None and family.value == "proxy_attention":
            base_limits.append(
                "proxy attention forecast: video/community activity, not search demand"
            )
        base_limits.append("relative index of the measured source; not search counts")
        base_limits += row.rejected
        # peak + windows (shared by all horizons of this series)
        analog = None
        if family is not None:
            al = self._champion(
                family.value, gran, cfg.targets.peak_window_hours, "peak", origin, meta.category
            )
            if al is not None:
                analog = (al.meta, AnalogModel.from_dict(al.payload["model"]))
        state = {f: float(row.values.get(f, math.nan)) for f in STATE_FEATURES}
        outputs = []
        for h in horizons or cfg.horizons_hours:
            elig = eligibility.check(vis, meta, cfg, h)
            quality = self._quality(vis, meta, row.values, elig)
            common: dict[str, Any] = {
                "query": meta.query,
                "query_id": meta.query_id,
                "target_id": meta.target_id,
                "series_id": meta.series_id,
                "category": meta.category,
                "region": meta.region,
                "provider": meta.provider,
                "source_classification": source,
                "target_family": family.value if family else "none",
                "is_proxy": meta.is_proxy,
                "synthetic": meta.is_synthetic,
                "prediction_timestamp": as_of,
                "data_cutoff": cutoff,
                "forecast_horizon_hours": h,
                "data_quality": quality,
                "features": values,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "configuration_version": self.loaded.version_tag,
                "forecast_version": FORECAST_VERSION,
            }
            if family is None or elig.status is not ForecastStatus.READY or ref.level is None:
                reasons = list(elig.reasons) or ([ref.reason] if ref.reason else [])
                outputs.append(
                    ForecastOutput(
                        prediction_id=prediction_id(meta.series_id, as_of, h, ["none"]),
                        status=elig.status
                        if elig.status is not ForecastStatus.READY
                        else ForecastStatus.INSUFFICIENT_HISTORY,
                        status_reasons=tuple(reasons),
                        uncertainty=Uncertainty(ood_status="unknown", ood_share=None),
                        evidence={"visible_observations": vis.n},
                        **common,
                    )
                )
                continue
            outputs.append(
                self._one(
                    meta,
                    vis,
                    ref,
                    row,
                    x,
                    values,
                    family.value,
                    origin,
                    gran,
                    h,
                    analog,
                    state,
                    base_limits,
                    common,
                )
            )
        return outputs

    # ------------------------------------------------------------------ helpers
    def _quality(self, vis: Any, meta: Any, v: dict[str, float], elig: Any) -> DataQuality:
        reasons = list(elig.reasons)
        missing = v.get("missing_rate")
        stale = v.get("staleness_h")
        stable = v.get("normalization_stable") == 1.0
        if elig.status is not ForecastStatus.READY:
            status = DataQualityStatus.UNUSABLE
        elif (missing is not None and missing <= 0.05) and stable:
            status = DataQualityStatus.GOOD
        elif missing is not None and missing <= self.cfg.eligibility.max_missing_rate:
            status = DataQualityStatus.ACCEPTABLE
        else:
            status = DataQualityStatus.DEGRADED
        return DataQuality(
            status=status,
            measurement_unit="relative_search_interest"
            if meta.measurement.value.endswith("search_interest")
            else f"proxy_{meta.unit}",
            scale_type=meta.scale_type.value,
            granularity=meta.granularity.value,
            observations=int(vis.n),
            missing_rate=None if missing is None or math.isnan(missing) else round(missing, 4),
            staleness_hours=None if stale is None or math.isnan(stale) else round(stale, 2),
            normalization_stable=stable,
            reasons=tuple(reasons),
        )

    def _one(
        self,
        meta: Any,
        vis: Any,
        ref: Any,
        row: Any,
        x: np.ndarray,
        values: dict[str, float | None],
        family: str,
        origin: DataOrigin,
        gran: str,
        h: int,
        analog: tuple[ModelMetadata, AnalogModel] | None,
        state: dict[str, float],
        base_limits: list[str],
        common: dict[str, Any],
    ) -> ForecastOutput:
        cfg = self.cfg
        reasons: list[str] = []
        status = ForecastStatus.READY
        bo = self._champion(family, gran, h, "breakout", origin, meta.category)
        co = self._champion(family, gran, h, "continuation", origin, meta.category)
        tr = self._champion(family, gran, h, "trajectory", origin, meta.category)
        ood_src = next((m for m in (bo, tr, co) if m is not None), None)
        ood: dict[str, Any] = {"status": "unknown", "share": None, "features": [], "novel": []}
        if ood_src is not None and ood_src.payload.get("ood"):
            ood = OODReference.from_dict(ood_src.payload["ood"]).assess(x, meta.category, gran)
        if ood["status"] == "out_of_distribution":
            status = ForecastStatus.OUT_OF_DISTRIBUTION
            reasons.append(
                "features outside the training distribution: "
                + ", ".join(ood["features"][:6] + ood["novel"])
            )
        elif ood["status"] == "limited":
            status = ForecastStatus.LIMITED_CONFIDENCE
            reasons.append("some features near or beyond the training range")
        contributions: dict[str, float] = {}
        model_used: ModelMetadata | None = None
        breakout: BreakoutForecast
        continuation: ContinuationForecast | None = None
        elevated = bool(ref.elevated)
        if elevated:
            breakout = BreakoutForecast(
                calibration_status=CalibrationStatus.NOT_APPLICABLE,
                applicable=False,
                threshold_index=round(ref.threshold, 3),
                baseline_index=round(ref.baseline, 3),
            )
            continuation, c_used, c_contrib = self._classify(co, x, ood, "continuation")
            if c_used is not None:
                model_used, contributions = c_used, c_contrib
        else:
            pb, cal_status, score, interval, used, contrib = self._probability(bo, x, ood)
            model_used, contributions = used, contrib
            breakout = BreakoutForecast(
                probability=pb,
                score=score if pb is None else None,
                calibration_status=cal_status,
                applicable=True,
                probability_interval=interval,
                threshold_index=round(ref.threshold, 3),
                baseline_index=round(ref.baseline, 3),
                model=_ref(used),
                baseline_comparison=self._baseline_cmp(used),
            )
            if used is None:
                if status is ForecastStatus.READY:
                    status = ForecastStatus.BASELINE_ONLY
                reasons.append("no validated breakout model for this scope; no probability")
            elif pb is None and status is ForecastStatus.READY:
                status = ForecastStatus.LIMITED_CONFIDENCE
                reasons.append(f"probability withheld: {cal_status.value}")
        traj = self._trajectory(tr, x, vis, meta, ref, h)
        if (
            (
                tr is None
                or tr.meta.model_type.value
                in (
                    "persistence",
                    "seasonal_naive",
                    "damped_trend",
                )
            )
            and status is ForecastStatus.READY
            and (bo is None and not elevated)
        ):
            status = ForecastStatus.BASELINE_ONLY
        peak = None
        windows: tuple[Any, ...] = ()
        if analog is not None:
            peak = analog[1].peak(state)
            relevant = (
                elevated
                or (breakout.probability is not None and breakout.probability >= 0.3)
                or (values.get("level_vs_baseline_log") or 0) > 0.2
            )
            windows = ow.windows(
                analog[1],
                state,
                prediction_time=vis.as_of,
                cfg=cfg.windows,
                peak=peak,
                relevant=relevant,
                max_horizon=max(cfg.horizons_hours),
                elevated_now=elevated,
            )
            if status is ForecastStatus.OUT_OF_DISTRIBUTION:
                peak = peak.model_copy(
                    update={"note": peak.note + "; features out of distribution"}
                )
        hist = (model_used.metrics if model_used else {}) or {}
        uncertainty = Uncertainty(
            ood_status=str(ood["status"]),
            ood_share=ood.get("share"),
            ood_features=tuple(ood.get("features", [])[:10]),
            calibration_support=(model_used.calibration or {}).get("support", {})
            if model_used
            else {},
            historical_reliability={
                k: hist.get(k) for k in ("brier", "ece", "pr_auc", "mae") if k in hist
            },
            notes=(
                "uncertainty is tracked separately from severity: a strong surge can still "
                "have an uncertain future",
            ),
        )
        expl = explainability.explain(
            query=meta.query,
            family=family,
            horizon=h,
            granularity=gran,
            breakout=breakout,
            trajectory=traj,
            windows=windows,
            model=model_used or (tr.meta if tr else None),
            contributions=contributions,
            values=values,
            uncertainty={"ood_status": ood["status"]},
            limitations=base_limits,
            persistence=cfg.targets.breakout_persistence.get(meta.granularity, 1),
        )
        used_ids = [m.meta.model_id for m in (bo, co, tr) if m is not None]
        if analog is not None:
            used_ids.append(analog[0].model_id)
        return ForecastOutput(
            prediction_id=prediction_id(
                meta.series_id, common["prediction_timestamp"], h, used_ids or ["baseline"]
            ),
            status=status,
            status_reasons=tuple(reasons),
            breakout=breakout,
            continuation=continuation,
            trajectory=traj,
            peak_estimate=peak,
            opportunity_windows=windows,
            uncertainty=uncertainty,
            explanation=expl,
            evidence={
                "models": used_ids,
                "visible_observations": int(vis.n),
                "last_observation_end": vis.t_end[-1].item() if vis.n else None,
                "reference": {
                    "baseline": ref.baseline,
                    "threshold": ref.threshold,
                    "level": ref.level,
                    "elevated": ref.elevated,
                },
                "provenance": row.provenance,
            },
            **common,
        )

    def _probability(
        self, lm: _Loaded | None, x: np.ndarray, ood: dict[str, Any]
    ) -> tuple[Any, ...]:
        if lm is None:
            return None, CalibrationStatus.UNCALIBRATED, None, None, None, {}
        raw = float(lm.model.score(x.reshape(1, -1))[0])
        contrib = lm.model.contributions(x)
        cal = (
            Calibrator.from_dict(lm.payload["calibrator"]) if lm.payload.get("calibrator") else None
        )
        if ood["status"] == "out_of_distribution":
            return (
                None,
                CalibrationStatus.OUT_OF_DISTRIBUTION,
                round(raw, 4),
                None,
                lm.meta,
                contrib,
            )
        if cal is None or cal.status is not CalibrationStatus.CALIBRATED:
            status = cal.status if cal else CalibrationStatus.UNCALIBRATED
            return None, status, round(raw, 4), None, lm.meta, contrib
        p_arr = cal.apply(np.array([raw]))
        assert p_arr is not None
        p = float(p_arr[0])
        return round(p, 4), CalibrationStatus.CALIBRATED, None, cal.interval(p), lm.meta, contrib

    def _classify(
        self, lm: _Loaded | None, x: np.ndarray, ood: dict[str, Any], kind: str
    ) -> tuple[ContinuationForecast, ModelMetadata | None, dict[str, float]]:
        p, status, _score, _interval, used, contrib = self._probability(lm, x, ood)
        return (
            ContinuationForecast(
                probability=p, calibration_status=status, applicable=True, model=_ref(used)
            ),
            used,
            contrib,
        )

    def _baseline_cmp(self, meta: ModelMetadata | None) -> dict[str, float | None]:
        if meta is None:
            return {}
        m = meta.metrics or {}
        return {
            k: m.get(k)
            for k in ("brier", "brier_climatology", "brier_skill_vs_reference", "pr_auc")
            if k in m
        }

    def _trajectory(
        self, tm: _Loaded | None, x: np.ndarray, vis: Any, meta: Any, ref: Any, h: int
    ) -> tuple[TrajectoryPoint, ...]:
        from datetime import timedelta

        level = float(ref.level)
        name = tm.meta.model_type.value if tm is not None else "persistence"
        if name == "lightgbm_regressor" and tm is not None:
            change = float(tm.model.score(x.reshape(1, -1))[0])
        elif name == "seasonal_naive":
            change = seasonal_naive(vis, meta.granularity, float(h))
            change = 0.0 if math.isnan(change) else change
        elif name == "damped_trend":
            slope = float(x[FEATURE_NAMES.index("slope_recent_log_per_h")])
            change = damped_trend(slope, float(h), meta.granularity)
        else:
            change = 0.0
        interval = (
            ConformalInterval.from_dict(tm.payload["interval"])
            if tm is not None and tm.payload.get("interval")
            else None
        )
        method = interval.method if interval and interval.half_width is not None else "none"
        lo_c = hi_c = None
        if interval is not None and interval.half_width is not None:
            lo_c, hi_c = change - interval.half_width, change + interval.half_width
        else:
            emp = _own_history_interval(
                vis, h, self.cfg.uncertainty.interval_level, meta.granularity
            )
            if emp is not None:
                lo_c, hi_c = change + emp[0], change + emp[1]
                method = "empirical_quantiles_of_own_history"

        def to_level(c: float | None) -> float | None:
            if c is None:
                return None
            v = (level + 1.0) * math.exp(c) - 1.0
            if bounded_index(meta):
                v = min(100.0, max(0.0, v))
            return round(max(0.0, v), 2)

        return (
            TrajectoryPoint(
                lead_hours=h,
                forecast_time=vis.as_of + timedelta(hours=h),
                value=to_level(change),
                lower=to_level(lo_c),
                upper=to_level(hi_c),
                interval_level=self.cfg.uncertainty.interval_level if lo_c is not None else None,
                interval_method=method,
                model=tm.meta.model_name if tm is not None else "persistence (unregistered)",
            ),
        )


def _own_history_interval(
    vis: Any, h: int, level: float, g: TimeGranularity
) -> tuple[float, float] | None:
    """Quantiles of the series' own past h-hour log changes (persistence residuals)."""
    ok = np.isfinite(vis.values)
    t, v = vis.t_end[ok], vis.values[ok]
    if t.size < 20:
        return None
    pos = {round(float(a), 3): float(b) for a, b in zip(t, v, strict=True)}
    changes = [
        math.log((pos[round(float(a) + h, 3)] + 1.0) / (float(b) + 1.0))
        for a, b in zip(t, v, strict=True)
        if round(float(a) + h, 3) in pos
    ]
    if len(changes) < 20:
        return None
    lo, hi = np.quantile(changes, [(1 - level) / 2, 1 - (1 - level) / 2])
    del g
    return float(lo), float(hi)


__all__ = ["ForecastEngine", "build_model", "hours", "prediction_id"]
