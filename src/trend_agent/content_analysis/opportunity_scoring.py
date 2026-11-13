"""Interpretable content-opportunity score (0-100), versioned and configurable.

Components (each 0-1, raw features kept alongside):

A demand_evidence        Phase 4 classification/stage (and severity) - copied, not recomputed
B intent_clarity         confidence label and ambiguity of the classified intent
C content_gap_strength   strength of the primary gap (NO_VERIFIABLE_GAP = 0)
D supply_relevance       1 - adequate relevant current results / threshold
E freshness_opportunity  share of relevant results outdated after a verified recent change
F evidence_quality       inspected volume, evidence tier, entity verification, claim checks

A component without data is **excluded** (value None) rather than set to zero; if the
available weight falls below ``min_available_weight`` the score is unavailable. The
score is capped by the demand basis, so an indirect attention signal can never produce
a high-confidence demand opportunity. It is a ranking index, not a probability of
success.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from trend_agent.content_analysis.config import ContentAnalysisConfig
from trend_agent.schemas.content_gap import (
    ContentGap,
    EvidenceStrength,
    GapType,
    SupplyClass,
    SupplySummary,
)
from trend_agent.schemas.content_opportunity import (
    DemandBasis,
    DemandEvidence,
    OpportunityScore,
    ScoreComponent,
)
from trend_agent.schemas.search_intent import ConfidenceLabel, IntentClassification
from trend_agent.schemas.search_result import EvidenceTier, FreshnessAssessment, FreshnessStatus

_STRENGTH = {
    EvidenceStrength.STRONG: 1.0,
    EvidenceStrength.MODERATE: 0.65,
    EvidenceStrength.WEAK: 0.35,
}
_CLARITY = {ConfidenceLabel.HIGH: 1.0, ConfidenceLabel.MEDIUM: 0.6, ConfidenceLabel.LOW: 0.25}


def evidence_quality(
    supply: SupplySummary,
    tiers: Sequence[EvidenceTier],
    entity_state: str | None,
    claims_verified_share: float | None,
    min_inspected: int,
) -> tuple[float, EvidenceStrength, dict[str, Any]]:
    volume = min(1.0, supply.inspected / (2 * min_inspected)) if supply.inspected else 0.0
    tier = 0.0
    if tiers:
        best = max(
            tiers,
            key=lambda t: ["d_insufficient", "a_metadata", "b_captions", "c_full_content"].index(
                t.value
            ),
        )
        tier = {"a_metadata": 0.5, "b_captions": 0.8, "c_full_content": 1.0}.get(best.value, 0.0)
    entity = {"confirmed": 1.0, "reported": 0.6, None: 0.8}.get(entity_state, 0.2)
    claims = claims_verified_share if claims_verified_share is not None else 0.5
    value = round(0.35 * volume + 0.25 * tier + 0.25 * entity + 0.15 * claims, 3)
    label = (
        EvidenceStrength.STRONG
        if value >= 0.8
        else EvidenceStrength.MODERATE
        if value >= 0.55
        else EvidenceStrength.WEAK
    )
    return (
        value,
        label,
        {
            "inspected": supply.inspected,
            "volume": round(volume, 3),
            "best_tier": max(tiers, key=lambda t: t.value).value if tiers else None,
            "entity_state": entity_state,
            "claims_verified_share": claims_verified_share,
        },
    )


def score(
    *,
    demand: DemandEvidence,
    intent: IntentClassification,
    primary_gap: ContentGap | None,
    supply: SupplySummary,
    freshness: Sequence[FreshnessAssessment],
    relevant_ids: Sequence[str],
    adequate: int,
    quality: tuple[float, EvidenceStrength, dict[str, Any]],
    config: ContentAnalysisConfig,
) -> OpportunityScore:
    sc = config.scoring
    w = sc.weights
    comps: list[ScoreComponent] = []

    def add(name: str, value: float | None, raw: dict[str, Any], note: str) -> None:
        comps.append(
            ScoreComponent(
                name=name,  # type: ignore[arg-type]
                value=None if value is None else round(min(1.0, max(0.0, value)), 3),
                weight=w[name],
                contribution=None if value is None else round(w[name] * value, 4),
                raw=raw,
                note=note,
            )
        )

    # A. demand evidence (Phase 4 label; never recomputed)
    if demand.basis is DemandBasis.MANUAL_NO_DEMAND_EVIDENCE:
        add(
            "demand_evidence",
            None,
            {"basis": demand.basis.value},
            "manual analysis: no measured demand",
        )
    else:
        base = sc.demand_values.get(demand.basis, 0.1)
        sev = demand.severity
        value = base * (0.5 + 0.5 * sev / 100) if sev is not None else base * 0.75
        add(
            "demand_evidence",
            value,
            {"basis": demand.basis.value, "severity": sev, "stage": demand.alert_stage},
            "from the Phase 4 classification and severity (copied)",
        )
    # B. intent clarity
    clarity = _CLARITY[intent.confidence] - (0.25 if intent.ambiguity else 0.0)
    add(
        "intent_clarity",
        clarity,
        {
            "confidence": intent.confidence.value,
            "ambiguity": len(intent.ambiguity),
            "specificity": intent.specificity.value,
        },
        "rule-agreement label, not a probability",
    )
    # C. gap strength
    if primary_gap is None or primary_gap.gap_type is GapType.INSUFFICIENT_EVIDENCE:
        add("content_gap_strength", None, {"gap": None}, "no reliable gap conclusion")
    elif primary_gap.gap_type is GapType.NO_VERIFIABLE_GAP:
        add(
            "content_gap_strength",
            0.0,
            {"gap": "no_verifiable_gap"},
            "the analysis established adequate coverage",
        )
    else:
        add(
            "content_gap_strength",
            _STRENGTH[primary_gap.evidence_strength],
            {"gap": primary_gap.gap_type.value, "strength": primary_gap.evidence_strength.value},
            "strength of the primary gap's evidence",
        )
    # D. supply relevance
    if supply.supply_class is SupplyClass.UNKNOWN_SUPPLY:
        add("supply_relevance", None, {"supply": "unknown"}, "nothing retrieved")
    else:
        target = config.gaps.adequate_relevant_current
        add(
            "supply_relevance",
            1.0 - min(1.0, adequate / target),
            {"adequate": adequate, "relevant": supply.relevant, "inspected": supply.inspected},
            "fewer adequate existing results -> more room",
        )
    # E. freshness opportunity
    if not intent.time_sensitive:
        add("freshness_opportunity", 0.0, {"time_sensitive": False}, "evergreen intent")
    elif not relevant_ids:
        add("freshness_opportunity", None, {"relevant": 0}, "no relevant results to assess")
    else:
        fresh = {f.video_id: f for f in freshness}
        stale = sum(
            1
            for v in relevant_ids
            if fresh.get(v)
            and fresh[v].status
            in (FreshnessStatus.POTENTIALLY_OUTDATED, FreshnessStatus.OUTDATED_WITH_EVIDENCE)
        )
        evidenced = sum(
            1
            for v in relevant_ids
            if fresh.get(v) and fresh[v].status is FreshnessStatus.OUTDATED_WITH_EVIDENCE
        )
        value = (stale + evidenced) / (2 * len(relevant_ids))
        add(
            "freshness_opportunity",
            value,
            {
                "relevant": len(relevant_ids),
                "potentially_outdated": stale,
                "outdated_with_evidence": evidenced,
            },
            "verified changes only; age alone does not count",
        )
    # F. evidence quality
    add("evidence_quality", quality[0], quality[2], f"evidence quality: {quality[1].value}")
    available = sum(c.weight for c in comps if c.value is not None)
    if available < sc.min_available_weight or (
        primary_gap is not None and primary_gap.gap_type is GapType.INSUFFICIENT_EVIDENCE
    ):
        return OpportunityScore(
            score=None,
            score_version=sc.score_version,
            components=tuple(comps),
            available_weight=round(available, 3),
            cap_reason="insufficient evidence for a score",
        )
    raw = 100.0 * sum(c.contribution or 0.0 for c in comps) / available
    cap = sc.caps.get(demand.basis, 50.0)
    capped = min(raw, cap)
    return OpportunityScore(
        score=round(capped, 1),
        score_version=sc.score_version,
        components=tuple(comps),
        available_weight=round(available, 3),
        cap=cap,
        cap_reason=(
            f"capped at {cap:.0f} for demand basis {demand.basis.value}" if raw > cap else None
        ),
    )
