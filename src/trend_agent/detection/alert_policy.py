"""Deterministic alert policy: detector results + evidence category -> alert stage.

Decision table (evaluated for every newly available observation, in this order):

1. series not analysable (incompatible / invalid scale / unsupported) -> INSUFFICIENT_EVIDENCE
2. missing or partial value                                         -> INSUFFICIENT_EVIDENCE
3. late or revised value older than the late-alert window           -> recorded, no stage
4. no baseline (cold start)                                          -> cold-start stage
   (WATCH / CANDIDATE with corroboration, never CONFIRMED) or INSUFFICIENT_EVIDENCE
5. no detector elevated: REJECTED if the anomaly detector crossed its threshold but
   failed validation (effect too small, level too low), otherwise nothing
6. only WATCH-level evidence                                         -> WATCH
7. any detector TRIGGERED                                            -> CANDIDATE, unless
   the only trigger is an anomaly whose calibrated p-value was not accepted by the FDR
   procedure of this run (-> WATCH)
8. CONFIRMED requires all of: direct YouTube search interest; READY baseline with a
   stable dispersion; complete (not low-volume) value; elevation persisting for
   ``confirm_min_periods`` consecutive periods; comparable history; and, when
   ``confirm_requires_calibration``, statistical support (a calibrated hypothesis of this
   observation accepted by BH/BY).
9. the stage is capped by the evidence category (external / proxy -> CANDIDATE).

Confidence: STATISTICALLY_SUPPORTED only with an accepted calibrated hypothesis, a READY
baseline and a stable dispersion; EMPIRICALLY_THRESHOLDED for other threshold crossings;
LOW_CONFIDENCE_COLD_START for cold start. Every unmet requirement is recorded as a reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trend_agent.detection.config import DetectionConfig, ScaleFamily
from trend_agent.detection.engine import ANALYSABLE, PointEvaluation
from trend_agent.detection.severity import Severity
from trend_agent.detection.severity import score as severity_score
from trend_agent.detection.significance import FamilyResult
from trend_agent.schemas.detection import (
    BaselineReadiness,
    DetectorType,
    Reason,
    ResultStatus,
    SupportStatus,
)
from trend_agent.schemas.evidence import AlertStage, SurgeClassification, cap_stage
from trend_agent.schemas.monitoring import ComparabilityStatus, MeasurementCategory

POLICY_VERSION = "alert-policy-v1"
COMPARABLE = frozenset(
    {ComparabilityStatus.COMPARABLE, ComparabilityStatus.COMPARABLE_WITH_CAVEATS}
)


@dataclass(frozen=True)
class DecisionContext:
    classification: SurgeClassification
    measurement: MeasurementCategory
    family: ScaleFamily
    multiple_testing: FamilyResult | None
    comparability: ComparabilityStatus | None
    freshness: str | None = None


@dataclass(frozen=True)
class AlertDecision:
    stage: AlertStage | None
    confidence: SupportStatus
    reasons: tuple[str, ...]
    severity: Severity
    triggered: tuple[DetectorType, ...]
    accepted_hypotheses: tuple[str, ...]
    calibration_version: str | None
    version: str = POLICY_VERSION

    @property
    def opens(self) -> bool:
        return self.stage in (AlertStage.WATCH, AlertStage.CANDIDATE, AlertStage.CONFIRMED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value if self.stage else None,
            "confidence": self.confidence.value,
            "reasons": list(self.reasons),
            "severity": self.severity.as_dict(),
            "triggered": [d.value for d in self.triggered],
            "accepted_hypotheses": list(self.accepted_hypotheses),
            "calibration_version": self.calibration_version,
            "version": self.version,
        }


def _category_reasons(classification: SurgeClassification, m: MeasurementCategory) -> list[str]:
    if classification is SurgeClassification.EXTERNAL_SEARCH_SURGE:
        return [Reason.EXTERNAL_EVIDENCE.value]
    if classification is SurgeClassification.PROXY_ATTENTION_SIGNAL:
        if m is MeasurementCategory.SEARCH_SUGGESTION_PRESENCE:
            return [Reason.SUGGESTION_PRESENCE.value]
        return [Reason.PROXY_EVIDENCE.value]
    return []


def decide(ev: PointEvaluation, ctx: DecisionContext, cfg: DetectionConfig) -> AlertDecision:
    reasons: list[str] = []
    triggered = tuple(
        d
        for d, hit in (
            (DetectorType.ROBUST_ANOMALY, ev.anomaly.triggered),
            (DetectorType.CUSUM, ev.cusum.triggered),
            (DetectorType.CHANGE_POINT, ev.change_point.triggered),
        )
        if hit
    )
    sev = severity_score(
        ev,
        ctx.family,
        cfg.severity,
        cusum_h=cfg.cusum.h,
        min_expected_for_relative=cfg.anomaly.min_expected_for_relative.get(ctx.family, 5.0),
        freshness=ctx.freshness,
    )
    mt = ctx.multiple_testing
    accepted = tuple(h.key for h in ev.hypotheses if mt is not None and mt.is_accepted(h.key))
    calibration = next((h.calibration_version for h in ev.hypotheses), None)

    def out(
        stage: AlertStage | None, confidence: SupportStatus, extra: list[str] | None = None
    ) -> AlertDecision:
        all_reasons = reasons + (extra or [])
        if stage is not None and stage in (
            AlertStage.WATCH,
            AlertStage.CANDIDATE,
            AlertStage.CONFIRMED,
        ):
            capped = cap_stage(stage, ctx.classification)
            if capped is not stage or ctx.classification is not (
                SurgeClassification.DIRECT_SEARCH_SURGE
            ):
                all_reasons += _category_reasons(ctx.classification, ctx.measurement)
            stage = capped
        return AlertDecision(
            stage,
            confidence,
            tuple(dict.fromkeys(all_reasons)),
            sev,
            triggered,
            accepted,
            calibration,
        )

    if ev.preprocess_status not in ANALYSABLE:
        return out(
            AlertStage.INSUFFICIENT_EVIDENCE, SupportStatus.INSUFFICIENT_DATA, [*ev.series_reasons]
        )
    if ev.value is None:
        why = Reason.PARTIAL_PERIOD if ev.quality == "partial_period" else Reason.MISSING_VALUE
        return out(AlertStage.INSUFFICIENT_EVIDENCE, SupportStatus.INSUFFICIENT_DATA, [why.value])
    if ev.is_late:
        return out(None, SupportStatus.NOT_SUPPORTED, [Reason.LATE_REVISION.value])
    if ev.baseline.readiness is BaselineReadiness.INSUFFICIENT:
        reasons.append(Reason.COLD_START.value)
        cs = ev.cold_start
        if cs is not None and cs.stage is not None:
            return out(cs.stage, SupportStatus.LOW_CONFIDENCE_COLD_START, list(cs.reasons))
        return out(
            AlertStage.INSUFFICIENT_EVIDENCE,
            SupportStatus.LOW_CONFIDENCE_COLD_START,
            list(cs.reasons) if cs else [],
        )
    if ev.baseline.readiness is BaselineReadiness.PROVISIONAL:
        reasons.append(Reason.PROVISIONAL_BASELINE.value)
    if ev.baseline.unstable:
        reasons.append(Reason.UNSTABLE_DENOMINATOR.value)
    if not triggered and not ev.any_watch:
        if ev.anomaly.status is ResultStatus.REJECTED:
            return out(AlertStage.REJECTED, SupportStatus.NOT_SUPPORTED, list(ev.anomaly.reasons))
        return out(None, SupportStatus.NOT_SUPPORTED)
    if not triggered:
        return out(AlertStage.WATCH, SupportStatus.EMPIRICALLY_THRESHOLDED)
    supported = (
        bool(accepted)
        and ev.baseline.readiness is BaselineReadiness.READY
        and not ev.baseline.unstable
    )
    anomaly_hyp = any(h.detector is DetectorType.ROBUST_ANOMALY for h in ev.hypotheses)
    if triggered == (DetectorType.ROBUST_ANOMALY,) and anomaly_hyp and not accepted:
        return out(
            AlertStage.WATCH,
            SupportStatus.EMPIRICALLY_THRESHOLDED,
            [Reason.NOT_ACCEPTED_BY_FDR.value],
        )
    confidence = (
        SupportStatus.STATISTICALLY_SUPPORTED
        if supported
        else SupportStatus.EMPIRICALLY_THRESHOLDED
    )
    if not ev.hypotheses:
        reasons.append(Reason.UNCALIBRATED.value)
    unmet: list[str] = []
    ap = cfg.alert_policy
    if ctx.classification is not SurgeClassification.DIRECT_SEARCH_SURGE:
        unmet += _category_reasons(ctx.classification, ctx.measurement)
    if ev.baseline.readiness is not BaselineReadiness.READY:
        unmet.append(Reason.PROVISIONAL_BASELINE.value)
    if ev.baseline.unstable:
        unmet.append(Reason.UNSTABLE_DENOMINATOR.value)
    if ev.quality == "low_volume":
        unmet.append(Reason.LOW_VOLUME.value)
    if ev.persistence < ap.confirm_min_periods:
        unmet.append(Reason.NOT_PERSISTENT.value)
    if ctx.comparability is not None and ctx.comparability not in COMPARABLE:
        unmet.append(Reason.INCOMPARABLE.value)
    if ap.confirm_requires_calibration and not supported:
        unmet.append(
            Reason.UNCALIBRATED.value if not ev.hypotheses else Reason.NOT_ACCEPTED_BY_FDR.value
        )
    if unmet:
        return out(AlertStage.CANDIDATE, confidence, unmet)
    return out(AlertStage.CONFIRMED, confidence)
