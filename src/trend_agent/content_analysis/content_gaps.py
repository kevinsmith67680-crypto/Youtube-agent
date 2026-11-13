"""Content-gap detection (conservative, scope-limited, evidence-referenced).

Rules run in a fixed order. Abstention comes first: too few inspected results, an
ambiguous or unverifiable entity, or a blocking requirement whose entity cannot be
verified yield INSUFFICIENT_EVIDENCE and nothing else. Adequate existing coverage
(enough relevant, current, intent-matching results about the exact entity) yields
NO_VERIFIABLE_GAP - weak metadata-only gaps are not manufactured on top of it.

Metadata-only absence is reported as a WEAK *hypothesis*; it is never stated as proof
that existing videos fail to cover something.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.content_analysis.config import ContentAnalysisConfig
from trend_agent.content_analysis.entity_verification import QueryEntities, split_version
from trend_agent.schemas.content_gap import (
    STRENGTH_RANK,
    ContentGap,
    EvidenceStrength,
    GapType,
    GapVerification,
    SupplySummary,
)
from trend_agent.schemas.search_intent import (
    EntityRef,
    IntentClassification,
    PrimaryIntent,
    QueryDecomposition,
    RequirementPriority,
)
from trend_agent.schemas.search_result import (
    CoverageStatus,
    EntityMatch,
    EvidenceTier,
    FreshnessAssessment,
    FreshnessStatus,
    RelevanceAssessment,
    RequirementSummary,
    RetrievalStatus,
    RetrievedResultSet,
)

METHOD = "content-gap-rules-1.0"
_PRECEDENCE = [
    GapType.NO_RELEVANT_RESULTS,
    GapType.VERSION_MISMATCH,
    GapType.POTENTIALLY_OUTDATED,
    GapType.INSUFFICIENT_PRACTICAL_DEMONSTRATION,
    GapType.MISSING_SPECIFIC_ANSWER,
    GapType.LOW_RELEVANCE,
    GapType.LANGUAGE_OR_REGION_GAP,
    GapType.FORMAT_GAP,
    GapType.NO_VERIFIABLE_GAP,
    GapType.INSUFFICIENT_EVIDENCE,
]
_DEMO_INTENTS = frozenset(
    {
        PrimaryIntent.TUTORIAL,
        PrimaryIntent.DEMONSTRATION,
        PrimaryIntent.GAMING_GUIDE,
        PrimaryIntent.PROBLEM_SOLVING,
    }
)


@dataclass(frozen=True)
class GapInputs:
    query: str
    intent: IntentClassification
    decomposition: QueryDecomposition
    entities: QueryEntities
    results: RetrievedResultSet
    relevance: Sequence[RelevanceAssessment]
    freshness: Sequence[FreshnessAssessment]
    requirements: Sequence[RequirementSummary]
    supply: SupplySummary
    language: str | None
    now: datetime
    video_evidence: Mapping[str, str]
    observation_evidence: Sequence[str]
    entity_evidence: Sequence[str]


def _version_number(version: str | None) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", version or "")
    return float(m.group(0)) if m else None


def _older_than_confirmed(ref: EntityRef, entities: QueryEntities) -> bool:
    """True for an unrecorded version that predates a confirmed version of the same
    product family: earlier versions very likely exist; a newer, unrecorded one may not."""
    mine = _version_number(ref.version)
    if mine is None:
        return False
    family = set(split_version(ref.name, ref.version).family)
    for f in entities.facts:
        # Same family, or a partial mention of it ("notes 2" next to "Quanta Notes 3").
        if f.ref.state != "confirmed" or not family or not family <= set(f.versioned.family):
            continue
        theirs = _version_number(f.versioned.version)
        if theirs is not None and mine < theirs:
            return True
    return False


def detect(inp: GapInputs, config: ContentAnalysisConfig) -> list[ContentGap]:
    g = config.gaps
    intent = inp.intent
    n = inp.supply.inspected

    def gap(
        kind: GapType,
        statement: str,
        strength: EvidenceStrength,
        limitations: Sequence[str],
        *,
        videos: Sequence[str] = (),
        requirements: Sequence[str] = (),
        verification: GapVerification | None = None,
        observations: Mapping[str, float | int | str | None] | None = None,
    ) -> ContentGap:
        support = [inp.video_evidence[v] for v in videos if v in inp.video_evidence]
        support += list(inp.observation_evidence) + list(inp.entity_evidence)
        tier_a = all(r.evidence_tier is EvidenceTier.A_METADATA for r in inp.relevance)
        if verification is None:
            if kind in (GapType.NO_VERIFIABLE_GAP, GapType.INSUFFICIENT_EVIDENCE):
                verification = GapVerification.VERIFIED
            elif strength is EvidenceStrength.WEAK or (
                kind is GapType.MISSING_SPECIFIC_ANSWER and tier_a
            ):
                verification = GapVerification.HYPOTHESIS
            elif any(r.state == "reported" for r in inp.entities.refs):
                verification = GapVerification.UNVERIFIED
            else:
                verification = GapVerification.VERIFIED
        lims = list(limitations)
        if tier_a and kind not in (GapType.INSUFFICIENT_EVIDENCE,):
            lims.append("only titles and descriptions were inspected; video contents were not")
        lims.append(f"scope: {n} retrieved results for this query, region and language")
        return ContentGap(
            gap_type=kind,
            target_query=inp.query,
            intent=intent.primary_intent.value,
            sub_intent=intent.sub_intent.value if intent.sub_intent else None,
            requirement_keys=tuple(requirements),
            statement=statement,
            supporting_evidence=tuple(dict.fromkeys(support)),
            evidence_strength=strength,
            limitations=tuple(dict.fromkeys(lims)),
            analysis_method=METHOD,
            detected_at=inp.now,
            verification_status=verification,
            observations=dict(observations or {}),
        )

    # ---------------------------------------------------------------- abstention
    reasons: list[str] = []
    if inp.results.status is RetrievalStatus.UNAVAILABLE:
        reasons.append("search results could not be retrieved")
    elif n < g.min_inspected:
        reasons.append(f"only {n} results were retrieved (at least {g.min_inspected} needed)")
    primary = inp.entities.primary
    for ref in inp.entities.refs:
        if ref.state in ("unknown", "rumored") and _older_than_confirmed(ref, inp.entities):
            continue  # an earlier version of a confirmed product (e.g. "X 2" next to "X 3")
        if ref.state == "removed":
            reasons.append(
                f"a credible record states that '{ref.name}' was removed or vaulted; a guide "
                "for it is not proposed as a content opportunity"
            )
        elif ref.state in ("unknown", "rumored", "rejected") and (
            ref.version is not None or primary is None or primary.ref.name == ref.name
        ):
            reasons.append(
                f"the query's subject '{ref.name}' is {ref.state}: no credible record "
                "establishes it, and a gap is not claimed for something that may not exist"
            )
        elif ref.state == "ambiguous":
            reasons.append(
                f"'{ref.name}' is ambiguous ({', '.join(ref.candidates)}); evidence that "
                "identifies the intended entity is needed"
            )
        elif ref.state != "confirmed" and any(r.blocking for r in inp.decomposition.requirements):
            reasons.append(
                f"'{ref.name}' is {ref.state}, not confirmed by a credible source; no location, "
                "mechanic or detail is proposed for it"
            )
    if intent.confidence.value == "low" and intent.specificity.value == "broad":
        reasons.append("the query's intent is too unclear to define what viewers need")
    if reasons:
        return [
            gap(
                GapType.INSUFFICIENT_EVIDENCE,
                "The available evidence cannot support a reliable content-gap conclusion: "
                + "; ".join(reasons)
                + ".",
                EvidenceStrength.WEAK,
                reasons,
                observations={"inspected": n},
            )
        ]
    rel = [r for r in inp.relevance if r.relevant]
    fresh = {f.video_id: f for f in inp.freshness}
    req_by_key = {r.key: r for r in inp.decomposition.requirements}
    summaries = {s.requirement_key: s for s in inp.requirements}
    essential = [k for k, r in req_by_key.items() if r.priority is RequirementPriority.ESSENTIAL]

    def answers_need(r: RelevanceAssessment) -> bool:
        if not essential:
            return True
        hit = 0
        for k in essential:
            s = summaries.get(k)
            if s and (r.video_id in s.covered_by or r.video_id in s.partially_by):
                hit += 1
        return hit >= max(1, len(essential) // 2)

    adequate = [
        r
        for r in rel
        if r.intent_match
        and r.entity_match in (EntityMatch.EXACT, EntityMatch.NOT_APPLICABLE)
        and fresh.get(r.video_id) is not None
        and fresh[r.video_id].status in (FreshnessStatus.CURRENT, FreshnessStatus.UNKNOWN)
        and answers_need(r)
    ]
    obs: dict[str, float | int | str | None] = {
        "inspected": n,
        "relevant": len(rel),
        "adequate": len(adequate),
        "highly_relevant": inp.supply.highly_relevant,
    }
    if len(adequate) >= g.adequate_relevant_current:
        return [
            gap(
                GapType.NO_VERIFIABLE_GAP,
                f"Of the {n} retrieved results inspected, {len(adequate)} appear relevant, "
                "current and aimed at this need; no meaningful gap is established.",
                EvidenceStrength.MODERATE,
                ["adequacy is judged from metadata; quality was not assessed"],
                videos=[r.video_id for r in adequate],
                observations=obs,
            )
        ]
    gaps: list[ContentGap] = []
    facts = inp.entities.primary
    if not rel:
        strong = n >= 2 * g.min_inspected and facts is not None and facts.ref.state == "confirmed"
        gaps.append(
            gap(
                GapType.NO_RELEVANT_RESULTS,
                f"None of the {n} retrieved results inspected appears materially relevant to "
                f"the {intent.primary_intent.value} intent.",
                EvidenceStrength.STRONG if strong else EvidenceStrength.MODERATE,
                ["other searches, regions or phrasings may return relevant videos"],
                observations=obs,
            )
        )
    # Version mismatch: the family is covered, the verified current version is not.
    if facts is not None and facts.versioned.version and facts.ref.state == "confirmed":
        family = [
            r
            for r in inp.relevance
            if r.entity_match
            in (EntityMatch.EXACT, EntityMatch.FAMILY, EntityMatch.VERSION_MISMATCH)
        ]
        older = [r for r in family if r.entity_match is EntityMatch.VERSION_MISMATCH]
        current = [r for r in rel if r.entity_match is EntityMatch.EXACT and r.intent_match]
        if (
            family
            and len(older) / len(family) >= g.version_mismatch_share
            and len(current) <= g.max_current_version_results_for_gap
        ):
            breaking = any(c.breaking for c in facts.changes)
            gaps.append(
                gap(
                    GapType.VERSION_MISMATCH,
                    f"Of {len(family)} inspected results about {facts.versioned.family_text}, "
                    f"{len(older)} concern other versions; {len(current)} match "
                    f"{facts.ref.name} and the intent.",
                    EvidenceStrength.STRONG if breaking else EvidenceStrength.MODERATE,
                    [] if breaking else ["no verified breaking change links the versions"],
                    videos=[r.video_id for r in older + current],
                    observations={
                        **obs,
                        "family": len(family),
                        "older_version": len(older),
                        "current_version": len(current),
                    },
                )
            )
    # Potentially outdated after a verified change.
    if facts is not None and intent.time_sensitive and rel:
        change = facts.latest_change
        ref_date = (change.when if change else None) or facts.release_date
        window = config.freshness.recent_window.get(intent.category or "", timedelta(days=60))
        if ref_date is not None and (inp.now.date() - ref_date).days <= window.days:
            stale = [
                r
                for r in rel
                if fresh.get(r.video_id)
                and fresh[r.video_id].status
                in (FreshnessStatus.POTENTIALLY_OUTDATED, FreshnessStatus.OUTDATED_WITH_EVIDENCE)
            ]
            evidenced = [
                r
                for r in stale
                if fresh[r.video_id].status is FreshnessStatus.OUTDATED_WITH_EVIDENCE
            ]
            if len(stale) / len(rel) >= g.outdated_share:
                gaps.append(
                    gap(
                        GapType.POTENTIALLY_OUTDATED,
                        f"{len(stale)} of {len(rel)} relevant inspected results predate or "
                        f"concern an earlier state than the verified change on "
                        f"{ref_date.isoformat()} ({len(evidenced)} with a verified change "
                        "that affects them).",
                        EvidenceStrength.STRONG
                        if len(evidenced) * 2 >= len(stale)
                        else EvidenceStrength.MODERATE,
                        ["being older does not show a video is inaccurate"],
                        videos=[r.video_id for r in stale],
                        observations={
                            **obs,
                            "potentially_outdated": len(stale),
                            "outdated_with_evidence": len(evidenced),
                        },
                    )
                )
    # Practical demonstration: relevant results are news/comparison/review only.
    exact_rel = [
        r for r in rel if r.entity_match in (EntityMatch.EXACT, EntityMatch.NOT_APPLICABLE)
    ]
    if intent.primary_intent in _DEMO_INTENTS and len(exact_rel) >= 2:
        demo = [r for r in exact_rel if r.video_format.value in g.demonstration_formats]
        if not demo:
            gaps.append(
                gap(
                    GapType.INSUFFICIENT_PRACTICAL_DEMONSTRATION,
                    f"The {len(exact_rel)} relevant inspected results about the topic are "
                    "announcements, comparisons or reviews by their titles; none appears to "
                    "be a tutorial, guide or demonstration.",
                    EvidenceStrength.MODERATE,
                    ["formats are inferred from titles"],
                    videos=[r.video_id for r in exact_rel],
                    observations={**obs, "demonstrations": 0},
                )
            )
    # Missing specific answers: essential needs not evidenced in any relevant result.
    missing = [
        k
        for k in essential
        if (s := summaries.get(k)) is not None
        and s.best_status in (CoverageStatus.NOT_EVIDENCED, CoverageStatus.CONTRADICTED)
    ]
    if missing and exact_rel:
        names = ", ".join(req_by_key[k].requirement for k in missing)
        tier_a = all(r.evidence_tier is EvidenceTier.A_METADATA for r in exact_rel)
        gaps.append(
            gap(
                GapType.MISSING_SPECIFIC_ANSWER,
                f"No title or description among {len(exact_rel)} relevant inspected results "
                f"mentions: {names}. Whether the videos cover it is not established."
                if tier_a
                else f"The inspected transcripts of {len(exact_rel)} relevant results do not "
                f"address: {names}.",
                EvidenceStrength.WEAK if tier_a else EvidenceStrength.MODERATE,
                ["absence from metadata is not absence from the video"] if tier_a else [],
                videos=[r.video_id for r in exact_rel],
                requirements=missing,
                observations={**obs, "missing_requirements": len(missing)},
            )
        )
    if rel and not inp.supply.highly_relevant:
        best = max(r.score for r in inp.relevance)
        if best < g.low_relevance_best_below:
            gaps.append(
                gap(
                    GapType.LOW_RELEVANCE,
                    f"{len(rel)} inspected results are loosely relevant, but none closely "
                    "matches the query's task.",
                    EvidenceStrength.MODERATE,
                    ["relevance is an uncalibrated metadata index"],
                    videos=[r.video_id for r in rel],
                    observations={**obs, "best_relevance": round(best, 1)},
                )
            )
    if inp.language and rel:
        known = [r for r in rel if r.language_compatible is not None]
        other = [r for r in known if r.language_compatible is False]
        if len(known) >= 3 and len(other) / len(known) >= g.language_gap_share:
            gaps.append(
                gap(
                    GapType.LANGUAGE_OR_REGION_GAP,
                    f"{len(other)} of {len(known)} relevant results with a reported language "
                    f"are not in {inp.language}.",
                    EvidenceStrength.MODERATE,
                    ["language metadata is optional and often missing"],
                    videos=[r.video_id for r in known],
                    observations={**obs, "other_language": len(other)},
                )
            )
    if intent.primary_intent in _DEMO_INTENTS and len(rel) >= 3:
        shorts = [r for r in rel if r.video_format.value == "short"]
        if len(shorts) / len(rel) >= g.format_gap_share:
            gaps.append(
                gap(
                    GapType.FORMAT_GAP,
                    f"{len(shorts)} of {len(rel)} relevant results are short-form (duration "
                    f"<= {g.short_form_seconds} s), which rarely suits a step-by-step need.",
                    EvidenceStrength.MODERATE,
                    ["duration is a proxy for format"],
                    videos=[r.video_id for r in rel],
                    observations={**obs, "short_form": len(shorts)},
                )
            )
    if not gaps:
        gaps.append(
            gap(
                GapType.NO_VERIFIABLE_GAP,
                f"Of the {n} retrieved results inspected, {len(rel)} appear relevant; the "
                "analysis does not establish a meaningful content gap.",
                EvidenceStrength.WEAK
                if len(rel) < g.adequate_relevant_current
                else EvidenceStrength.MODERATE,
                ["fewer adequate results than the threshold, but no specific gap was found"],
                observations=obs,
            )
        )
    return gaps


def primary_gap(gaps: Sequence[ContentGap]) -> ContentGap | None:
    if not gaps:
        return None
    return min(
        gaps,
        key=lambda g: (-STRENGTH_RANK[g.evidence_strength], _PRECEDENCE.index(g.gap_type)),
    )
