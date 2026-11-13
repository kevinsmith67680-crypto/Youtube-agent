"""Deterministic explanation templates for surge episodes (no language model involved).

Every sentence is filled from stored, structured results: values, baseline, detectors,
p-values, reasons and the evidence category. The same inputs always produce the same
text. Uncertainty and the limits of the evidence are stated explicitly; a relative index
is never described as a number of searches, and indirect signals are never described as
measured YouTube search demand.
"""

from __future__ import annotations

from trend_agent.detection.alert_policy import AlertDecision
from trend_agent.detection.config import DetectionConfig, ScaleFamily
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import PointEvaluation
from trend_agent.detection.events import Episode
from trend_agent.schemas.detection import (
    BaselineModel,
    BaselineReadiness,
    DetectorType,
    Reason,
    SupportStatus,
)
from trend_agent.schemas.evidence import MEANING, AlertStage, SurgeClassification
from trend_agent.schemas.monitoring import MeasurementCategory

SYNTHETIC_PREFIX = "[SYNTHETIC] "

DETECTOR_NAMES = {
    DetectorType.ROBUST_ANOMALY: "robust anomaly detector",
    DetectorType.CUSUM: "sustained-change (CUSUM) detector",
    DetectorType.CHANGE_POINT: "change-point (BOCPD) detector",
}

REASON_TEXT: dict[str, str] = {
    Reason.NOT_PERSISTENT.value: "the elevation has not yet persisted long enough",
    Reason.UNCALIBRATED.value: "thresholds are not calibrated for this source",
    Reason.NOT_ACCEPTED_BY_FDR.value: "the calibrated p-value was not accepted by FDR control",
    Reason.PROVISIONAL_BASELINE.value: "the baseline is provisional (short history)",
    Reason.UNSTABLE_DENOMINATOR.value: "the baseline dispersion estimate is unstable",
    Reason.LOW_VOLUME.value: "the value is flagged low-volume by the provider",
    Reason.INCOMPARABLE.value: "the history is not fully comparable",
    Reason.EXTERNAL_EVIDENCE.value: (
        "the evidence is search interest on another property, not YouTube Search"
    ),
    Reason.PROXY_EVIDENCE.value: "the evidence is an indirect attention signal",
    Reason.SUGGESTION_PRESENCE.value: "suggestion presence is not search volume",
    Reason.BUDGET_EXHAUSTED.value: "the daily alert budget was exhausted",
}


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}"


def _detectors(names: list[DetectorType]) -> str:
    words = [DETECTOR_NAMES[d] for d in names]
    if len(words) == 1:
        return f"The {words[0]} triggered."
    return "The " + ", ".join(words[:-1]) + f" and {words[-1]} triggered."


def subject(meta: EpisodeMeta, family: ScaleFamily | None) -> str:
    m = meta.measurement
    if m is MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST:
        return "YouTube search-interest index"
    if m is MeasurementCategory.EXTERNAL_SEARCH_INTEREST:
        return "External search-interest index (not YouTube Search)"
    unit = "count" if family is ScaleFamily.COUNT else "index"
    return f"{m.value.replace('_', ' ')} {unit}"


