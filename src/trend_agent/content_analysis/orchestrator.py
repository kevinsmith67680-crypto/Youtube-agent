"""One content analysis, end to end, from prepared inputs to a verified report.

Order: policy check -> entities -> intent -> requirements -> bounded retrieval (canonical
query, one variant only if the first set is thin) -> evidence registration -> relevance ->
freshness -> coverage -> supply -> gaps -> optional LLM assist (suggestions only) ->
report claims -> application-side verification -> score -> report.

Deterministic checks run before any LLM call; at most one assist call (with bounded
retries) is made per analysis, and none in reduced-capability mode.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trend_agent.content_analysis import (
    content_gaps,
    coverage,
    freshness,
    opportunity_scoring,
    relevance,
    supply,
)
from trend_agent.content_analysis.config import ANALYSIS_VERSION, LoadedContentConfig
from trend_agent.content_analysis.entity_verification import QueryEntities, QueryEntityResolver
from trend_agent.content_analysis.evidence import observation_draft, video_draft
from trend_agent.content_analysis.intent_classifier import classify, merge_llm_suggestions
from trend_agent.content_analysis.llm_client import (
    AnalysisLLM,
    AssistRun,
    NullAnalysisLLM,
    build_prompt,
    run_assist,
)
from trend_agent.content_analysis.query_decomposition import decompose
from trend_agent.content_analysis.result_retrieval import ResultSource, make_variants
from trend_agent.content_analysis.verification import DraftClaim, verify_claims
from trend_agent.schemas.content_gap import (
    OPPORTUNITY_GAPS,
    ContentGap,
    EvidenceStrength,
    GapType,
    GapVerification,
    SupplySummary,
)
from trend_agent.schemas.content_opportunity import (
    DemandBasis,
    DemandEvidence,
    OpportunityReport,
    OpportunityStatus,
    ReportClaim,
    ReportClaimKind,
    ReportClaimStatus,
    VideoReference,
)
from trend_agent.schemas.search_intent import (
    IntentClassification,
    PrimaryIntent,
    QueryDecomposition,
    RequirementPriority,
    SubIntent,
)
from trend_agent.schemas.search_result import (
    TIER_RANK,
    CandidateVideo,
    CoverageCell,
    EntityMatch,
    EvidenceTier,
    FreshnessAssessment,
    RelevanceAssessment,
    RequirementSummary,
    RetrievalRequest,
    RetrievalStatus,
    RetrievedResultSet,
)
from trend_agent.schemas.verification import (
    AuthoritativeStatistics,
    EvidenceBundle,
    EvidenceDraft,
    EvidenceRecord,
)
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.config import VerificationConfig
from trend_agent.verification.entities import EntityCatalog, RecordView
from trend_agent.verification.registry import EvidenceRegistry

_ANGLE = {
    PrimaryIntent.TUTORIAL: "step-by-step tutorial",
    PrimaryIntent.PROBLEM_SOLVING: "troubleshooting guide",
    PrimaryIntent.COMPARISON: "comparison",
    PrimaryIntent.REVIEW: "hands-on review",
    PrimaryIntent.DEMONSTRATION: "working demonstration",
    PrimaryIntent.NEWS: "explainer of what changed",
    PrimaryIntent.INFORMATIONAL: "explainer",
    PrimaryIntent.GAMING_GUIDE: "guide",
    PrimaryIntent.ENTERTAINMENT: "entertainment video",
    PrimaryIntent.NAVIGATIONAL: "resource overview",
}
ITEM_SUB_INTENTS = frozenset(
    {SubIntent.ITEM_LOCATION, SubIntent.REWARD_UNLOCK, SubIntent.WEAPON_GUIDE}
)
_NEED = {
    PrimaryIntent.TUTORIAL: "step-by-step instructions",
    PrimaryIntent.PROBLEM_SOLVING: "a fix for a specific problem",
    PrimaryIntent.COMPARISON: "a side-by-side comparison",
    PrimaryIntent.REVIEW: "an evaluation of the product",
    PrimaryIntent.DEMONSTRATION: "to see it working",
    PrimaryIntent.NEWS: "what changed recently",
    PrimaryIntent.INFORMATIONAL: "an explanation",
    PrimaryIntent.GAMING_GUIDE: "help completing an in-game objective",
    PrimaryIntent.ENTERTAINMENT: "entertaining content",
    PrimaryIntent.NAVIGATIONAL: "a specific resource",
}


class Registrar(Protocol):
    """Application-side evidence registration (the analysis' only write path)."""

    registry: EvidenceRegistry

    def register(self, drafts: Sequence[EvidenceDraft]) -> list[EvidenceRecord]: ...

    def bundle(
        self, records: Sequence[EvidenceRecord], *, target_id: int | None, synthetic: bool
    ) -> EvidenceBundle: ...


class MemoryRegistrar:
    def __init__(self, registry: EvidenceRegistry) -> None:
        self.registry = registry

    def register(self, drafts: Sequence[EvidenceDraft]) -> list[EvidenceRecord]:
        return [self.registry.register(d) for d in drafts]

    def bundle(
        self, records: Sequence[EvidenceRecord], *, target_id: int | None, synthetic: bool
    ) -> EvidenceBundle:
        return self.registry.issue_bundle(records, target_id=target_id, synthetic=synthetic)


@dataclass(frozen=True)
class AnalysisInputs:
    query: str
    query_id: int | None
    category: str | None
    region: str | None
    language: str | None
    demand: DemandEvidence
    stats: AuthoritativeStatistics
    context_records: Sequence[EvidenceRecord]
    catalog: EntityCatalog
    now: datetime
    topic_cluster_id: int | None = None
    target_id: int | None = None
    synthetic: bool = False
    published_after: datetime | None = None


@dataclass
class AnalysisCost:
    search_requests: int = 0
    quota_units: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_usd: float = 0.0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {k: round(v, 6) if isinstance(v, float) else v for k, v in self.__dict__.items()}


@dataclass
class AnalysisOutcome:
    run_id: str
    report: OpportunityReport
    intent: IntentClassification
    decomposition: QueryDecomposition
    results: RetrievedResultSet
    videos: list[CandidateVideo]
    relevance: list[RelevanceAssessment]
    freshness: list[FreshnessAssessment]
    cells: list[CoverageCell]
    summaries: list[RequirementSummary]
    gaps: list[ContentGap]
    bundle: EvidenceBundle | None
    fingerprint: str
    expires_at: datetime | None
    cost: AnalysisCost
    llm: AssistRun | None = None
    registered: list[EvidenceRecord] = field(default_factory=list)


def inputs_fingerprint(inp: AnalysisInputs, config_tag: str) -> str:
    payload = {
        "v": ANALYSIS_VERSION,
        "c": config_tag,
        "q": inp.query.casefold().strip(),
        "r": inp.region,
        "l": inp.language,
        "d": [inp.demand.basis.value, inp.demand.surge_event_id, inp.demand.statement],
        "e": sorted(r.content_hash for r in inp.context_records),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ContentAnalyzer:
    def __init__(
        self,
        loaded: LoadedContentConfig,
        registrar: Registrar,
        *,
        verification_config: VerificationConfig,
        source: ResultSource,
        llm: AnalysisLLM | None = None,
        semantic: relevance.SemanticScorer | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.registrar = registrar
        self.vconfig = verification_config
        self.source = source
        self.llm = llm or NullAnalysisLLM()
        self.semantic = semantic
        self._clock = clock

    def _understand(
        self, inp: AnalysisInputs
    ) -> tuple[dict[str, RecordView], QueryEntities, IntentClassification, QueryDecomposition]:
        cfg = self.config
        reg = self.registrar.registry
        ctx_res = reg.resolve(
            [r.evidence_id for r in inp.context_records],
            reg.issue_bundle(inp.context_records, target_id=inp.target_id, synthetic=inp.synthetic),
            allow_synthetic=inp.synthetic,
            now=inp.now,
        )
        views = {
            eid: RecordView.build(r, inp.catalog)
            for eid, r in ctx_res.records.items()
            if r.published_at is None or r.published_at <= inp.now  # nothing from the future
        }
        resolver = QueryEntityResolver(inp.catalog, views, now=inp.now, category=inp.category)
        entities = resolver.resolve(inp.query)
        intent = classify(inp.query, entities, cfg, category=inp.category)
        if intent.sub_intent in ITEM_SUB_INTENTS:
            resolver.resolve_item(inp.query, entities)
            intent = classify(inp.query, entities, cfg, category=inp.category)
        return views, entities, intent, decompose(intent, entities)

    def understand(
        self, inp: AnalysisInputs
    ) -> tuple[QueryEntities, IntentClassification, QueryDecomposition]:
        """Entities, intent and information requirements only (no retrieval, no LLM)."""
        _, entities, intent, decomposition = self._understand(inp)
        return entities, intent, decomposition

    async def analyse(self, inp: AnalysisInputs) -> AnalysisOutcome:
        started = time.perf_counter()
        cfg = self.config
        cost = AnalysisCost()
        run_id = str(uuid.uuid4())
        allow_syn = inp.synthetic
        reg = self.registrar.registry
        views, entities, intent, decomposition = self._understand(inp)
        if intent.policy_flags:
            return self._blocked(run_id, inp, intent, decomposition, entities, cost, started)
        # ---- bounded retrieval: canonical query first, a variant only if thin
        request = RetrievalRequest(
            query=inp.query,
            region=inp.region,
            language=inp.language,
            published_after=inp.published_after,
            max_results_per_variant=cfg.retrieval.max_results_per_variant,
        )
        results = await self.source.retrieve(request, now=inp.now)
        videos = [v for v in results.videos if v.published_at is None or v.published_at <= inp.now]
        variants = make_variants(inp.query, intent, entities, cfg.retrieval.max_variants)
        if variants and results.status is not RetrievalStatus.UNAVAILABLE:
            quick = [
                relevance.assess(v, inp.query, intent, entities, cfg, catalog=inp.catalog)
                for v in videos
            ]
            if sum(r.relevant for r in quick) < cfg.retrieval.expand_when_relevant_below:
                more = await self.source.retrieve(
                    request.model_copy(update={"query": variants[0]}), now=inp.now
                )
                seen = {v.video_id for v in videos}
                videos += [
                    v
                    for v in more.videos
                    if v.video_id not in seen
                    and (v.published_at is None or v.published_at <= inp.now)
                ]
                results = results.model_copy(
                    update={
                        "request": request.model_copy(update={"variants": (variants[0],)}),
                        "requests_made": results.requests_made + more.requests_made,
                        "quota_units": results.quota_units + more.quota_units,
                        "cache_hits": results.cache_hits + more.cache_hits,
                        "warnings": results.warnings + more.warnings,
                        "status": results.status
                        if more.status is not RetrievalStatus.UNAVAILABLE
                        else RetrievalStatus.PARTIAL,
                    }
                )
        cost.search_requests = results.requests_made
        cost.quota_units = results.quota_units
        cost.cache_hits = results.cache_hits
        # ---- register video evidence (application principal)
        video_records = self.registrar.register([video_draft(v) for v in videos])
        videos = [
            v.model_copy(update={"evidence_id": r.evidence_id})
            for v, r in zip(videos, video_records, strict=True)
        ]
        video_ev = {v.video_id: v.evidence_id for v in videos if v.evidence_id}
        for r in video_records:
            views[r.evidence_id] = RecordView.build(r, inp.catalog)
        facts = entities.primary
        rels = [
            relevance.assess(
                v, inp.query, intent, entities, cfg, catalog=inp.catalog,
                language=inp.language, semantic=self.semantic,
            )
            for v in videos
        ]  # fmt: skip
        fresh = [
            freshness.assess(v, r, intent, facts, cfg, now=inp.now)
            for v, r in zip(videos, rels, strict=True)
        ]
        denied = [f.ref.name for f in entities.facts if f.denied or f.ref.state == "rejected"]
        decomposition, cells, summaries, gaps, supply_summary, calc_records = self._evaluate(
            inp, intent, decomposition, entities, results, videos, rels, fresh, video_ev, denied,
        )  # fmt: skip
        # ---- optional LLM assist (suggestions only)
        assist: AssistRun | None = None
        narrative: list[DraftClaim] = []
        if not isinstance(self.llm, NullAnalysisLLM) and self.config.llm.enabled and videos:
            assist = self._assist(inp, intent, decomposition, entities, videos)
            cost.llm_calls = assist.attempts
            cost.llm_input_tokens = assist.usage.input_tokens
            cost.llm_output_tokens = assist.usage.output_tokens
            cost.llm_usd = (
                assist.usage.input_tokens * self.config.llm.input_usd_per_mtok
                + assist.usage.output_tokens * self.config.llm.output_usd_per_mtok
            ) / 1e6
            if assist.output is not None:
                out = assist.output
                intent = merge_llm_suggestions(
                    intent, [(s.primary_intent, s.sub_intent) for s in out.intent_suggestions]
                )
                if out.additional_requirements:
                    decomposition = decompose(
                        intent,
                        entities,
                        llm_requirements=[
                            (r.requirement, r.priority, tuple(r.cues))
                            for r in out.additional_requirements
                        ],
                    )
                    decomposition, cells, summaries, gaps, supply_summary, calc_records = (
                        self._evaluate(
                            inp, intent, decomposition, entities, results, videos, rels, fresh,
                            video_ev, denied,
                        )
                    )  # fmt: skip
                narrative = [
                    DraftClaim(ReportClaimKind.NARRATIVE, h.text, tuple(h.evidence_ids),
                               "LLM hypothesis, checked against the evidence")
                    for h in out.gap_hypotheses
                ]  # fmt: skip
                from trend_agent.verification.text import sentences

                narrative += [
                    DraftClaim(ReportClaimKind.NARRATIVE, s,
                               tuple(video_ev.values())[:8], "LLM summary sentence")
                    for s in sentences(out.summary)
                ]  # fmt: skip
        # ---- bundle, claims, verification
        records = list(inp.context_records) + video_records + calc_records
        bundle = self.registrar.bundle(records, target_id=inp.target_id, synthetic=allow_syn)
        for r in calc_records:
            views[r.evidence_id] = RecordView.build(r, inp.catalog)
        draft_claims = self._claims(entities, supply_summary, summaries, gaps, calc_records)
        draft_claims += narrative
        tiers = [v.tier for v in videos] or [EvidenceTier.D_INSUFFICIENT]
        max_tier = max(tiers, key=TIER_RANK.__getitem__)
        claims = verify_claims(
            draft_claims,
            registry=reg,
            bundle=bundle,
            views=views,
            catalog=inp.catalog,
            stats=inp.stats,
            query=inp.query,
            now=inp.now,
            verification_config=self.vconfig,
            max_tier=max_tier,
            allow_synthetic=allow_syn,
        )
        report = self._report(
            run_id, inp, intent, decomposition, entities, results, videos, rels, fresh,
            summaries, gaps, supply_summary, claims, bundle, assist,
        )  # fmt: skip
        expiries = [v.expires_at for v in videos if v.expires_at is not None]
        cost.seconds = time.perf_counter() - started
        return AnalysisOutcome(
            run_id=run_id,
            report=report,
            intent=intent,
            decomposition=decomposition,
            results=results,
            videos=videos,
            relevance=rels,
            freshness=fresh,
            cells=cells,
            summaries=summaries,
            gaps=gaps,
            bundle=bundle,
            fingerprint=self._fingerprint(inp, videos),
            expires_at=min(expiries) if expiries else None,
            cost=cost,
            llm=assist,
            registered=video_records + calc_records,
        )

    # ----------------------------------------------------------------- pieces
    def _evaluate(
        self,
        inp: AnalysisInputs,
        intent: IntentClassification,
        decomposition: QueryDecomposition,
        entities: QueryEntities,
        results: RetrievedResultSet,
        videos: list[CandidateVideo],
        rels: list[RelevanceAssessment],
        fresh: list[FreshnessAssessment],
        video_ev: dict[str, str],
        denied: list[str],
    ) -> tuple[
        QueryDecomposition,
        list[CoverageCell],
        list[RequirementSummary],
        list[ContentGap],
        SupplySummary,
        list[EvidenceRecord],
    ]:
        cfg = self.config
        by_rel = {r.video_id: r for r in rels}
        assessed = [
            v
            for v in videos
            if by_rel[v.video_id].relevant
            or by_rel[v.video_id].entity_match is EntityMatch.VERSION_MISMATCH
        ]
        cells = [
            coverage.cell(req, v, by_rel[v.video_id], denied_names=denied)
            for req in decomposition.requirements
            for v in assessed
        ]
        relevant_cells = [c for c in cells if by_rel[c.video_id].relevant]
        summaries = [
            coverage.summarise(req, relevant_cells, len(videos))
            for req in decomposition.requirements
        ]
        window = (
            cfg.freshness.recent_window.get(intent.category or "", None)
            or cfg.freshness.recent_window["ai"]
        )
        supply_summary = supply.summarise(
            results, videos, rels, fresh, now=inp.now, recent_window=window,
            intent_label=intent.primary_intent.value,
        )  # fmt: skip
        inputs = list(video_ev.values())
        expiry = min((v.expires_at for v in videos if v.expires_at), default=None)
        family = sum(
            r.entity_match in (EntityMatch.EXACT, EntityMatch.FAMILY, EntityMatch.VERSION_MISMATCH)
            for r in rels
        )
        outdated = sum(
            1
            for r, f in zip(rels, fresh, strict=True)
            if r.relevant and f.status.value in ("potentially_outdated", "outdated_with_evidence")
        )
        drafts = [
            observation_draft(
                "supply",
                {
                    "inspected": supply_summary.inspected,
                    "relevant": supply_summary.relevant,
                    "highly_relevant": supply_summary.highly_relevant,
                    "recent_relevant": supply_summary.recent_relevant,
                    "exact_entity": supply_summary.exact_entity,
                    "older_version": supply_summary.older_version,
                    "family": family,
                    "distinct_channels": supply_summary.distinct_channels,
                    "recent_window_days": window.days,
                    "potentially_outdated": outdated,
                },
                subject=inp.query,
                computed_at=inp.now,
                inputs=inputs,
                expires_at=expiry,
                synthetic=inp.synthetic,
            )
        ]
        for s in summaries:
            relevant_n = len(
                {c.video_id for c in relevant_cells if c.requirement_key == s.requirement_key}
            )
            drafts.append(
                observation_draft(
                    f"coverage:{s.requirement_key}",
                    {
                        "covered": len(s.covered_by),
                        "partially": len(s.partially_by),
                        "relevant_inspected": relevant_n,
                    },
                    subject=inp.query,
                    computed_at=inp.now,
                    inputs=inputs,
                    expires_at=expiry,
                    synthetic=inp.synthetic,
                )
            )
        calc_records = self.registrar.register(drafts)
        supply_id = calc_records[0].evidence_id
        entity_ev = list(entities.primary.ref.evidence_ids[:3]) if entities.primary else []
        gap_inputs = content_gaps.GapInputs(
            query=inp.query,
            intent=intent,
            decomposition=decomposition,
            entities=entities,
            results=results,
            relevance=rels,
            freshness=fresh,
            requirements=summaries,
            supply=supply_summary,
            language=inp.language,
            now=inp.now,
            video_evidence=video_ev,
            observation_evidence=[supply_id],
            entity_evidence=entity_ev,
        )
        gaps = content_gaps.detect(gap_inputs, cfg)
        # Each gap's numbers become a citable observation as well.
        gap_drafts = [
            observation_draft(
                f"gap:{g.gap_type.value}",
                {
                    **{k: v for k, v in g.observations.items() if isinstance(v, int | float)},
                    "min_inspected": cfg.gaps.min_inspected,
                    "short_form_seconds": cfg.gaps.short_form_seconds,
                },
                subject=inp.query,
                computed_at=inp.now,
                inputs=inputs,
                expires_at=expiry,
                synthetic=inp.synthetic,
            )
            for g in gaps
        ]
        gap_records = self.registrar.register(gap_drafts)
        gaps = [
            g.model_copy(update={"supporting_evidence": (*g.supporting_evidence, r.evidence_id)})
            for g, r in zip(gaps, gap_records, strict=True)
        ]
        return decomposition, cells, summaries, gaps, supply_summary, calc_records + gap_records

    def _assist(
        self,
        inp: AnalysisInputs,
        intent: IntentClassification,
        decomposition: QueryDecomposition,
        entities: QueryEntities,
        videos: list[CandidateVideo],
    ) -> AssistRun:
        system, prompt = build_prompt(
            query=inp.query,
            category=intent.category,
            entities=[
                {"name": r.name, "state": r.state, "version": r.version} for r in entities.refs
            ],
            demand_statement=inp.demand.statement,
            requirements=[
                {"key": r.key, "requirement": r.requirement, "priority": r.priority.value}
                for r in decomposition.requirements
            ],
            videos=videos[:25],
            limitations=["only titles and descriptions were inspected"]
            if all(v.tier is EvidenceTier.A_METADATA for v in videos)
            else [],
        )
        return run_assist(self.llm, system, prompt, max_attempts=self.config.llm.max_attempts)

    def _claims(
        self,
        entities: QueryEntities,
        supply_summary: SupplySummary,
        summaries: list[RequirementSummary],
        gaps: list[ContentGap],
        calcs: list[EvidenceRecord],
    ) -> list[DraftClaim]:
        by_key = {r.evidence_key.split(":", 2)[1]: r.evidence_id for r in calcs}
        out: list[DraftClaim] = []
        for f in entities.facts:
            ids = f.ref.evidence_ids
            if f.ref.state == "confirmed" and f.release_date is not None and ids:
                out.append(
                    DraftClaim(
                        ReportClaimKind.FACT,
                        f"{f.ref.name} was released on {f.release_date.strftime('%B')} "
                        f"{f.release_date.day}, {f.release_date.year}.",
                        ids,
                    )
                )
            label = {
                "reported": "reported only by lower-trust sources; not confirmed",
                "rumored": "a single lower-trust source; a rumour",
                "unknown": "not found in the evidence",
                "ambiguous": "ambiguous between several entities",
                "rejected": "denied or rejected by the evidence",
            }.get(f.ref.state)
            if label:
                out.append(
                    DraftClaim(
                        ReportClaimKind.OBSERVATION,
                        f"'{f.ref.name}' is {label}.",
                        ids or (by_key.get("supply", ""),),
                        "entity verification",
                    )
                )
        supply_id = by_key.get("supply")
        if supply_id:
            out += [
                DraftClaim(ReportClaimKind.OBSERVATION, s, (supply_id,))
                for s in supply_summary.statements
            ]
        for s in summaries:
            cid = by_key.get(f"coverage:{s.requirement_key}".split(":", 1)[0])
            calc = next(
                (
                    r.evidence_id
                    for r in calcs
                    if r.evidence_key.startswith(f"calc:coverage:{s.requirement_key}:")
                ),
                None,
            )
            if calc or cid:
                out.append(
                    DraftClaim(
                        ReportClaimKind.OBSERVATION,
                        s.statement,
                        (calc or cid or "",),
                        "metadata-based" if s.max_tier is EvidenceTier.A_METADATA else None,
                    )
                )
        for g in gaps:
            out.append(
                DraftClaim(
                    ReportClaimKind.HYPOTHESIS
                    if g.gap_type in OPPORTUNITY_GAPS
                    else ReportClaimKind.OBSERVATION,
                    g.statement,
                    g.supporting_evidence,
                    f"{g.verification_status.value} ({g.evidence_strength.value} evidence)",
                )
            )
        return out

    def _fingerprint(self, inp: AnalysisInputs, videos: Sequence[CandidateVideo]) -> str:
        payload = {
            "inputs": inputs_fingerprint(inp, self.loaded.version_tag),
            "videos": sorted((v.video_id, v.collected_at.isoformat()) for v in videos),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _report(
        self,
        run_id: str,
        inp: AnalysisInputs,
        intent: IntentClassification,
        decomposition: QueryDecomposition,
        entities: QueryEntities,
        results: RetrievedResultSet,
        videos: list[CandidateVideo],
        rels: list[RelevanceAssessment],
        fresh: list[FreshnessAssessment],
        summaries: list[RequirementSummary],
        gaps: list[ContentGap],
        supply_summary: SupplySummary,
        claims: list[ReportClaim],
        bundle: EvidenceBundle,
        assist: AssistRun | None,
    ) -> OpportunityReport:
        cfg = self.config
        primary = content_gaps.primary_gap(gaps)
        relevant_ids = [r.video_id for r in rels if r.relevant]
        by_fresh = {f.video_id: f for f in fresh}
        adequate = next(
            (
                int(g.observations.get("adequate") or 0)
                for g in gaps
                if "adequate" in g.observations
            ),
            0,
        )
        published = [c for c in claims if c.published]
        verified_share = (
            sum(c.status is ReportClaimStatus.VERIFIED for c in published) / len(published)
            if published
            else None
        )
        facts = entities.primary
        quality = opportunity_scoring.evidence_quality(
            supply_summary,
            [v.tier for v in videos],
            facts.ref.state if facts else None,
            verified_share,
            cfg.gaps.min_inspected,
        )
        score = opportunity_scoring.score(
            demand=inp.demand,
            intent=intent,
            primary_gap=primary,
            supply=supply_summary,
            freshness=fresh,
            relevant_ids=relevant_ids,
            adequate=adequate,
            quality=quality,
            config=cfg,
        )
        if primary is None or primary.gap_type is GapType.INSUFFICIENT_EVIDENCE:
            status = OpportunityStatus.INSUFFICIENT_EVIDENCE
        elif primary.gap_type is GapType.NO_VERIFIABLE_GAP:
            status = OpportunityStatus.NO_OPPORTUNITY
        else:
            status = OpportunityStatus.CANDIDATE
        contradicted = any(
            c.status is ReportClaimStatus.CONTRADICTED and c.kind is ReportClaimKind.FACT
            for c in claims
        )
        if results.status is RetrievalStatus.UNAVAILABLE:
            state = "source_unavailable"
        elif status is OpportunityStatus.INSUFFICIENT_EVIDENCE:
            state = "insufficient_evidence"
        elif contradicted:
            state = "contradicted"
        elif (
            published
            and all(c.status is ReportClaimStatus.VERIFIED for c in published)
            and not [c for c in claims if not c.published]
            and primary is not None
            and primary.verification_status.value == "verified"
            and inp.demand.basis is DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE
        ):
            state = "verified_result"
        else:
            state = "partially_verified"
        by_id = {v.video_id: v for v in videos}
        refs = [
            VideoReference(
                video_id=r.video_id,
                url=by_id[r.video_id].url,
                channel_title=by_id[r.video_id].channel_title,
                published_at=by_id[r.video_id].published_at,
                relevance_score=r.score,
                entity_match=r.entity_match.value,
                video_format=r.video_format.value,
                freshness=by_fresh[r.video_id].status.value,
                evidence_tier=r.evidence_tier.value,
                evidence_id=by_id[r.video_id].evidence_id,
            )
            for r in sorted(rels, key=lambda x: -x.score)
            if r.relevant or r.entity_match is EntityMatch.VERSION_MISMATCH
        ][:15]
        limitations = list(dict.fromkeys(lim for g in gaps for lim in g.limitations))
        if all(v.tier is EvidenceTier.A_METADATA for v in videos) and videos:
            limitations.insert(
                0, "metadata only: titles and descriptions were inspected, not videos"
            )
        limitations.append("API result order is request-specific, not a YouTube search ranking")
        limitations += [f"retrieval: {w[:200]}" for w in results.warnings[:3]]
        essential = [
            r.requirement
            for r in decomposition.requirements
            if r.priority is RequirementPriority.ESSENTIAL
        ]
        entity_name = facts.ref.name if facts else None
        opp = [g for g in gaps if g.gap_type in OPPORTUNITY_GAPS]
        evidence_needed = []
        if all(v.tier is EvidenceTier.A_METADATA for v in videos):
            evidence_needed.append(
                "authorised captions/transcripts would show what videos actually cover"
            )
        if inp.demand.basis not in (
            DemandBasis.CONFIRMED_DIRECT_YOUTUBE_SEARCH_SURGE,
            DemandBasis.DIRECT_SEARCH_CANDIDATE,
        ):
            evidence_needed.append("direct YouTube search-interest measurement for the query")
        for ref in entities.refs:
            if ref.state not in ("confirmed",):
                evidence_needed.append(f"an official source for '{ref.name}'")
        if primary is not None and primary.gap_type is GapType.INSUFFICIENT_EVIDENCE:
            evidence_needed.extend(primary.limitations[:3])
        explanation = {
            "audience_wants": (
                f"Viewers searching “{inp.query}” appear to want {_NEED[intent.primary_intent]}"
                + (f" about {entity_name}" if entity_name else "")
                + (f", in particular: {'; '.join(essential[:4])}." if essential else ".")
            ),
            "existing_content": supply_summary.scope,
            "possibly_missing": " ".join(g.statement for g in opp)
            or "No content gap is established.",
            "why_investigate": (
                f"{inp.demand.statement} {primary.statement}"
                if status is OpportunityStatus.CANDIDATE and primary is not None
                else "Not recommended for investigation: "
                + (primary.statement if primary is not None else "no conclusion")
            ),
            "evidence_needed": "; ".join(dict.fromkeys(evidence_needed)) or "none identified",
        }
        angle = None
        if status is OpportunityStatus.CANDIDATE and primary is not None:
            missing = [
                s.requirement for s in summaries if s.requirement_key in primary.requirement_keys
            ]
            focus = missing[:3] or essential[:3]
            subject = (
                (entities.item.ref.name if entities.item else None) or entity_name or inp.query
            )
            angle = (
                f"An up-to-date {_ANGLE[intent.primary_intent]} for {subject}"
                + (f", focused on: {'; '.join(focus)}" if focus else "")
                + "."
            )
        uncertainty = (
            primary.limitations[0]
            if primary is not None and primary.limitations
            else (limitations[0] if limitations else "none recorded")
        )
        if intent.ambiguity:
            uncertainty = intent.ambiguity[0]
        sources = sorted(
            {e for c in claims if c.published for e in c.evidence_ids}
            | {e for g in gaps for e in g.supporting_evidence}
        )
        return OpportunityReport(
            opportunity_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"opp:{inp.query_id}:{inp.region}:{inp.language}:{inp.query.casefold()}",
                )
            ),
            analysis_run_id=run_id,
            query_id=inp.query_id,
            canonical_query=inp.query,
            category=intent.category,
            topic_cluster_id=inp.topic_cluster_id,
            entity=entity_name,
            entity_key=facts.ref.entity_key if facts else None,
            intent=intent,
            demand=inp.demand,
            analysed_at=inp.now,
            region=inp.region,
            language=inp.language,
            inspected_results=len(videos),
            coverage_limitations=tuple(limitations),
            requirements=decomposition.requirements,
            requirement_coverage=tuple(summaries),
            relevant_videos=tuple(refs),
            gaps=tuple(gaps),
            primary_gap=primary,
            supply=supply_summary,
            score=score,
            evidence_quality=quality[1] if videos else None,
            status=status,
            verification_state=state,
            main_uncertainty=uncertainty,
            claims=tuple(claims),
            source_references=tuple(sources),
            explanation=explanation,
            suggested_content_angle=angle,
            relevance=tuple(rels),
            freshness=tuple(fresh),
            analysis_version=ANALYSIS_VERSION,
            configuration_version=self.loaded.version_tag,
            llm_model=assist.model if assist and assist.output else None,
            synthetic=inp.synthetic or inp.demand.synthetic,
        )

    def _blocked(
        self,
        run_id: str,
        inp: AnalysisInputs,
        intent: IntentClassification,
        decomposition: QueryDecomposition,
        entities: QueryEntities,
        cost: AnalysisCost,
        started: float,
    ) -> AnalysisOutcome:
        empty = RetrievedResultSet(
            request=RetrievalRequest(query=inp.query),
            status=RetrievalStatus.UNAVAILABLE,
            videos=(),
            provider="none",
            collected_at=None,
            warnings=("not retrieved: the query matches the content policy",),
        )
        supply_summary = supply.summarise(
            empty, [], [], [], now=inp.now, recent_window=self.config.freshness.recent_window["ai"],
            intent_label=intent.primary_intent.value,
        )  # fmt: skip
        gap = ContentGap(
            gap_type=GapType.INSUFFICIENT_EVIDENCE,
            target_query=inp.query,
            intent=intent.primary_intent.value,
            sub_intent=None,
            statement="Not analysed: cheats, exploits, anti-cheat evasion and account "
            "compromise are never treated as content opportunities.",
            supporting_evidence=(),
            evidence_strength=EvidenceStrength.WEAK,
            limitations=("content policy",),
            analysis_method=content_gaps.METHOD,
            detected_at=inp.now,
            verification_status=GapVerification.VERIFIED,
        )
        bundle = self.registrar.bundle(
            list(inp.context_records), target_id=inp.target_id, synthetic=inp.synthetic
        )
        report = self._report(
            run_id, inp, intent, decomposition, entities, empty, [], [], [], [], [gap],
            supply_summary, [], bundle, None,
        ).model_copy(update={"status": OpportunityStatus.BLOCKED_POLICY,
                             "verification_state": "insufficient_evidence"})  # fmt: skip
        cost.seconds = time.perf_counter() - started
        return AnalysisOutcome(
            run_id=run_id, report=report, intent=intent, decomposition=decomposition,
            results=empty, videos=[], relevance=[], freshness=[], cells=[], summaries=[],
            gaps=[gap], bundle=bundle, fingerprint=self._fingerprint(inp, []), expires_at=None,
            cost=cost,
        )  # fmt: skip
