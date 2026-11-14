"""The recommendation verification gate (application code decides; generators never do).

Each concept is checked in two layers:

1. **Phase 7 rules** for any generator: title checks (length, clickbait, superlatives,
   invented numbers, view promises), hook checks (untested results stated as fact, artificial
   urgency), outline label integrity (a VERIFIED_FACT must cite evidence of this analysis; a
   hypothesis is never a completed result), forecast and view claims in free text, and
   evidence references that are not in the opportunity's evidence bundle (fabricated or
   substituted IDs).
2. **The existing anti-hallucination gate** (``verification.gate.VerificationGate``): the
   concept becomes a ``RecommendationDraft`` (primary title + alternative titles, hooks,
   VERIFIED_FACT items and the premise as declared claims) and goes through all ten gate
   steps against the Phase 5 analysis' evidence bundle: claim support, numbers and the
   statistical authority (search-volume, proxy-as-search, external-as-YouTube rules),
   freshness, entity existence (unknown, rumoured, ambiguous, denied), injection output
   checks. Unsupported statements are removed and mapped back onto the concept.

Status: REJECTED (contradicted core claim), RESEARCH_REQUIRED (insufficient evidence, no
supportable title, nothing left to film, or a research-only opportunity),
APPROVED_WITH_UNCERTAINTY (publishable with removals, attributions or indirect demand),
APPROVED (everything checked is verified). Valid JSON is never evidence of correctness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from trend_agent.recommendations.config import LoadedRecommendationConfig
from trend_agent.recommendations.context_builder import EvidenceContext, RecommendationContext
from trend_agent.recommendations.hook_generator import check_hook
from trend_agent.recommendations.idea_generator import ConceptDraft
from trend_agent.recommendations.outline_generator import UNSAFE_TEXT, enforce_labels
from trend_agent.recommendations.title_generator import check_title, sentence_case
from trend_agent.schemas.recommendation import (
    Hook,
    Outline,
    OutlineItem,
    OutlineSection,
    RecommendationStatus,
    RecommendationVerification,
    StatementLabel,
    TitleOption,
    VerificationCheck,
)
from trend_agent.schemas.verification import (
    AbstentionState,
    ClaimAction,
    ClaimCertainty,
    ClaimDraft,
    ClaimOrigin,
    ClaimType,
    RecommendationDraft,
)
from trend_agent.verification.gate import VerificationGate
from trend_agent.verification.generation import GenerationContext, ScriptedGenerator

S = RecommendationStatus
_KEPT = {ClaimAction.KEEP, ClaimAction.KEEP_ATTRIBUTED, ClaimAction.RECITE, ClaimAction.NOT_FACTUAL}
_FORECAST_TEXT = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s*%|probability|chance of|will (?:peak|grow|explode|trend|spike|keep "
    r"growing)|is (?:about|going) to (?:peak|explode|trend)|forecast(?:ed|s)? (?:to|that))\b",
    re.IGNORECASE,
)
_VIEWS_TEXT = re.compile(
    r"\b(?:go(?:es)? viral|get (?:more )?views|guaranteed?|millions? of views|views (?:will|are)|"
    r"rank #?1|blow up)\b",
    re.IGNORECASE,
)


@dataclass
class VerifiedConcept:
    titles: list[TitleOption]
    hooks: list[Hook]
    outline: Outline
    evidence_ids: list[str]
    premise: str
    why: str
    verification: RecommendationVerification
    title: str | None
    removed: list[str] = field(default_factory=list)


def _lower_first(text: str, names: tuple[str, ...]) -> str:
    """A sentence-initial common word ("Viewers ...") is not a product name."""
    if not text or any(text.startswith(n) for n in names if n):
        return text
    return text[:1].lower() + text[1:]


def _strip(text: str, pattern: re.Pattern[str]) -> tuple[str, list[str]]:
    kept: list[str] = []
    removed: list[str] = []
    for s in re.split(r"(?<=[.!?])\s+", text.strip()):
        (removed if pattern.search(s) else kept).append(s)
    return " ".join(kept).strip(), removed


def _outline(draft: ConceptDraft) -> Outline:
    return Outline(
        sections=tuple(
            OutlineSection(
                heading=s.heading,
                purpose=s.purpose,
                items=tuple(
                    OutlineItem(label=i.label, text=i.text, evidence_ids=tuple(i.evidence_ids))
                    for i in s.items
                ),
            )
            for s in draft.outline
        ),
        required_evidence=tuple(draft.required_evidence),
        failure_points=tuple(draft.failure_points),
        conclusion_to_investigate=draft.conclusion_to_investigate or "What the video shows.",
    )


def verify_concept(
    draft: ConceptDraft,
    ctx: RecommendationContext,
    ev: EvidenceContext,
    loaded: LoadedRecommendationConfig,
    *,
    generator: str,
) -> VerifiedConcept:
    gcfg = loaded.config.generation
    checks: list[VerificationCheck] = []
    removed: list[str] = []
    names = tuple(n for n in (*ctx.entities, ctx.other_entity) if n)
    # ---------------------------------------------------------------- titles
    titles: list[TitleOption] = []
    for dt in draft.titles:
        tc = check_title(
            dt.text,
            query=ctx.query,
            allowed_names=names,
            soft_max=gcfg.max_title_chars,
            hard_max=gcfg.hard_max_title_chars,
        )
        if UNSAFE_TEXT.search(dt.text):
            tc = replace(tc, accepted=False, reasons=(*tc.reasons, "instruction or command text"))
        titles.append(
            TitleOption(
                strategy=dt.strategy,
                text=" ".join(dt.text.split()),
                rationale=dt.rationale or "generated",
                target_intent=ctx.sub_intent or ctx.primary_intent,
                query_term_coverage=tc.coverage,
                length=len(dt.text),
                accepted=tc.accepted,
                reasons=tc.reasons,
            )
        )
        if not tc.accepted:
            removed.append(f"title: {dt.text} ({'; '.join(tc.reasons)})")
    checks.append(
        VerificationCheck(
            check="title_rules",
            passed=any(x.accepted for x in titles),
            detail=f"{sum(x.accepted for x in titles)}/{len(titles)} titles passed",
        )
    )
    # ----------------------------------------------------------------- hooks
    hooks: list[Hook] = []
    for dh in draft.hooks:
        hc = check_hook(dh.text, allowed_names=names, word_range=gcfg.hook_words)
        if UNSAFE_TEXT.search(dh.text):
            hc = replace(hc, accepted=False, reasons=(*hc.reasons, "instruction or command text"))
        hooks.append(
            Hook(
                hook_type=dh.hook_type,
                text=dh.text,
                estimated_seconds=hc.seconds,
                accepted=hc.accepted,
                reasons=hc.reasons,
            )
        )
        if not hc.accepted:
            removed.append(f"hook: {dh.text} ({'; '.join(hc.reasons)})")
    checks.append(
        VerificationCheck(
            check="hook_rules",
            passed=any(x.accepted for x in hooks),
            detail=f"{sum(x.accepted for x in hooks)}/{len(hooks)} hooks passed",
        )
    )
    # --------------------------------------------------------------- outline
    outline, out_removed = enforce_labels(_outline(draft), ctx.bundle_ids)
    removed += [f"outline: {r}" for r in out_removed]
    relabelled = [
        i.text for s in outline.sections for i in s.items if i.note and "without evidence" in i.note
    ]
    removed += [
        f"outline: relabelled UNKNOWN (no evidence from this analysis): {t}" for t in relabelled
    ]
    downgraded = len(relabelled)
    checks.append(
        VerificationCheck(
            check="outline_labels",
            passed=not out_removed,
            detail=f"{len(out_removed)} removed, {downgraded} unsupported facts relabelled UNKNOWN",
        )
    )
    # ------------------------------------------------- evidence references
    ids = [i for i in draft.evidence_ids if i in ctx.bundle_ids]
    fabricated = [i for i in draft.evidence_ids if i not in ctx.bundle_ids]
    checks.append(
        VerificationCheck(
            check="evidence_references",
            passed=not fabricated,
            detail=("references outside this analysis' evidence: " + ", ".join(fabricated[:5]))
            if fabricated
            else f"{len(ids)} references resolved",
        )
    )
    # ------------------------------------------- forecast / view claims in text
    premise, p_rm = _strip(draft.premise, _FORECAST_TEXT)
    why, w_rm = _strip(draft.why_it_may_satisfy_demand, _FORECAST_TEXT)
    premise, p_rm2 = _strip(premise, _VIEWS_TEXT)
    why, w_rm2 = _strip(why, _VIEWS_TEXT)
    text_removed = p_rm + w_rm + p_rm2 + w_rm2
    removed += [f"forecast or view claim: {s}" for s in text_removed]
    checks.append(
        VerificationCheck(
            check="no_forecast_or_view_claims",
            passed=not text_removed,
            detail="forecasts and demand are attached from Phase 4-6 outputs only",
        )
    )
    if not why:
        why = f"It answers the '{ctx.sub_intent or ctx.primary_intent}' intent behind the query."
    if not premise:
        premise = f"A video about {ctx.topic} that answers the search query directly."
    # ------------------------------------------------------- the existing gate
    accepted_titles = [t for t in titles if t.accepted]
    primary = accepted_titles[0].text if accepted_titles else None
    # Declared claims are verified as facts: only VERIFIED_FACT items are declared. Hooks,
    # alternative titles, premise and rationale are free text: the gate extracts and checks
    # their factual elements sentence by sentence (entities, release wording, numbers,
    # trend statements); plain plans are not factual and pass as such.
    claims: list[tuple[str, str, ClaimDraft]] = []
    facts = [
        i for s in outline.sections for i in s.items if i.label is StatementLabel.VERIFIED_FACT
    ]
    for i in facts[:8]:
        claims.append(
            (
                "fact",
                i.text,
                ClaimDraft(
                    text=i.text[:600],
                    claim_type=ClaimType.OTHER,
                    subject=ctx.topic,
                    evidence_ids=list(i.evidence_ids),
                    certainty=ClaimCertainty.REPORTED,
                    attribution=(i.text.split(":", 1)[0].replace("According to ", "")[:200]),
                ),
            )
        )
    free: list[tuple[str, str]] = [("why", why), ("premise", premise)]
    free += [("hook", h.text) for h in hooks if h.accepted]
    free += [("title", sentence_case(t.text, names)) for t in accepted_titles[1:]]
    rationale = "\n".join(
        t if t.rstrip().endswith((".", "?", "!")) else t.rstrip() + "." for _, t in free
    )[:2000]
    gate_state = gate_pub = gate_run = trend = None
    labels: list[str] = []
    title: str | None = None
    if primary is not None:
        rd = RecommendationDraft(
            proposed_video_title=primary[:160],
            topic_id=ctx.topic_cluster_id,
            search_query_id=ctx.query_id or 0,
            audience_search_intent=ctx.gate_intent,
            audience_intent_description=_lower_first(draft.target_audience, names)[:400],
            supporting_evidence_ids=ids[:24],
            factual_claims=[c for _, _, c in claims],
            # only generator-written caveats are checked; the application's own limitation
            # notes (synthetic data, missing forecast, ...) are not generated claims
            uncertainties=[
                u[:300]
                for u in draft.uncertainties
                if u not in ctx.limitations and not generator.startswith("template")
            ][:12],
            recommendation_rationale=rationale,
        )
        gctx = GenerationContext(
            target_id=None,
            query_id=ctx.query_id or 0,
            query=ctx.query,
            topic_id=ctx.topic_cluster_id,
            topic=ctx.topic,
            category=ctx.category,
            primary_intent=ctx.gate_intent,
            bundle=ev.bundle,
            records=ev.records,
            catalog=ev.catalog,
            stats=ev.stats,
            now=ev.now,
            topic_entity_keys=ctx.entity_keys,
            synthetic=ctx.synthetic,
        )
        decision = VerificationGate(ev.registry, ev.vloaded, store=None, clock=lambda: ev.now).run(
            gctx, ScriptedGenerator([rd.model_dump(mode="json")], name=generator)
        )
        rec = decision.recommendation
        gate_state, gate_pub, gate_run = (
            rec.verification_status.value,
            rec.publication.value,
            rec.run_id,
        )
        trend = rec.trend_statement
        labels = sorted({c.uncertainty_label for c in rec.factual_claims if c.uncertainty_label})
        bad_verdicts = [v for v in decision.verdicts if v.action not in _KEPT]
        bad_text = {v.text for v in bad_verdicts if v.origin is ClaimOrigin.DECLARED}
        bad_sentences = [v for v in bad_verdicts if v.origin is ClaimOrigin.RATIONALE]

        def free_bad(text: str) -> str | None:
            for v in bad_sentences:
                frag = v.text.strip().rstrip(".").lower()
                if frag and frag in text.lower():
                    return v.reasons[0] if v.reasons else "unsupported"
            return None

        title_removed = rec.title_revised or any(
            v.origin is ClaimOrigin.TITLE and v.action not in _KEPT for v in decision.verdicts
        )
        reasons_by_text: dict[str, str] = {}
        for v in bad_verdicts:
            if v.reasons:
                reasons_by_text.setdefault(v.text, v.reasons[0])
        for kind, text, _ in claims:
            if text in bad_text:
                removed.append(
                    f"{kind} (gate): {text} - {reasons_by_text.get(text, 'unsupported')}"
                )
        new_titles = []
        for t in titles:
            why_bad = free_bad(t.text) if t.accepted and t.text != primary else None
            if t.accepted and t.text == primary and title_removed:
                why_bad = "not supported by the evidence (gate)"
            if why_bad:
                removed.append(f"title (gate): {t.text} - {why_bad}")
                t = t.model_copy(update={"accepted": False, "reasons": (*t.reasons, why_bad)})
            new_titles.append(t)
        titles = new_titles
        new_hooks = []
        for h in hooks:
            if h.accepted:
                why_bad = free_bad(h.text)
                if why_bad:
                    removed.append(f"hook (gate): {h.text} - {why_bad}")
                    h = h.model_copy(
                        update={
                            "accepted": False,
                            "verification_status": "removed",
                            "reasons": (*h.reasons, why_bad),
                        }
                    )
                else:
                    h = h.model_copy(update={"verification_status": "checked"})
            new_hooks.append(h)
        hooks = new_hooks
        if free_bad(premise):
            removed.append(f"premise (gate): {premise}")
            premise = f"A video about {ctx.topic} that answers the search query directly."
        if free_bad(why):
            removed.append(f"rationale (gate): {why}")
            why = (
                f"It answers the '{ctx.sub_intent or ctx.primary_intent}' intent behind the query."
            )
        fact_bad = {t for k, t, _ in claims if k == "fact" and t in bad_text}
        if fact_bad:
            sections = []
            for s in outline.sections:
                items = tuple(i for i in s.items if i.text not in fact_bad)
                if items:
                    sections.append(s.model_copy(update={"items": items}))
            outline = outline.model_copy(
                update={
                    "sections": tuple(sections),
                    "required_evidence": (
                        *outline.required_evidence,
                        *(f"Verify before filming: {t}" for t in fact_bad),
                    ),
                    "removed_items": (*outline.removed_items, *fact_bad),
                }
            )
        title = next((t.text for t in titles if t.accepted), None)
        checks.append(
            VerificationCheck(
                check="verification_gate",
                passed=gate_state
                in (
                    AbstentionState.VERIFIED_RESULT.value,
                    AbstentionState.PARTIALLY_VERIFIED.value,
                ),
                detail=f"{gate_state} / {gate_pub}; {len(bad_text)} declared statement(s) removed",
            )
        )
    else:
        # without a title the gate does not run, so nothing may keep the VERIFIED_FACT label
        unverified = [i.text for i in facts]
        if unverified:
            outline = outline.model_copy(
                update={
                    "sections": tuple(
                        s.model_copy(
                            update={
                                "items": tuple(
                                    i.model_copy(
                                        update={
                                            "label": StatementLabel.UNKNOWN,
                                            "note": "not verified: no title passed the rules",
                                        }
                                    )
                                    if i.label is StatementLabel.VERIFIED_FACT
                                    else i
                                    for i in s.items
                                )
                            }
                        )
                        for s in outline.sections
                    )
                }
            )
            removed += [f"fact (unverified, relabelled UNKNOWN): {t}" for t in unverified]
        checks.append(
            VerificationCheck(
                check="verification_gate", passed=False, detail="no title passed the title rules"
            )
        )
    # ------------------------------------------------------------- status
    reasons: list[str] = []
    filmable = any(
        i.label
        in (
            StatementLabel.PLANNED_DEMONSTRATION,
            StatementLabel.VERIFIED_FACT,
            StatementLabel.HYPOTHESIS_TO_TEST,
        )
        for s in outline.sections
        for i in s.items
    )
    if gate_state == AbstentionState.CONTRADICTED.value:
        status = S.REJECTED
        reasons.append("a core statement is contradicted by credible evidence")
    elif title is None:
        status = S.RESEARCH_REQUIRED
        reasons.append("no title is supported by the evidence")
    elif ctx.research_only:
        status = S.RESEARCH_REQUIRED
        reasons.append("the opportunity has insufficient evidence: research candidate only")
    elif gate_state in (
        AbstentionState.INSUFFICIENT_EVIDENCE.value,
        AbstentionState.SOURCE_UNAVAILABLE.value,
    ):
        status = S.RESEARCH_REQUIRED
        reasons.append(f"verification gate: {gate_state}")
    elif not any(h.accepted for h in hooks) or not filmable:
        status = S.RESEARCH_REQUIRED
        reasons.append("no supportable hook or nothing concrete to film")
    elif gate_state == AbstentionState.VERIFIED_RESULT.value and not removed and not fabricated:
        status = S.APPROVED
    else:
        status = S.APPROVED_WITH_UNCERTAINTY
        if removed or fabricated:
            reasons.append(f"{len(removed) + len(fabricated)} statement(s) removed or relabelled")
        if gate_state == AbstentionState.PARTIALLY_VERIFIED.value:
            reasons.append(
                "the gate published with labels (attributions, indirect demand or "
                "removed statements)"
            )
    if ctx.synthetic:
        labels.insert(0, "[SYNTHETIC] fictional test data")
    if ctx.demand.indirect:
        labels.append("demand is not measured YouTube search growth: " + ctx.demand.basis)
    if not ctx.forecast.available:
        labels.append("no usable forecast")
    return VerifiedConcept(
        titles=titles,
        hooks=hooks,
        outline=outline,
        evidence_ids=ids,
        premise=premise,
        why=why,
        verification=RecommendationVerification(
            status=status,
            synthetic=ctx.synthetic,
            gate_state=gate_state,
            gate_publication=gate_pub,
            gate_run_id=gate_run,
            trend_statement=trend,
            checks=tuple(checks),
            removed=tuple(dict.fromkeys(removed)),
            labels=tuple(dict.fromkeys(labels)),
            reasons=tuple(reasons),
            verified_at=ev.now,
        ),
        title=title,
        removed=removed,
    )
