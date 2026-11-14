"""The bounded recommendation workflow for one verified opportunity.

1 validate Phase 4-6 inputs -> 2 build the constrained context (creator profile included)
-> 3 select formats -> 4 generate concepts (template or LLM; an unavailable or failing LLM
falls back to the deterministic template generator, recorded) -> 5 validate structure ->
6 verify (Phase 7 rules + the verification gate) -> 7 intent alignment and 8 feasibility ->
9 score. Deduplication, diversity and final ranking run across opportunities
(``diversity.select``); persistence and caching live in the service.

Recommendation IDs are UUIDv5 of (opportunity, format, creator profile, input fingerprint):
unchanged evidence regenerates the same IDs (idempotent), changed evidence new ones.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trend_agent.recommendations import feasibility as feas_mod
from trend_agent.recommendations.config import RECOMMENDATION_VERSION, LoadedRecommendationConfig
from trend_agent.recommendations.context_builder import (
    EvidenceContext,
    RecommendationContext,
    build_context,
    validate_inputs,
)
from trend_agent.recommendations.format_selector import (
    CATALOG,
    RankedFormat,
    as_choice,
    select_formats,
)
from trend_agent.recommendations.idea_generator import (
    ConceptDraft,
    ConceptGenerator,
    GenerationOutcome,
    TemplateConceptGenerator,
    Usage,
)
from trend_agent.recommendations.ranking import rank
from trend_agent.recommendations.verification import verify_concept
from trend_agent.schemas.content_opportunity import OpportunityReport
from trend_agent.schemas.forecast import ForecastOutput
from trend_agent.schemas.recommendation import (
    CreatorProfile,
    EvidenceRef,
    RecommendationOutput,
    RecommendationStatus,
)

_NS = uuid.UUID("7a1c0d6e-4b2f-4f0e-9e57-0c7d0e7a7a07")


@dataclass
class OpportunityOutcome:
    opportunity_id: str
    query: str
    category: str | None
    recommendations: list[RecommendationOutput]
    skipped: tuple[str, ...] = ()
    generator: str = ""
    generation_status: str = ""
    usage: Usage = field(default_factory=Usage)
    violations: list[str] = field(default_factory=list)
    fingerprint: str = ""
    fallback: bool = False
    seconds: float = 0.0
    synthetic: bool = False

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {s.value: 0 for s in RecommendationStatus}
        for r in self.recommendations:
            c[r.verification.status.value] += 1
        return c


def recommendation_id(opportunity_id: str, fmt: str, profile_id: str, fingerprint: str) -> str:
    return str(uuid.uuid5(_NS, f"{opportunity_id}|{fmt}|{profile_id}|{fingerprint}"))


def cost_usd(usage: Usage, loaded: LoadedRecommendationConfig) -> float | None:
    llm = loaded.config.llm
    if llm.usd_per_million_input_tokens is None or llm.usd_per_million_output_tokens is None:
        return None
    return round(
        usage.input_tokens / 1e6 * llm.usd_per_million_input_tokens
        + usage.output_tokens / 1e6 * llm.usd_per_million_output_tokens,
        6,
    )


class RecommendationEngine:
    def __init__(
        self,
        loaded: LoadedRecommendationConfig,
        *,
        generator: ConceptGenerator | None = None,
    ) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.template = TemplateConceptGenerator(
            hooks_per_concept=self.cfg.generation.hooks_per_concept
        )
        self.generator: ConceptGenerator = generator if generator is not None else self.template

    def context(
        self,
        report: OpportunityReport,
        forecasts: Sequence[ForecastOutput],
        evidence: EvidenceContext,
        profile: CreatorProfile,
    ) -> tuple[RecommendationContext | None, tuple[str, ...]]:
        v = validate_inputs(report, forecasts, now=evidence.now, loaded=self.loaded)
        if not v.eligible:
            return None, v.problems
        excl = [
            e
            for e in profile.content_exclusions
            if e.strip() and e.lower() in report.canonical_query.lower()
        ]
        if excl:
            return None, (f"excluded by the creator profile: {', '.join(excl)}",)
        if (
            report.category
            and profile.preferred_categories
            and (report.category not in profile.preferred_categories)
        ):
            return None, (f"category '{report.category}' is not in the creator profile",)
        return build_context(report, forecasts, evidence, profile=profile, validation=v), ()

    def formats(self, ctx: RecommendationContext) -> list[RankedFormat]:
        n = 1 if ctx.research_only else self.cfg.generation.max_concepts_per_opportunity
        return select_formats(
            category=ctx.category,
            primary_intent=ctx.primary_intent,
            sub_intent=ctx.sub_intent,
            gap_type=ctx.gap_type,
            preferred=frozenset(f.value for f in ctx.profile.preferred_formats),
            has_second_entity=ctx.other_entity is not None,
            limit=n,
        )

    def recommend(
        self,
        report: OpportunityReport,
        forecasts: Sequence[ForecastOutput],
        evidence: EvidenceContext,
        profile: CreatorProfile,
    ) -> OpportunityOutcome:
        t0 = time.perf_counter()
        ctx, problems = self.context(report, forecasts, evidence, profile)
        out = OpportunityOutcome(
            report.opportunity_id,
            report.canonical_query,
            report.category,
            [],
            skipped=problems,
            synthetic=report.synthetic,
        )
        if ctx is None:
            out.seconds = time.perf_counter() - t0
            return out
        formats = self.formats(ctx)
        if not formats:
            out.skipped = ("no video format serves this search intent and category",)
            out.seconds = time.perf_counter() - t0
            return out
        fp = ctx.fingerprint(self.generator.name, self.loaded.version_tag)
        out.fingerprint = fp
        gen: GenerationOutcome = self.generator.generate(ctx, formats, len(formats))
        out.usage.add(gen.usage)
        out.violations += gen.violations
        if (
            gen.status not in ("ok", "insufficient_evidence")
            and self.generator.name != self.template.name
        ):
            out.fallback = True
            out.violations.append(
                f"generator {gen.generator} {gen.status}: {gen.detail}; "
                "used the deterministic template generator"
            )
            gen = self.template.generate(ctx, formats, len(formats))
        out.generator, out.generation_status = gen.generator, gen.status
        for i, draft in enumerate(gen.concepts):
            out.recommendations.append(self._assemble(i, draft, ctx, evidence, formats, gen, fp))
        out.seconds = time.perf_counter() - t0
        return out

    def _assemble(
        self,
        index: int,
        draft: ConceptDraft,
        ctx: RecommendationContext,
        evidence: EvidenceContext,
        formats: Sequence[RankedFormat],
        gen: GenerationOutcome,
        fingerprint: str,
    ) -> RecommendationOutput:
        spec = CATALOG[draft.video_format]
        vc = verify_concept(draft, ctx, evidence, self.loaded, generator=gen.generator)
        status = vc.verification.status
        feas = feas_mod.evaluate(spec, ctx, self.loaded)
        title = (
            f"Research candidate: {ctx.query}"
            if ctx.research_only or vc.title is None
            else vc.title
        )
        coverage = next((t.query_term_coverage for t in vc.titles if t.text == vc.title), 0.0)
        ranking = rank(ctx, spec.fmt, feas, status, coverage, self.loaded)
        cited = (
            set(vc.evidence_ids)
            | {e for s in vc.outline.sections for i in s.items for e in i.evidence_ids}
            | set(ctx.demand.evidence_ids)
        )
        refs: tuple[EvidenceRef, ...] = tuple(r for r in ctx.evidence if r.evidence_id in cited)
        chosen = next((r for r in formats if r.spec.fmt is spec.fmt), None)
        rid = recommendation_id(
            ctx.opportunity_id, spec.fmt.value, ctx.profile.profile_id, fingerprint
        )
        uncertainties = list(dict.fromkeys([*draft.uncertainties, *ctx.limitations]))
        return RecommendationOutput(
            recommendation_id=rid,
            opportunity_id=ctx.opportunity_id,
            concept_index=index,
            category=ctx.category,
            region=ctx.region,
            language=ctx.language,
            primary_query=ctx.query,
            query_id=ctx.query_id,
            topic_cluster_id=ctx.topic_cluster_id,
            target_intent=ctx.primary_intent,
            target_sub_intent=ctx.sub_intent,
            title=title,
            alternative_titles=tuple(vc.titles),
            hooks=tuple(vc.hooks),
            video_format=spec.fmt,
            format_label=spec.label,
            format_rationale=chosen.rationale if chosen else "selected by the generator",
            alternative_approaches=tuple(
                as_choice(r) for r in formats if r.spec.fmt is not spec.fmt
            ),
            target_audience=draft.target_audience,
            core_viewer_problem=draft.core_viewer_problem,
            premise=vc.premise,
            why_it_may_satisfy_demand=vc.why,
            key_information_requirements=tuple(r.text for r in ctx.requirements),
            key_questions=tuple(draft.key_questions),
            outline=vc.outline,
            evidence=refs,
            competing_videos=ctx.competing_videos,
            uncertainties=tuple(uncertainties[:14]),
            production=feas,
            ranking=ranking,
            verification=vc.verification,
            demand=ctx.demand,
            forecast=ctx.forecast,
            gap_type=ctx.gap_type,
            gap_strength=ctx.gap_strength,
            research_hypothesis=ctx.research_only
            or status is RecommendationStatus.RESEARCH_REQUIRED,
            creator_profile_id=ctx.profile.profile_id,
            generator=gen.generator,
            generator_model=gen.model,
            input_fingerprint=fingerprint,
            generated_at=ctx.now,
            versions={
                "recommendation": RECOMMENDATION_VERSION,
                "configuration": self.loaded.version_tag,
                "ranking": self.cfg.ranking.version,
                "analysis": ctx.report.analysis_version,
                "forecast": ctx.forecast.prediction_id or "none",
            },
            synthetic=ctx.synthetic,
        )


def now_or(clock_value: datetime | None, fallback: datetime) -> datetime:
    return clock_value or fallback
