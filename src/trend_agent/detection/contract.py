"""Builds the versioned ``SurgeAlert`` contract (``schemas/surge.py``) for an episode.

``make_renderer`` returns the callback the episode manager uses after every change: it
renders the deterministic explanation and stores the full, validated alert payload on
the episode, so Phase 5 reads a self-contained record without recomputing statistics.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from trend_agent.detection.alert_policy import AlertDecision
from trend_agent.detection.config import LoadedDetectionConfig, ScaleFamily
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import PointEvaluation
from trend_agent.detection.events import Episode
from trend_agent.detection.explanations import explain, limitations
from trend_agent.detection.significance import FamilyResult
from trend_agent.logging import get_logger, log_event
from trend_agent.monitoring.comparability import ComparabilityAssessment
from trend_agent.schemas.detection import (
    BaselineModel,
    BaselineReadiness,
    DetectorType,
    MultipleTestingMethod,
    ResultStatus,
    SupportStatus,
)
from trend_agent.schemas.monitoring import ComparabilityStatus
from trend_agent.schemas.surge import (
    BaselineSummary,
    ComparabilitySummary,
    DetectorSummary,
    EvidenceRefOut,
    MultipleTestingSummary,
    ObservationRef,
    ObservationWindow,
    SeverityComponentOut,
    SurgeAlert,
)

_log = get_logger(__name__)


def _obs_ref(ev: PointEvaluation) -> ObservationRef:
    p = ev.observation
    return ObservationRef(
        observation_id=p.id if p else None,
        revision=p.revision if p else None,
        series_id=ev.series_id,
        period_start=ev.period_start,
        period_end=ev.period_end,
        value=ev.value,
        available_at=ev.available_at,
        availability_basis=ev.availability_basis,
        collected_at=p.collected_at if p else None,
        provider_published_at=p.provider_published_at if p else None,
        source_reference=p.source_reference if p else None,
        data_policy_id=p.data_policy_id if p else None,
    )


def source_quality(ev: PointEvaluation, comparability: ComparabilityStatus | None) -> str:
    if ev.value is None:
        return "insufficient"
    caveats = (
        ev.quality == "low_volume"
        or ev.baseline.unstable
        or ev.baseline.readiness is not BaselineReadiness.READY
        or comparability is ComparabilityStatus.COMPARABLE_WITH_CAVEATS
    )
    return "acceptable_with_caveats" if caveats else "acceptable"


def build_alert(
    ep: Episode,
    ev: PointEvaluation,
    decision: AlertDecision,
    meta: EpisodeMeta,
    *,
    loaded: LoadedDetectionConfig,
    family: ScaleFamily | None,
    comparability: ComparabilityAssessment | None,
    multiple_testing: FamilyResult | None,
    freshness: str | None,
    generated_at: datetime,
    explanation: str,
) -> SurgeAlert:
    cfg = loaded.config
    b = ev.baseline
    a = ev.anomaly
    cs = ev.cold_start
    detectors = [
        DetectorSummary(
            name=DetectorType.ROBUST_ANOMALY,
            version=a.version,
            triggered=a.triggered,
            status=a.status,
            score=None if a.z is None else round(a.z, 4),
            threshold=a.alert_threshold,
            p_value=a.p_value if a.p_value_exact else None,
            details={"reasons": list(a.reasons), "uncertainty": a.uncertainty},
        ),
        DetectorSummary(
            name=DetectorType.CUSUM,
            version=ev.cusum.version,
            triggered=ev.cusum.triggered,
            status=ev.cusum.status,
            score=None if ev.cusum.statistic is None else round(ev.cusum.statistic, 4),
            threshold=ev.cusum.threshold,
            p_value=ev.cusum_p_value,
            details={"k": cfg.cusum.k, "reasons": list(ev.cusum.reasons)},
        ),
        DetectorSummary(
            name=DetectorType.CHANGE_POINT,
            version=ev.change_point.version,
            triggered=ev.change_point.triggered,
            status=ev.change_point.status,
            score=ev.change_point.probability,
            threshold=cfg.change_point.probability_threshold,
            details=ev.change_point.as_dict(),
        ),
    ]
    if cs is not None:
        detectors.append(
            DetectorSummary(
                name=DetectorType.COLD_START,
                version=cs.version,
                triggered=cs.stage is not None,
                status=ResultStatus.WATCH if cs.stage else ResultStatus.NOT_TRIGGERED,
                score=cs.growth_ratio,
                threshold=cfg.cold_start.growth_ratio,
                details=cs.as_dict(),
            )
        )
    mts: MultipleTestingSummary | None
    if ev.hypotheses:
        mts = MultipleTestingSummary(
            method=cfg.multiple_testing.method,
            family_size=multiple_testing.family_size if multiple_testing else 0,
            accepted=bool(decision.accepted_hypotheses),
            level=cfg.multiple_testing.fdr_q,
            threshold_version=cfg.threshold_version,
            calibration_version=decision.calibration_version,
            note="per-run Benjamini-Hochberg/Yekutieli control; not an online FDR guarantee",
        )
    else:
        mts = MultipleTestingSummary(
            method=MultipleTestingMethod.ALERT_BUDGET,
            family_size=multiple_testing.family_size if multiple_testing else 0,
            accepted=False,
            level=None,
            threshold_version=cfg.threshold_version,
            note=(
                "no calibrated p-values for this source; configured thresholds plus a daily "
                f"alert budget ({cfg.multiple_testing.max_new_alerts_per_day}/day) apply"
            ),
        )
    comp = comparability
    peak = None
    if ep.peak_observation_id is not None:
        peak = ObservationRef(
            observation_id=ep.peak_observation_id,
            revision=None,
            series_id=ep.series_id,
            period_start=ep.peak_period_start,
            period_end=None,
            value=ep.peak_value,
            available_at=None,
            availability_basis=None,
            collected_at=None,
            provider_published_at=None,
            source_reference=None,
            data_policy_id=None,
        )
    severity_parts = [
        SeverityComponentOut(
            name=c.name,
            raw=c.raw,
            normalized=round(c.normalized, 4),
            weight=round(c.weight, 4),
            contribution=round(c.contribution, 4),
        )
        for c in decision.severity.components
    ]
    evidence = [
        EvidenceRefOut(
            evidence_type="observation",
            observation_id=ev.observation_id,
            detector_result_id=None,
            source_reference=ev.observation.source_reference if ev.observation else None,
            contribution=next(
                (c.normalized for c in decision.severity.components if c.name == "magnitude"),
                0.0,
            ),
        )
    ]
    status = ep.status
    confidence = decision.confidence
    if confidence is SupportStatus.STATISTICALLY_SUPPORTED and not decision.accepted_hypotheses:
        confidence = SupportStatus.EMPIRICALLY_THRESHOLDED
    return SurgeAlert(
        event_id=ep.id or 0,
        query=meta.query,
        query_id=meta.query_id,
        target_id=meta.target_id,
        keyword_id=meta.keyword_id,
        topic_cluster_id=meta.topic_cluster_id,
        category=meta.category.value,
        region=meta.region,
        language=meta.language,
        provider=meta.provider,
        measurement_type=meta.measurement,
        is_proxy=meta.is_proxy,
        classification=meta.classification,
        alert_status=status,
        confidence_status=confidence,
        cold_start_state=cs.state if cs else None,
        severity=round(ep.severity, 2),
        maximum_severity=round(max(ep.maximum_severity, ep.severity), 2),
        severity_components=tuple(severity_parts),
        episode_start=ep.episode_start,
        first_detected_at=ep.first_detected_at,
        detected_at=generated_at,
        last_updated_at=ep.last_updated_at,
        resolved_at=ep.resolved_at,
        observation_window=ObservationWindow(start=ev.period_start, end=ev.period_end),
        observation=_obs_ref(ev),
        peak=peak,
        baseline=BaselineSummary(
            model=b.model,
            model_version=b.version,
            expected=None if b.expected is None else round(b.expected, 4),
            observed=ev.value,
            dispersion=None if b.dispersion is None else round(b.dispersion, 4),
            dispersion_source=b.dispersion_source.value if b.dispersion_source else None,
            sample_count=b.sample_count,
            readiness=b.readiness,
            resolution=b.resolution.value,
            scale=ev.observation.scale_type.value if ev.observation else "unknown",
            unit=ev.observation.unit if ev.observation else "",
            normalization_context=(ev.observation.normalization_key[:16] if ev.observation else ""),
            seasonal_component=None
            if b.model is not BaselineModel.SEASONAL_PROFILE or b.seasonal_component is None
            else round(b.seasonal_component, 4),
            absolute_difference=None
            if a.absolute_difference is None
            else round(a.absolute_difference, 4),
            relative_difference=None
            if a.relative_difference is None
            else round(a.relative_difference, 4),
        ),
        detectors=tuple(detectors),
        multiple_testing=mts,
        comparability=ComparabilitySummary(
            status=comp.status if comp else ComparabilityStatus.INSUFFICIENT_METADATA,
            reasons=tuple(r.value for r in comp.reasons) if comp else ("not assessed",),
        ),
        source_quality=source_quality(ev, comp.status if comp else None),
        freshness=freshness,
        limitations=limitations(ep, ev, decision, family),
        explanation=explanation,
        reasons=decision.reasons,
        evidence=tuple(evidence),
        algorithm_version=cfg.algorithm_version,
        configuration_version=loaded.version_tag,
        synthetic=ep.is_synthetic,
        generated_at=generated_at,
    )


def make_renderer(
    *,
    loaded: LoadedDetectionConfig,
    family: ScaleFamily | None,
    comparability: ComparabilityAssessment | None,
    multiple_testing: FamilyResult | None,
    freshness: str | None,
    now: datetime,
) -> Callable[
    [Episode, PointEvaluation | None, AlertDecision | None, EpisodeMeta],
    tuple[str, dict[str, Any]],
]:
    cfg = loaded.config
    fdr = (
        (
            cfg.multiple_testing.method.value,
            cfg.multiple_testing.fdr_q,
            multiple_testing.family_size,
        )
        if multiple_testing is not None
        else None
    )

    def render(
        ep: Episode,
        ev: PointEvaluation | None,
        decision: AlertDecision | None,
        meta: EpisodeMeta,
    ) -> tuple[str, dict[str, Any]]:
        supports = decision is not None and decision.stage is ep.status
        if ev is None or decision is None or (ep.payload and not supports):
            # The latest value does not (re)support the current stage: keep the supporting
            # evidence and explanation, refresh only the dynamic fields.
            return ep.explanation, refreshed_payload(ep)
        text = explain(ep, ev, decision, meta, family=family, cfg=cfg, fdr=fdr)
        try:
            alert = build_alert(
                ep,
                ev,
                decision,
                meta,
                loaded=loaded,
                family=family,
                comparability=comparability,
                multiple_testing=multiple_testing,
                freshness=freshness,
                generated_at=now,
                explanation=text,
            )
        except ValidationError as exc:  # invariant violated: never publish an invalid alert
            log_event(
                _log,
                "detect.contract_invalid",
                logging.ERROR,
                event_id=ep.id,
                error=str(exc).splitlines()[0],
            )
            payload = refreshed_payload(ep)
            payload["contract_error"] = str(exc).splitlines()[0]
            return text, payload
        return text, alert.model_dump(mode="json")

    return render


def refreshed_payload(ep: Episode) -> dict[str, Any]:
    payload = dict(ep.payload)
    payload.update(
        {
            "alert_status": ep.status.value,
            "severity": round(ep.severity, 2),
            "maximum_severity": round(max(ep.maximum_severity, ep.severity), 2),
            "last_updated_at": ep.last_updated_at.isoformat(),
            "resolved_at": ep.resolved_at.isoformat() if ep.resolved_at else None,
        }
    )
    return payload
