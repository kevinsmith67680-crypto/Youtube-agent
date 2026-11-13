"""Per-series detection engine: baselines + detectors + persistent state, causally.

``SeriesEngine.run`` processes the periods of one analysis-ready series in time order:
baseline (from earlier values only) -> rapid anomaly -> CUSUM -> change point -> cold
start, and returns one ``PointEvaluation`` per newly available observation.

**Incremental = recomputed.** Every quantity at period t is a deterministic function of
the values before (and at) t, so processing only the new periods with the saved state
gives exactly the same results as recomputing the whole loaded history. The engine uses
that to handle out-of-order delivery explicitly:

* new observations after the last processed period -> incremental update;
* a new observation for an earlier period (late arrival) or a revised value ->
  **recompute** the state from the start of the loaded history with the current
  knowledge (``recompute_reason`` = ``late_arrival`` / ``revision``); results that were
  already recorded for earlier observations stay as detection-time records, the revised
  observation gets its own result, and late/revised values older than
  ``late_alert_window_periods`` are recorded but never open alerts (``is_late``);
* a different algorithm version or configuration digest -> recompute.

The state is never patched in place, so it cannot be silently corrupted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from trend_agent.detection import anomaly as anomaly_mod
from trend_agent.detection import change_points as cp_mod
from trend_agent.detection import cusum as cusum_mod
from trend_agent.detection.anomaly import AnomalyResult
from trend_agent.detection.baseline import BaselineEstimate, BaselineModeler, SeasonalFit
from trend_agent.detection.change_points import BocpdState, ChangePointStep
from trend_agent.detection.cold_start import (
    ColdStartAssessment,
    CorroborationContext,
)
from trend_agent.detection.cold_start import assess as assess_cold_start
from trend_agent.detection.config import (
    LoadedDetectionConfig,
    ResolvedThresholds,
    ScaleFamily,
    StratumContext,
)
from trend_agent.detection.cusum import CusumState, CusumStep
from trend_agent.detection.preprocessing import AnalysisSeries
from trend_agent.detection.significance import NullLookup, stratum_key
from trend_agent.schemas.detection import (
    BaselineReadiness,
    DetectorType,
    PreprocessStatus,
    Reason,
    ResultStatus,
)
from trend_agent.schemas.time_series import StoredPoint

ENGINE_VERSION = "engine-v1"
ANALYSABLE = frozenset(
    {
        PreprocessStatus.READY,
        PreprocessStatus.PARTIALLY_READY,
        PreprocessStatus.IRREGULAR_SAMPLING,
        PreprocessStatus.INSUFFICIENT_DATA,
    }
)


@dataclass
class EngineState:
    algorithm_version: str
    config_digest: str
    processed_until_slot: int | None = None
    processed_until_period_end: datetime | None = None
    watermark_obs_id: int = 0
    last_obs_id: int | None = None
    cusum: CusumState = field(default_factory=CusumState)
    bocpd: BocpdState | None = None
    fit: SeasonalFit | None = None  # most recent seasonal fit (cache)
    elevated_streak: int = 0
    evaluations: int = 0
    recomputations: int = 0
    last_status: str | None = None
    last_cold_start: dict[str, Any] | None = None

    def payloads(self) -> dict[DetectorType, dict[str, Any]]:
        """Serialised state, one payload per detector type (``detector_states`` rows)."""
        return {
            DetectorType.ENGINE: {
                "version": ENGINE_VERSION,
                "algorithm_version": self.algorithm_version,
                "config_digest": self.config_digest,
                "processed_until_slot": self.processed_until_slot,
                "processed_until_period_end": self.processed_until_period_end.isoformat()
                if self.processed_until_period_end
                else None,
                "watermark_obs_id": self.watermark_obs_id,
                "last_obs_id": self.last_obs_id,
                "elevated_streak": self.elevated_streak,
                "evaluations": self.evaluations,
                "recomputations": self.recomputations,
                "last_status": self.last_status,
            },
            DetectorType.ROBUST_ANOMALY: {"seasonal_fit": self.fit.as_dict() if self.fit else None},
            DetectorType.CUSUM: self.cusum.as_dict(),
            DetectorType.CHANGE_POINT: self.bocpd.as_dict() if self.bocpd else {},
            DetectorType.COLD_START: self.last_cold_start or {},
        }

    @classmethod
    def from_payloads(cls, payloads: dict[DetectorType, dict[str, Any]]) -> EngineState:
        e = payloads[DetectorType.ENGINE]
        fit_data = payloads.get(DetectorType.ROBUST_ANOMALY, {}).get("seasonal_fit")
        cp_data = payloads.get(DetectorType.CHANGE_POINT) or {}
        end = e.get("processed_until_period_end")
        return cls(
            algorithm_version=e["algorithm_version"],
            config_digest=e["config_digest"],
            processed_until_slot=e.get("processed_until_slot"),
            processed_until_period_end=datetime.fromisoformat(end) if end else None,
            watermark_obs_id=int(e.get("watermark_obs_id", 0)),
            last_obs_id=e.get("last_obs_id"),
            cusum=CusumState.from_dict(payloads.get(DetectorType.CUSUM) or {}),
            bocpd=BocpdState.from_dict(cp_data) if cp_data else None,
            fit=_fit_from_dict(fit_data) if fit_data else None,
            elevated_streak=int(e.get("elevated_streak", 0)),
            evaluations=int(e.get("evaluations", 0)),
            recomputations=int(e.get("recomputations", 0)),
            last_status=e.get("last_status"),
            last_cold_start=payloads.get(DetectorType.COLD_START) or None,
        )


def _fit_from_dict(d: dict[str, Any]) -> SeasonalFit:
    return SeasonalFit(
        block=int(d["block"]),
        eligible=bool(d["eligible"]),
        selected=bool(d["selected"]),
        profile=tuple(float(x) for x in d["profile"]),
        dispersion=float(d["dispersion"]),
        robust_dispersion=float(d["robust_dispersion"]),
        reduction=float(d["reduction"]),
        kw_statistic=d.get("kw_statistic"),
        kw_p_value=d.get("kw_p_value"),
        n=int(d["n"]),
        reason=str(d["reason"]),
    )


@dataclass(frozen=True)
class DetectorHypothesis:
    """One testable statistic with a calibrated p-value (input to FDR control)."""

    key: str
    detector: DetectorType
    p_value: float
    exact: bool
    calibration_version: str


@dataclass(frozen=True)
class PointEvaluation:
    series_id: int
    target_id: int | None
    slot: int
    period_start: datetime
    period_end: datetime
    observation: StoredPoint | None
    value: float | None
    quality: str
    available_at: datetime | None
    availability_basis: str | None
    baseline: BaselineEstimate
    thresholds: ResolvedThresholds
    anomaly: AnomalyResult
    cusum: CusumStep
    change_point: ChangePointStep
    cold_start: ColdStartAssessment | None
    persistence: int
    is_late: bool
    preprocess_status: PreprocessStatus
    series_reasons: tuple[str, ...]
    hypotheses: tuple[DetectorHypothesis, ...]
    cusum_p_value: float | None

    @property
    def observation_id(self) -> int | None:
        return self.observation.id if self.observation else None

    @property
    def any_trigger(self) -> bool:
        return self.anomaly.triggered or self.cusum.triggered or self.change_point.triggered

    @property
    def any_watch(self) -> bool:
        return (
            self.anomaly.status is ResultStatus.WATCH
            or self.cusum.status is ResultStatus.WATCH
            or self.change_point.status is ResultStatus.WATCH
        )


@dataclass
class EngineOutput:
    state: EngineState
    evaluations: list[PointEvaluation]
    recomputed: bool = False
    recompute_reason: str | None = None
    skipped: str | None = None
    processed_slots: int = 0


class SeriesEngine:
    def __init__(self, loaded: LoadedDetectionConfig, nulls: NullLookup | None = None) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.nulls = nulls or NullLookup()

    def fresh_state(self) -> EngineState:
        return EngineState(self.cfg.algorithm_version, self.loaded.digest)

    def run(
        self,
        series: AnalysisSeries,
        state: EngineState | None,
        *,
        new_obs_ids: set[int] | None = None,
        category: str | None = None,
        cold_context: CorroborationContext | None = None,
    ) -> EngineOutput:
        if series.status not in ANALYSABLE or series.family is None:
            out = EngineOutput(state or self.fresh_state(), [])
            out.skipped = "; ".join(series.reasons) or series.status.value
            return out
        present = {series.first_slot + i: p for i, p in enumerate(series.points) if p is not None}
        if new_obs_ids is None:
            after = state.processed_until_slot if state else None
            emit = {s for s in present if after is None or s > after}
        else:
            emit = {s for s, p in present.items() if p.id in new_obs_ids}
        reason: str | None = None
        if state is None:
            reason = "initial"
        elif (
            state.algorithm_version != self.cfg.algorithm_version
            or state.config_digest != self.loaded.digest
        ):
            reason = "configuration_changed"
        elif state.processed_until_slot is not None and any(
            s <= state.processed_until_slot for s in emit
        ):
            revised = any(present[s].revision > 1 for s in emit if s <= state.processed_until_slot)
            reason = "revision" if revised else "late_arrival"
        recompute = reason is not None
        previous = state
        if recompute:
            state = self.fresh_state()
            if previous is not None:
                state.recomputations = previous.recomputations + 1
                state.evaluations = previous.evaluations
            start = series.first_slot
        else:
            assert state is not None
            assert state.processed_until_slot is not None
            start = state.processed_until_slot + 1
        out = EngineOutput(state, [], recompute and reason != "initial", reason)
        if start > series.last_slot:
            return out
        self._process(series, state, start, emit, out, category, cold_context)
        state.watermark_obs_id = max([state.watermark_obs_id, *(p.id for p in present.values())])
        state.last_status = series.status.value
        return out

    # ------------------------------------------------------------------ internals
    def _process(
        self,
        series: AnalysisSeries,
        state: EngineState,
        start: int,
        emit: set[int],
        out: EngineOutput,
        category: str | None,
        cold_context: CorroborationContext | None,
    ) -> None:
        cfg = self.cfg
        family = series.family
        assert family is not None
        modeler = BaselineModeler(cfg.baseline, series.granularity, family)
        fits: dict[int, SeasonalFit] = {}
        if state.fit is not None:
            fits[state.fit.block] = state.fit
        first_index = start - series.first_slot
        indices = list(range(first_index, series.n_slots))
        baselines = modeler.estimates(series.values, series.first_slot, indices, fits)
        if fits:
            state.fit = fits[max(fits)]
        epsilon = cfg.baseline.robust.epsilon
        floor_abs = cfg.baseline.robust.dispersion_floor_abs.get(family, 1.0)
        late_window = cfg.anomaly.late_alert_window_periods
        anomaly_null = self.nulls.get(
            stratum_key(
                series.provider,
                series.granularity.value,
                series.measurement.value,
                family.value,
                DetectorType.ROBUST_ANOMALY.value,
            ),
            synthetic=series.is_synthetic,
        )
        cusum_null = self.nulls.get(
            stratum_key(
                series.provider,
                series.granularity.value,
                series.measurement.value,
                family.value,
                DetectorType.CUSUM.value,
            ),
            synthetic=series.is_synthetic,
        )
        usable_before = [float(v) for v in series.values[:first_index] if np.isfinite(v)]
        cold_enabled = cfg.enabled(DetectorType.COLD_START)
        threshold_cache: dict[BaselineReadiness, ResolvedThresholds] = {}
        for i, baseline in zip(indices, baselines, strict=True):
            slot = series.first_slot + i
            raw = series.values[i]
            value = float(raw) if np.isfinite(raw) else None
            point = series.points[i]
            readiness = (
                baseline.readiness
                if baseline.readiness is not BaselineReadiness.INSUFFICIENT
                else BaselineReadiness.PROVISIONAL
            )
            thresholds = threshold_cache.get(readiness)
            if thresholds is None:
                thresholds = cfg.thresholds(
                    StratumContext(
                        provider=series.provider,
                        granularity=series.granularity,
                        category=category,
                        readiness=readiness,
                        scale_family=family,
                        measurement=series.measurement,
                    )
                )
                threshold_cache[readiness] = thresholds
            anomaly = anomaly_mod.detect(
                value,
                baseline,
                thresholds,
                cfg.anomaly,
                family,
                epsilon=epsilon,
                null=anomaly_null,
            )
            if (
                point is not None
                and value is None
                and point.sample_quality.value == "partial_period"
            ):
                anomaly = anomaly_mod.not_applicable(
                    ResultStatus.INSUFFICIENT_DATA, Reason.PARTIAL_PERIOD.value
                )
            cusum = self._cusum(series, state, slot, baseline, anomaly)
            cusum_p: float | None = None
            hypotheses: list[DetectorHypothesis] = []
            obs_id = point.id if point is not None else None
            if obs_id is not None and anomaly.score is not None and anomaly_null is not None:
                p, exact = anomaly_null.p_value(anomaly.score)
                hypotheses.append(
                    DetectorHypothesis(
                        f"obs:{obs_id}:{DetectorType.ROBUST_ANOMALY.value}",
                        DetectorType.ROBUST_ANOMALY,
                        p,
                        exact,
                        anomaly_null.calibration_version,
                    )
                )
            if obs_id is not None and cusum.statistic is not None and cusum_null is not None:
                cusum_p, exact = cusum_null.p_value(cusum.statistic)
                hypotheses.append(
                    DetectorHypothesis(
                        f"obs:{obs_id}:{DetectorType.CUSUM.value}",
                        DetectorType.CUSUM,
                        cusum_p,
                        exact,
                        cusum_null.calibration_version,
                    )
                )
            change = self._change_point(
                series, state, slot, value, baseline, floor_abs, thresholds.min_absolute_effect
            )
            cold: ColdStartAssessment | None = None
            if value is not None:
                usable_before.append(value)
            if cold_enabled and baseline.readiness is not BaselineReadiness.READY:
                cold = assess_cold_start(
                    usable_before,
                    min_baseline=cfg.baseline.robust.min_samples,
                    min_effect=thresholds.min_absolute_effect,
                    ctx=cold_context,
                    cfg=cfg.cold_start,
                    max_stage=cfg.alert_policy.cold_start_max_stage,
                )
                state.last_cold_start = cold.as_dict()
            # Persistence counts periods that are themselves elevated (anomaly watch or
            # above) or that complete a sustained-change alarm; a slowly decaying CUSUM
            # statistic alone does not extend it.
            elevated = anomaly.elevated or cusum.triggered or change.triggered
            if value is not None:
                state.elevated_streak = state.elevated_streak + 1 if elevated else 0
            state.processed_until_slot = slot
            period_start, period_end = series.period(slot)
            state.processed_until_period_end = period_end
            out.processed_slots += 1
            if slot in emit and point is not None:
                available_at, basis = series.available_at(slot)
                state.last_obs_id = point.id
                state.evaluations += 1
                out.evaluations.append(
                    PointEvaluation(
                        series_id=series.series_id,
                        target_id=series.target_id,
                        slot=slot,
                        period_start=period_start,
                        period_end=period_end,
                        observation=point,
                        value=value,
                        quality=point.sample_quality.value,
                        available_at=available_at,
                        availability_basis=basis,
                        baseline=baseline,
                        thresholds=thresholds,
                        anomaly=anomaly,
                        cusum=cusum,
                        change_point=change,
                        cold_start=cold,
                        persistence=state.elevated_streak if elevated else 0,
                        is_late=slot < series.last_slot - late_window,
                        preprocess_status=series.status,
                        series_reasons=tuple(series.reasons),
                        hypotheses=tuple(hypotheses),
                        cusum_p_value=cusum_p,
                    )
                )

    def _cusum(
        self,
        series: AnalysisSeries,
        state: EngineState,
        slot: int,
        baseline: BaselineEstimate,
        anomaly: AnomalyResult,
    ) -> CusumStep:
        cfg = self.cfg.cusum
        if not self.cfg.enabled(DetectorType.CUSUM):
            return cusum_mod.skip(cfg, ResultStatus.NOT_APPLICABLE, "disabled")
        if not series.sequential_ok:
            return cusum_mod.skip(
                cfg, ResultStatus.INSUFFICIENT_DATA, Reason.IRREGULAR_CADENCE.value
            )
        if anomaly.z is None:
            return cusum_mod.skip(cfg, ResultStatus.INSUFFICIENT_DATA, anomaly.reasons[0])
        if baseline.readiness is not BaselineReadiness.READY:
            return cusum_mod.skip(
                cfg, ResultStatus.NOT_APPLICABLE, Reason.PROVISIONAL_BASELINE.value
            )
        return cusum_mod.update(state.cusum, slot, anomaly.z, cfg)

    def _change_point(
        self,
        series: AnalysisSeries,
        state: EngineState,
        slot: int,
        value: float | None,
        baseline: BaselineEstimate,
        floor_abs: float,
        min_effect: float,
    ) -> ChangePointStep:
        cfg = self.cfg.change_point
        if not self.cfg.enabled(DetectorType.CHANGE_POINT):
            return cp_mod.skip(ResultStatus.NOT_APPLICABLE, "disabled")
        if not series.sequential_ok:
            return cp_mod.skip(ResultStatus.INSUFFICIENT_DATA, Reason.IRREGULAR_CADENCE.value)
        if value is None:
            return cp_mod.skip(ResultStatus.INSUFFICIENT_DATA, Reason.MISSING_VALUE.value)
        x = value - (baseline.seasonal_component or 0.0)
        floor = max(floor_abs, self.cfg.baseline.robust.dispersion_floor_rel * abs(x))
        if state.bocpd is None:
            state.bocpd = cp_mod.init_state(x, slot, max(floor_abs, 0.1 * abs(x)), cfg)
            return cp_mod.skip(ResultStatus.INSUFFICIENT_DATA, Reason.INSUFFICIENT_HISTORY.value)
        return cp_mod.update(state.bocpd, x, slot, cfg, floor=floor, min_effect=min_effect)


def scale_family_or_none(series: AnalysisSeries) -> ScaleFamily | None:
    return series.family
