"""Versioned surge-alert output contract (consumed by Phase 5).

``SurgeAlert`` is a self-contained snapshot of one surge episode: what was measured, on
which scale, against which baseline, which detectors fired (with versions, scores,
thresholds and p-values where valid), how multiple testing was handled, what limits the
conclusion, and where every number came from. Phase 5 can use it without recomputing any
statistic. The model validates the evidence rules again (defence in depth): a CONFIRMED
alert must be direct YouTube search interest with a ready baseline and comparable history.

The JSON schema is published by ``trend-agent detect schema``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trend_agent.schemas.detection import (
    BaselineModel,
    BaselineReadiness,
    ColdStartState,
    DetectorType,
    MultipleTestingMethod,
    ResultStatus,
    SupportStatus,
)
from trend_agent.schemas.evidence import (
    AlertStage,
    SurgeClassification,
    classify,
    validate_stage,
)
from trend_agent.schemas.monitoring import ComparabilityStatus, MeasurementCategory

SCHEMA_VERSION: Literal["surge-alert/1.0"] = "surge-alert/1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ObservationWindow(_Frozen):
    start: datetime
    end: datetime


class ObservationRef(_Frozen):
    observation_id: int | None
    revision: int | None
    series_id: int | None
    period_start: datetime | None
    period_end: datetime | None
    value: float | None
    available_at: datetime | None = Field(
        description="When the value became available (provider publication time, else the "
        "conservative collection/ingestion time)"
    )
    availability_basis: str | None
    collected_at: datetime | None
    provider_published_at: datetime | None
    source_reference: str | None
    data_policy_id: str | None


class BaselineSummary(_Frozen):
    model: BaselineModel
    model_version: str
    expected: float | None
    observed: float | None
    dispersion: float | None
    dispersion_source: str | None
    sample_count: int
    readiness: BaselineReadiness
    resolution: str
    scale: str
    unit: str
    normalization_context: str = Field(description="normalisation key (first 16 hex chars)")
    seasonal_component: float | None = None
    absolute_difference: float | None = None
    relative_difference: float | None = Field(
        default=None, description="observed/expected - 1, only when the baseline is not tiny"
    )


class DetectorSummary(_Frozen):
    name: DetectorType
    version: str
    triggered: bool
    status: ResultStatus
    score: float | None
    threshold: float | None
    p_value: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class MultipleTestingSummary(_Frozen):
    method: MultipleTestingMethod
    family_size: int
    accepted: bool
    level: float | None = Field(description="FDR level q, or None for alert budgets")
    threshold_version: str
    calibration_version: str | None = None
    note: str = ""


class ComparabilitySummary(_Frozen):
    status: ComparabilityStatus
    reasons: tuple[str, ...] = ()


class SeverityComponentOut(_Frozen):
    name: str
    raw: float | None
    normalized: float
    weight: float
    contribution: float


class EvidenceRefOut(_Frozen):
    evidence_type: str
    observation_id: int | None
    detector_result_id: int | None
    source_reference: str | None
    contribution: float


class SurgeAlert(_Frozen):
    schema_version: Literal["surge-alert/1.0"] = SCHEMA_VERSION
    event_id: int
    query: str
    query_id: int
    target_id: int
    keyword_id: int | None
    topic_cluster_id: int | None
    category: str
    region: str
    language: str | None
    provider: str
    measurement_type: MeasurementCategory = Field(
        description="effective evidence category (what a synthetic value simulates)"
    )
    is_proxy: bool
    classification: SurgeClassification
    alert_status: AlertStage
    confidence_status: SupportStatus
    cold_start_state: ColdStartState | None = None
    severity: float = Field(ge=0, le=100)
    maximum_severity: float = Field(ge=0, le=100)
    severity_note: str = (
        "Severity is an operational ranking index (0-100), not a probability that the "
        "surge is real."
    )
    severity_components: tuple[SeverityComponentOut, ...] = ()
    episode_start: datetime
    first_detected_at: datetime
    detected_at: datetime
    last_updated_at: datetime
    resolved_at: datetime | None = None
    observation_window: ObservationWindow
    observation: ObservationRef
    peak: ObservationRef | None = None
    baseline: BaselineSummary
    detectors: tuple[DetectorSummary, ...]
    multiple_testing: MultipleTestingSummary | None
    comparability: ComparabilitySummary
    source_quality: str
    freshness: str | None
    limitations: tuple[str, ...]
    explanation: str
    reasons: tuple[str, ...] = ()
    evidence: tuple[EvidenceRefOut, ...] = ()
    algorithm_version: str
    configuration_version: str
    synthetic: bool
    generated_at: datetime

    @model_validator(mode="after")
    def _evidence_rules(self) -> SurgeAlert:
        expected = classify(self.measurement_type, is_proxy=self.is_proxy)
        if self.classification is not expected:
            raise ValueError(
                f"classification {self.classification.value} does not match measurement "
                f"{self.measurement_type.value} (expected {expected.value})"
            )
        validate_stage(self.alert_status, self.classification)
        if self.alert_status is AlertStage.CONFIRMED:
            if self.baseline.readiness is not BaselineReadiness.READY:
                raise ValueError("CONFIRMED requires a ready baseline")
            if self.comparability.status not in (
                ComparabilityStatus.COMPARABLE,
                ComparabilityStatus.COMPARABLE_WITH_CAVEATS,
            ):
                raise ValueError("CONFIRMED requires comparable history")
            if self.cold_start_state not in (None, ColdStartState.BASELINE_ESTABLISHED):
                raise ValueError("a cold-start series cannot be CONFIRMED")
        if self.confidence_status is SupportStatus.STATISTICALLY_SUPPORTED and (
            self.multiple_testing is None
            or not self.multiple_testing.accepted
            or self.multiple_testing.calibration_version is None
        ):
            raise ValueError(
                "STATISTICALLY_SUPPORTED requires a calibrated, multiple-testing-accepted result"
            )
        if self.synthetic and not self.explanation.startswith("[SYNTHETIC]"):
            raise ValueError("synthetic alerts must be labelled in the explanation")
        return self


class TargetSurgeView(_Frozen):
    """All open episodes of one target with the combined (target-level) classification.

    A combination never adds independent search evidence: mixed indirect signals become
    POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY, never measured YouTube search growth.
    """

    schema_version: Literal["surge-target/1.0"] = "surge-target/1.0"
    target_id: int
    query: str
    category: str
    topic_cluster_id: int | None
    combined_classification: SurgeClassification | None
    highest_stage: AlertStage | None
    evidence_categories: tuple[MeasurementCategory, ...]
    statement: str
    alerts: tuple[SurgeAlert, ...]
    synthetic: bool