def explain(
    ep: Episode,
    ev: PointEvaluation | None,
    decision: AlertDecision | None,
    meta: EpisodeMeta,
    *,
    family: ScaleFamily | None,
    cfg: DetectionConfig,
    fdr: tuple[str, float, int] | None = None,
) -> str:
    prefix = SYNTHETIC_PREFIX if ep.is_synthetic else ""
    if ev is None or decision is None:
        return prefix + (ep.explanation.removeprefix(SYNTHETIC_PREFIX) or "Surge episode.")
    parts: list[str] = []
    cold = ev.baseline.readiness is BaselineReadiness.INSUFFICIENT
    if cold:
        cs = ev.cold_start
        n = cs.usable_values if cs else 0
        parts.append(
            "This topic has insufficient historical observations for a statistically reliable "
            f"baseline ({n} comparable observation(s); {cfg.baseline.robust.min_samples} "
            "required). Current evidence supports monitoring but not confirmation of a search "
            "surge."
        )
        if cs is not None and cs.first_value is not None:
            parts.append(
                f"Measured values moved from {_fmt(cs.first_value)} to {_fmt(cs.last_value)}"
                + (f" (x{cs.growth_ratio:.1f})" if cs.growth_ratio else "")
                + f"; cold-start state: {cs.state.value.replace('_', ' ')}."
            )
            if cs.corroborated:
                types = ", ".join(cs.corroboration.get("corroborating_types", []))
                parts.append(
                    f"Independent discovery evidence ({types}) corroborates that the topic is "
                    "new or active; it explains possible interest but is not a measurement."
                )
    else:
        window = f"{ev.period_start:%Y-%m-%d %H:%M}..{ev.period_end:%Y-%m-%d %H:%M} UTC"
        expected = ev.baseline.expected
        if ep.baseline_expected is not None and ep.status is not AlertStage.WATCH:
            expected = ep.baseline_expected
        parts.append(
            f"{subject(meta, family)} increased from an expected baseline of {_fmt(expected)} "
            f"to {_fmt(ev.value)} for a comparable observation window ({window})."
        )
        if decision.triggered:
            parts.append(_detectors(list(decision.triggered)))
        else:
            parts.append(
                "No detector reached its alert threshold; the result is preliminary (watch)."
            )
        model = (
            "a seasonal (weekday / hour-of-day) profile"
            if ev.baseline.model is BaselineModel.SEASONAL_PROFILE
            else "a robust rolling median"
        )
        parts.append(
            f"The baseline is {model} of {ev.baseline.sample_count} prior observations "
            f"(dispersion {_fmt(ev.baseline.dispersion)}, {ev.baseline.readiness.value})."
        )
        if ev.persistence > 1:
            parts.append(f"The elevation has persisted for {ev.persistence} consecutive periods.")
    parts.append(MEANING[meta.measurement])
    if decision.confidence is SupportStatus.STATISTICALLY_SUPPORTED and fdr is not None:
        method, q, size = fdr
        parts.append(
            f"The increase is statistically supported: a calibrated p-value was accepted by "
            f"{method.replace('_', '-')} control at q={q:g} among {size} hypotheses of this run "
            "(per-run false-discovery control, not an online guarantee)."
        )
    elif decision.confidence is SupportStatus.EMPIRICALLY_THRESHOLDED:
        parts.append(
            "Thresholds are empirical: no calibrated error rate supports this result, so it is "
            "not a statistically confirmed surge."
        )
    unmet = [REASON_TEXT[r] for r in decision.reasons if r in REASON_TEXT]
    if ep.status is AlertStage.CANDIDATE and unmet and not cold:
        parts.append("Not confirmed because " + "; ".join(dict.fromkeys(unmet)) + ".")
    if ep.classification is SurgeClassification.PROXY_ATTENTION_SIGNAL:
        parts.append("This is an attention candidate only, not a search-demand surge.")
    return prefix + " ".join(parts)


def limitations(
    ep: Episode,
    ev: PointEvaluation | None,
    decision: AlertDecision | None,
    family: ScaleFamily | None,
) -> tuple[str, ...]:
    out: list[str] = []
    if family is ScaleFamily.INDEX:
        out.append("relative index: values are not numbers of searches")
    if ep.is_synthetic:
        out.append("synthetic test data: not a real-world measurement")
    if ep.classification is SurgeClassification.EXTERNAL_SEARCH_SURGE:
        out.append("external search property: not YouTube Search interest")
    if ep.classification is SurgeClassification.PROXY_ATTENTION_SIGNAL:
        out.append("indirect attention signal: not measured search demand")
    if decision is not None:
        if decision.confidence is not SupportStatus.STATISTICALLY_SUPPORTED:
            out.append("no calibrated error rate for this result")
        if decision.confidence is SupportStatus.LOW_CONFIDENCE_COLD_START:
            out.append("cold start: no reliable baseline, low statistical confidence")
    if ev is not None:
        if ev.baseline.unstable:
            out.append("unstable baseline dispersion")
        if ev.baseline.readiness is BaselineReadiness.PROVISIONAL:
            out.append("provisional baseline (short history)")
        if ev.quality == "low_volume":
            out.append("provider flagged the value as low volume")
    out.append("FDR control (when applied) holds per detection run, not over time")
    return tuple(out)
