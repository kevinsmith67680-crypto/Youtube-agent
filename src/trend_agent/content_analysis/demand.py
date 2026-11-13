"""Phase 4 -> Phase 5 demand evidence (labels over the existing contract; nothing recomputed).

Content analysis never decides whether demand rose: it copies the Phase 4 alert fields and
labels them with a ``DemandBasis``. An external search surge stays external; proxy
attention stays proxy; content findings can never upgrade either.
"""

from __future__ import annotations

from collections.abc import Sequence

from trend_agent.schemas.content_opportunity import AnalysisTrigger, DemandBasis, DemandEvidence
from trend_agent.schemas.detection import ColdStartState
from trend_agent.schemas.evidence import AlertStage, SurgeClassification
from trend_agent.schemas.surge import SurgeAlert
from trend_agent.schemas.verification import AuthoritativeStatistics
from trend_agent.verification.authority import build_statistics, statement
from trend_agent.verification.service import target_view

C = SurgeClassification


def basis_for(alert: SurgeAlert | None, combined: SurgeClassification | None = None) -> DemandBasis:
    if alert is None:
        return DemandBasis.INSUFFICIENT_EVIDENCE
    cls = combined or alert.classification
    stage = alert.alert_status
    if stage is AlertStage.INSUFFICIENT_EVIDENCE:
        return DemandBasis.INSUFFICIENT_EVIDENCE
    if (
        alert.cold_start_state is ColdStartState.COLD_START_GROWTH_CANDIDATE
        and stage is not AlertStage.CONFIRMED
    ):
        return DemandBasis.COLD_START_GROWTH_CANDIDATE
    if stage is AlertStage.WATCH:
        return DemandBasis.WATCH
    if cls is C.DIRECT_SEARCH_SURGE:
        return (
            DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE
            if stage is AlertStage.CONFIRMED
            else DemandBasis.DIRECT_SEARCH_CANDIDATE
        )
    if cls is C.EXTERNAL_SEARCH_SURGE:
        return DemandBasis.EXTERNAL_SEARCH_SURGE
    # POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY or PROXY_ATTENTION_SIGNAL: never search growth.
    return DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY


def from_alerts(
    alerts: Sequence[SurgeAlert],
    *,
    query: str,
    event_id: int | None = None,
    evidence_ids: dict[int, str] | None = None,
) -> tuple[DemandEvidence, AuthoritativeStatistics]:
    """Demand evidence for one event (or the target's strongest open episode)."""
    ids = evidence_ids or {}
    view = target_view(list(alerts))
    stats = build_statistics(
        view, target_id=alerts[0].target_id if alerts else None, query=query, evidence_ids=ids
    )
    if not alerts:
        return manual(query, None), stats
    chosen = next((a for a in alerts if a.event_id == event_id), None) if event_id else None
    if chosen is None:
        chosen = max(alerts, key=lambda a: (a.alert_status is AlertStage.CONFIRMED, a.severity))
    combined = view.combined_classification if view and event_id is None else None
    basis = basis_for(chosen, combined)
    return (
        DemandEvidence(
            basis=basis,
            trigger=AnalysisTrigger.SURGE_EVENT,
            surge_event_id=chosen.event_id,
            query_id=chosen.query_id,
            canonical_query=chosen.query,
            category=chosen.category,
            topic_cluster_id=chosen.topic_cluster_id,
            detected_at=chosen.detected_at,
            window_start=chosen.observation_window.start,
            window_end=chosen.observation_window.end,
            measurement_type=chosen.measurement_type.value,
            classification=chosen.classification.value,
            alert_stage=chosen.alert_status.value,
            confidence_status=chosen.confidence_status.value,
            cold_start_state=chosen.cold_start_state.value if chosen.cold_start_state else None,
            severity=chosen.severity,
            relative_change=stats.relative_change,
            freshness=chosen.freshness,
            limitations=chosen.limitations,
            evidence_ids=tuple(ids[a.event_id] for a in alerts if a.event_id in ids),
            source_references=tuple(
                r.source_reference
                for r in (chosen.observation, chosen.peak)
                if r and r.source_reference
            ),
            synthetic=chosen.synthetic,
            statement=stats.statement,
        ),
        stats,
    )


def manual(query: str, query_id: int | None, *, synthetic: bool = False) -> DemandEvidence:
    stats = AuthoritativeStatistics(target_id=None, available=False)
    return DemandEvidence(
        basis=DemandBasis.MANUAL_NO_DEMAND_EVIDENCE,
        trigger=AnalysisTrigger.MANUAL,
        query_id=query_id,
        canonical_query=query,
        limitations=("manual analysis: no Phase 4 demand measurement is attached",),
        synthetic=synthetic,
        statement=statement(stats, query),
    )


def from_statistics(
    stats: AuthoritativeStatistics,
    *,
    query: str,
    query_id: int | None,
    category: str | None = None,
    topic_cluster_id: int | None = None,
    cold_start_state: str | None = None,
) -> DemandEvidence:
    """Demand evidence from an engine statistics block (fixtures, replays)."""
    if not stats.available or stats.classification is None or stats.highest_stage is None:
        return manual(query, query_id, synthetic=stats.synthetic)
    stage = stats.highest_stage
    cls = stats.classification
    if stage is AlertStage.INSUFFICIENT_EVIDENCE:
        basis = DemandBasis.INSUFFICIENT_EVIDENCE
    elif (
        cold_start_state == ColdStartState.COLD_START_GROWTH_CANDIDATE.value
        and stage is not AlertStage.CONFIRMED
    ):
        basis = DemandBasis.COLD_START_GROWTH_CANDIDATE
    elif stage is AlertStage.WATCH:
        basis = DemandBasis.WATCH
    elif cls is C.DIRECT_SEARCH_SURGE:
        basis = (
            DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE
            if stage is AlertStage.CONFIRMED
            else DemandBasis.DIRECT_SEARCH_CANDIDATE
        )
    elif cls is C.EXTERNAL_SEARCH_SURGE:
        basis = DemandBasis.EXTERNAL_SEARCH_SURGE
    else:
        basis = DemandBasis.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY
    return DemandEvidence(
        basis=basis,
        trigger=AnalysisTrigger.SURGE_EVENT,
        surge_event_id=stats.event_ids[0] if stats.event_ids else None,
        query_id=query_id,
        canonical_query=query,
        category=category,
        topic_cluster_id=topic_cluster_id,
        detected_at=stats.detected_at,
        measurement_type=stats.evidence_categories[0].value if stats.evidence_categories else None,
        classification=cls.value,
        alert_stage=stage.value,
        confidence_status=stats.confidence_status.value if stats.confidence_status else None,
        cold_start_state=cold_start_state,
        severity=stats.severity,
        relative_change=stats.relative_change,
        freshness=stats.freshness,
        evidence_ids=stats.evidence_ids,
        synthetic=stats.synthetic,
        statement=stats.statement,
    )
