"""The recommendation verification gate.

Nothing generated reaches a user without passing these steps, in this order:

 1. validate the schema (malformed, refused, truncated output -> bounded retry / abstain)
 2. resolve evidence references (fabricated, substituted, tampered, expired, synthetic)
 3. verify every claim - declared and extracted from title, rationale and notes
 4. check numerical consistency (product numbers vs evidence; statistics vs the engine)
 5. check freshness (stale or outdated support cannot back time-sensitive claims)
 6. confirm the trend classification (no proxy-as-search, no invented growth)
 7. check entity existence (unknown, ambiguous, rumoured, denied, conflicting)
 8. apply uncertainty labels
 9. reject or revise unsupported statements (deterministic removal/re-rendering; a
    bounded number of regenerations, stopped early when a rejected claim repeats)
10. persist the decision

The abstention state and the publication decision are computed here, by application
code. A generator's opinion about its own output (``verification_status``,
``insufficient_evidence``) can only make the result more conservative.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trend_agent.logging import get_logger, log_event
from trend_agent.schemas.evidence import AlertStage
from trend_agent.schemas.verification import (
    AbstentionState,
    ClaimAction,
    ClaimOrigin,
    ClaimStatus,
    ClaimVerdict,
    EvidenceCitation,
    EvidenceRecord,
    PublicationDecision,
    PublishedClaim,
    RecommendationDraft,
    RemovedStatement,
    VerifiedRecommendation,
    Violation,
    ViolationCode,
)
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.authority import stats_stale
from trend_agent.verification.claims import CandidateClaim, candidate_claims, extract
from trend_agent.verification.config import VERIFIER_VERSION, LoadedVerificationConfig
from trend_agent.verification.entities import EntityState, EntityVerifier, RecordView
from trend_agent.verification.generation import (
    GenerationContext,
    GenerationResult,
    GenerationStatus,
    ParseStatus,
    RecommendationGenerator,
    build_request,
    fallback_title,
    llm_schema,
    parse_draft,
    response_digest,
)
from trend_agent.verification.permissions import (
    PermissionPolicy,
    Principal,
    ToolRequest,
)
from trend_agent.verification.registry import EvidenceRegistry, ReferenceProblem
from trend_agent.verification.text import tokens
from trend_agent.verification.verifier import (
    ClaimVerifier,
    SemanticVerifier,
    VerificationInputs,
)

_log = get_logger(__name__)

KEPT = frozenset({ClaimAction.KEEP, ClaimAction.RECITE, ClaimAction.KEEP_ATTRIBUTED})
STEP_NAMES = (
    "1_validate_schema",
    "2_resolve_evidence",
    "3_verify_claims",
    "4_numerical_consistency",
    "5_freshness",
    "6_trend_classification",
    "7_entity_existence",
    "8_uncertainty_labels",
    "9_revise_or_reject",
    "10_persist_decision",
)
_NUMERIC = {
    ViolationCode.UNSUPPORTED_NUMBER,
    ViolationCode.STATISTIC_OVERRIDE,
    ViolationCode.SEARCH_VOLUME,
}
_FRESHNESS = {ViolationCode.STALE_EVIDENCE, ViolationCode.OUTDATED}
_TREND = {
    ViolationCode.PROXY_AS_SEARCH,
    ViolationCode.EXTERNAL_AS_YOUTUBE,
    ViolationCode.UNSUPPORTED_TREND,
}
_ENTITY = {
    ViolationCode.UNKNOWN_ENTITY,
    ViolationCode.AMBIGUOUS_ENTITY,
    ViolationCode.UNCONFIRMED_ENTITY,
    ViolationCode.RELEASE_CONFLICT,
}


@dataclass(frozen=True)
class GateStep:
    name: str
    outcome: str  # passed | revised | failed | skipped
    detail: str


@dataclass
class AttemptRecord:
    attempt: int
    generation_status: GenerationStatus
    parse_status: ParseStatus | None
    stop_reason: str | None
    response_sha256: str | None
    detail: str
    model: str | None = None
    violations: list[Violation] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    verdicts: list[ClaimVerdict] = field(default_factory=list)


@dataclass
class Evaluation:
    draft: RecommendationDraft
    verdicts: list[ClaimVerdict]
    violations: list[Violation]
    steps: list[GateStep]
    title_ok: bool
    retryable: bool
    feedback: list[str]


@dataclass
class GateDecision:
    run_id: str
    recommendation: VerifiedRecommendation
    attempts: list[AttemptRecord]
    steps: list[GateStep]
    verdicts: list[ClaimVerdict]
    draft: RecommendationDraft | None
    context: GenerationContext
    generator: str
    generator_model: str | None

    @property
    def published(self) -> bool:
        return self.recommendation.publication is not PublicationDecision.WITHHOLD


class VerificationStore(Protocol):
    def save(self, decision: GateDecision) -> None: ...


class VerificationGate:
    def __init__(
        self,
        registry: EvidenceRegistry,
        loaded: LoadedVerificationConfig,
        *,
        policy: PermissionPolicy | None = None,
        semantic: SemanticVerifier | None = None,
        store: VerificationStore | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.registry = registry
        self.loaded = loaded
        self.config = loaded.config
        self.policy = policy or PermissionPolicy.default()
        self.semantic = semantic
        self.store = store
        self._clock = clock
        self._schema = llm_schema()
        self._conflicts: list[str] = []

    # ------------------------------------------------------------------ public
    def run(self, context: GenerationContext, generator: RecommendationGenerator) -> GateDecision:
        run_id = str(uuid.uuid4())
        steps: list[GateStep] = []
        attempts: list[AttemptRecord] = []
        views, problems = self._views(context)
        if not views:
            state = (
                AbstentionState.SOURCE_UNAVAILABLE
                if context.unavailable or problems
                else AbstentionState.INSUFFICIENT_EVIDENCE
            )
            missing = list(context.unavailable) or ["no evidence is registered for this query"]
            missing += [f"{p.evidence_id}: {p.detail}" for p in problems.values()]
            steps.append(GateStep("2_resolve_evidence", "failed", "; ".join(missing)))
            return self._finish(
                run_id, context, generator, None, [], attempts, steps, state, missing, views
            )
        feedback: list[str] = []
        rejected: set[str] = set()
        final: Evaluation | None = None
        failure: str | None = None
        max_attempts = self.config.retry.max_attempts
        for attempt in range(1, max_attempts + 1):
            request = build_request(context, attempt, feedback, self._schema)
            try:
                result = generator.generate(request)
            except Exception as exc:  # a generator must never take the gate down
                result = GenerationResult(
                    GenerationStatus.ERROR, None, generator.name, detail=type(exc).__name__
                )
            record = AttemptRecord(
                attempt=attempt,
                generation_status=result.status,
                parse_status=None,
                stop_reason=result.stop_reason,
                response_sha256=response_digest(result.raw_text),
                detail=result.detail or "",
                model=result.model,
            )
            attempts.append(record)
            tool_violations = self._tool_requests(result.tool_requests, context)
            record.violations.extend(tool_violations)
            if result.status in (GenerationStatus.ERROR, GenerationStatus.UNAVAILABLE):
                failure = f"generator {result.status.value}: {result.detail or 'no detail'}"
                if result.transient and attempt < max_attempts:
                    continue
                break
            outcome = parse_draft(result)
            record.parse_status = outcome.status
            record.violations.extend(outcome.violations)
            if outcome.status is ParseStatus.REFUSED:
                record.violations.append(
                    Violation(code=ViolationCode.REFUSAL, detail=outcome.detail)
                )
                failure = f"generator refused: {outcome.detail}"
                break  # a refusal is final for this request; no regeneration
            if outcome.draft is None:
                failure = f"no schema-valid draft ({outcome.status.value}: {outcome.detail})"
                feedback = [
                    f"Your previous output was rejected ({outcome.status.value}): "
                    f"{outcome.detail}. Return only JSON that matches the schema."
                ]
                if self.config.retry.retry_on_malformed and attempt < max_attempts:
                    continue
                break
            failure = None
            evaluation = self._evaluate(context, outcome.draft, outcome.violations, views, problems)
            evaluation.violations.extend(tool_violations)
            record.verdicts = evaluation.verdicts
            record.feedback = evaluation.feedback
            final = evaluation
            if not evaluation.retryable or attempt == max_attempts:
                break
            removed = {v.fingerprint for v in evaluation.verdicts if v.action is ClaimAction.REMOVE}
            repeated = removed & rejected
            rejected |= removed
            if repeated and self.config.retry.stop_on_repeated_claim:
                evaluation.violations.append(
                    Violation(
                        code=ViolationCode.REPEATED_UNSUPPORTED,
                        detail=f"{len(repeated)} rejected claim(s) were generated again; "
                        "stopping regeneration",
                    )
                )
                break
            feedback = evaluation.feedback
        if final is None:
            steps.append(GateStep("1_validate_schema", "failed", failure or "no draft"))
            missing = [failure or "no draft produced"]
            return self._finish(
                run_id,
                context,
                generator,
                None,
                [],
                attempts,
                steps,
                AbstentionState.SOURCE_UNAVAILABLE,
                missing,
                views,
            )
        steps.extend(final.steps)
        return self._finish(
            run_id, context, generator, final, final.verdicts, attempts, steps, None, [], views
        )

    # --------------------------------------------------------------- internals
    def _views(
        self, context: GenerationContext
    ) -> tuple[dict[str, RecordView], dict[str, ReferenceProblem]]:
        allow = context.synthetic or self.config.allow_synthetic_evidence
        resolution = self.registry.resolve(
            context.bundle.evidence_ids, context.bundle, allow_synthetic=allow, now=context.now
        )
        views = {
            eid: RecordView.build(rec, context.catalog) for eid, rec in resolution.records.items()
        }
        return views, {p.evidence_id: p for p in resolution.problems}

    def _tool_requests(
        self, requests: Sequence[ToolRequest], context: GenerationContext
    ) -> list[Violation]:
        out = []
        for req in requests:
            # The principal is fixed by where the request came from, not by its content.
            forced = ToolRequest(req.name, dict(req.arguments), Principal.GENERATOR)
            decision = self.policy.authorize_tool(forced, context.bundle)
            log_event(
                _log,
                "verification.tool_request",
                tool=req.name,
                allowed=decision.allowed,
                reason=decision.reason,
            )
            if not decision.allowed:
                out.append(
                    Violation(
                        code=ViolationCode.TOOL_REQUEST_DENIED,
                        detail=f"{req.name}: {decision.reason}",
                    )
                )
        return out

    def _inputs(
        self,
        context: GenerationContext,
        views: dict[str, RecordView],
        problems: dict[str, ReferenceProblem],
    ) -> VerificationInputs:
        grounding = set(tokens(context.query)) | set(tokens(context.topic or ""))
        for key in context.topic_entity_keys:
            entity = context.catalog.get(key)
            if entity is not None:
                for n in entity.names():
                    grounding.update(tokens(n))
        urls = set()
        for v in views.values():
            for u in (v.record.canonical_url, v.record.source_reference):
                if u and u.startswith(("http://", "https://")):
                    urls.add(u.rstrip("/"))
        return VerificationInputs(
            views=views,
            catalog=context.catalog,
            stats=context.stats,
            query=context.query,
            now=context.now,
            config=self.config,
            category=context.category,
            topic_entity_keys=context.topic_entity_keys,
            grounding_terms=frozenset(grounding),
            problems=problems,
            allowed_urls=frozenset(urls),
        )

    def _evaluate(
        self,
        context: GenerationContext,
        draft: RecommendationDraft,
        parse_violations: list[Violation],
        views: dict[str, RecordView],
        bundle_problems: dict[str, ReferenceProblem],
    ) -> Evaluation:
        steps: list[GateStep] = []
        violations = list(parse_violations)
        steps.append(
            GateStep(
                STEP_NAMES[0],
                "revised" if parse_violations else "passed",
                "; ".join(v.detail for v in parse_violations) or "schema valid (not a fact check)",
            )
        )
        if draft.search_query_id != context.query_id:
            violations.append(
                Violation(
                    code=ViolationCode.ID_SUBSTITUTION,
                    detail=(
                        f"search_query_id {draft.search_query_id} replaced by {context.query_id}"
                    ),
                    field="search_query_id",
                )
            )
        if draft.topic_id is not None and draft.topic_id != context.topic_id:
            violations.append(
                Violation(
                    code=ViolationCode.ID_SUBSTITUTION,
                    detail=f"topic_id {draft.topic_id} replaced by {context.topic_id}",
                    field="topic_id",
                )
            )
        cited = list(draft.supporting_evidence_ids)
        for c in draft.factual_claims:
            cited.extend(c.evidence_ids)
        allow = context.synthetic or self.config.allow_synthetic_evidence
        resolution = self.registry.resolve(
            cited, context.bundle, allow_synthetic=allow, now=context.now
        )
        problems = dict(bundle_problems)
        problems.update({p.evidence_id: p for p in resolution.problems})
        for p in resolution.problems:
            violations.append(Violation(code=p.code, detail=f"{p.evidence_id}: {p.detail}"))
        steps.append(
            GateStep(
                STEP_NAMES[1],
                "failed" if resolution.problems else "passed",
                f"{len(resolution.records)} of {len(set(cited))} cited references resolved"
                + (
                    "; "
                    + "; ".join(f"{p.evidence_id}: {p.code.value}" for p in resolution.problems)
                    if resolution.problems
                    else ""
                ),
            )
        )
        verifier = ClaimVerifier(self._inputs(context, views, problems), self.semantic)
        candidates = candidate_claims(draft)
        verdicts = [verifier.verify(c) for c in candidates]
        counts = Counter(
            v.status.value for v in verdicts if v.action is not ClaimAction.NOT_FACTUAL
        )
        steps.append(
            GateStep(
                STEP_NAMES[2],
                "passed"
                if not any(v.action is ClaimAction.REMOVE for v in verdicts)
                else "revised",
                ", ".join(f"{k}={n}" for k, n in sorted(counts.items())) or "no factual claims",
            )
        )
        for name, codes in (
            (STEP_NAMES[3], _NUMERIC),
            (STEP_NAMES[4], _FRESHNESS),
            (STEP_NAMES[5], _TREND),
            (STEP_NAMES[6], _ENTITY),
        ):
            hit = [v for v in verdicts if codes & set(v.violations)]
            steps.append(
                GateStep(
                    name,
                    "revised" if hit else "passed",
                    "; ".join(
                        f"{v.field}: {', '.join(c.value for c in v.violations if c in codes)}"
                        for v in hit
                    )
                    or "no issues",
                )
            )
        labels = [v for v in verdicts if v.uncertainty_label]
        steps.append(
            GateStep(STEP_NAMES[7], "revised" if labels else "passed", f"{len(labels)} labelled")
        )
        title = next(v for v in verdicts if v.origin is ClaimOrigin.TITLE)
        title_ok = title.action is ClaimAction.KEEP
        factual = [v for v in verdicts if v.action is not ClaimAction.NOT_FACTUAL]
        kept = [v for v in factual if v.action in KEPT and v.origin is not ClaimOrigin.TITLE]
        removed = [v for v in factual if v.action is ClaimAction.REMOVE]
        core_contradicted = any(v.core and v.status is ClaimStatus.CONTRADICTED for v in factual)
        retryable = (
            (not title_ok and self.config.retry.retry_on_unsupported_title)
            or core_contradicted
            or (not kept and bool(removed))
        ) and not draft.insufficient_evidence
        feedback = [
            f"{v.field}: '{v.text[:160]}' - {v.reasons[0] if v.reasons else v.status.value}"
            for v in removed
        ]
        feedback += [f"{v.field}: {v.detail}" for v in violations if v.field]
        steps.append(
            GateStep(
                STEP_NAMES[8],
                "revised" if removed or not title_ok else "passed",
                f"{len(removed)} statement(s) removed"
                + ("" if title_ok else "; title not supported"),
            )
        )
        return Evaluation(draft, verdicts, violations, steps, title_ok, retryable, feedback)

    def _topic_problem(
        self, context: GenerationContext, views: dict[str, RecordView]
    ) -> str | None:
        checker = EntityVerifier(
            context.catalog,
            views,
            now=context.now,
            category=context.category,
            topic_entity_keys=context.topic_entity_keys,
        )
        for key in sorted(context.topic_entity_keys):
            entity = context.catalog.get(key)
            if entity is None:
                continue
            chk = checker.check_name(entity.canonical_name)
            for conflict in chk.conflicts:
                self._conflicts.append(f"{entity.canonical_name}: {conflict}")
            if chk.state in (EntityState.UNKNOWN, EntityState.REJECTED, EntityState.AMBIGUOUS):
                return f"topic entity '{entity.canonical_name}' is {chk.state.value}"
            if chk.denied_by:
                return f"a credible source denies or cancels '{entity.canonical_name}'"
        return None

    def _finish(
        self,
        run_id: str,
        context: GenerationContext,
        generator: RecommendationGenerator,
        evaluation: Evaluation | None,
        verdicts: list[ClaimVerdict],
        attempts: list[AttemptRecord],
        steps: list[GateStep],
        forced: AbstentionState | None,
        missing: list[str],
        views: dict[str, RecordView],
    ) -> GateDecision:
        stats = context.stats
        now = context.now
        draft = evaluation.draft if evaluation else None
        missing = list(missing)
        labels: list[str] = []
        title: str | None = None
        title_revised = False
        if evaluation is not None and draft is not None:
            factual = [v for v in verdicts if v.action is not ClaimAction.NOT_FACTUAL]
            kept = [v for v in factual if v.action in KEPT and v.origin is not ClaimOrigin.TITLE]
            removed = [v for v in factual if v.action is ClaimAction.REMOVE]
            core_contra = [v for v in factual if v.core and v.status is ClaimStatus.CONTRADICTED]
            self._conflicts = []
            topic_problem = self._topic_problem(context, views)
            for conflict in self._conflicts:
                missing.append(conflict)
                labels.append(f"Sources disagree - {conflict}")
            stale = stats_stale(stats, now, self.config)
            title = draft.proposed_video_title if evaluation.title_ok else None
            if title is None:
                candidate = fallback_title(context.query, draft.audience_search_intent)
                verifier = ClaimVerifier(self._inputs(context, views, {}), self.semantic)
                check = verifier.verify(
                    CandidateClaim(
                        index=len(verdicts),
                        origin=ClaimOrigin.TITLE,
                        field="proposed_video_title(fallback)",
                        text=candidate,
                        cited_ids=(),
                        features=extract(candidate),
                    )
                )
                if check.action is ClaimAction.KEEP:
                    title, title_revised = candidate, True
            for v in removed:
                missing.append(f"{v.field}: {v.reasons[0] if v.reasons else v.status.value}")
            if core_contra:
                state = AbstentionState.CONTRADICTED
                missing.insert(0, "a core statement is contradicted by credible evidence")
            elif draft.insufficient_evidence:
                state = AbstentionState.INSUFFICIENT_EVIDENCE
                missing.insert(0, "the generator reported that the evidence is insufficient")
            elif topic_problem:
                state = AbstentionState.INSUFFICIENT_EVIDENCE
                missing.insert(0, topic_problem)
            elif title is None:
                state = AbstentionState.INSUFFICIENT_EVIDENCE
                missing.insert(0, "no supportable title")
            elif not kept and removed:
                state = AbstentionState.INSUFFICIENT_EVIDENCE
                missing.insert(0, "every factual statement of the draft was unsupported")
            elif not any(
                v.status in (ClaimStatus.VERIFIED, ClaimStatus.CORROBORATED) for v in kept
            ) and not (stats.available and not stale):
                state = AbstentionState.INSUFFICIENT_EVIDENCE
                missing.insert(0, "no verified statement and no current surge evidence")
            elif (
                all(v.status is ClaimStatus.VERIFIED for v in kept)
                and not removed
                and not title_revised
                and stats.measures_youtube_search
                and stats.highest_stage in (AlertStage.CANDIDATE, AlertStage.CONFIRMED)
                and not stale
            ):
                state = AbstentionState.VERIFIED_RESULT
            else:
                state = AbstentionState.PARTIALLY_VERIFIED
            if forced is not None:
                state = forced
        else:
            state = forced or AbstentionState.SOURCE_UNAVAILABLE
        if not stats.available:
            missing.append("no surge episode: rising demand is not established")
        elif not stats.measures_youtube_search:
            missing.append(
                "no direct YouTube search-interest measurement; demand evidence is "
                + ", ".join(c.value for c in stats.evidence_categories)
            )
        if stats_stale(stats, now, self.config):
            missing.append("the statistical engine's last update is stale")
        publication = {
            AbstentionState.VERIFIED_RESULT: PublicationDecision.PUBLISH,
            AbstentionState.PARTIALLY_VERIFIED: PublicationDecision.PUBLISH_WITH_LABELS,
        }.get(state, PublicationDecision.WITHHOLD)
        if context.synthetic and publication is PublicationDecision.PUBLISH:
            publication = PublicationDecision.PUBLISH_WITH_LABELS
        publish = publication is not PublicationDecision.WITHHOLD
        claims: list[PublishedClaim] = []
        rationale: list[str] = []
        intent_text: list[str] = []
        uncertainties: list[str] = []
        cited: list[str] = []
        for v in verdicts:
            if v.action in KEPT and v.origin is not ClaimOrigin.TITLE:
                cited.extend(v.supporting_ids)
                if v.uncertainty_label:
                    labels.append(v.uncertainty_label)
            text = v.published_text
            keep_text = v.action in KEPT or v.action is ClaimAction.NOT_FACTUAL
            if not keep_text or text is None:
                continue
            if v.origin is ClaimOrigin.DECLARED:
                claims.append(
                    PublishedClaim(
                        text=text,
                        status=v.status,
                        certainty=v.certainty,
                        evidence_ids=v.supporting_ids,
                        uncertainty_label=v.uncertainty_label,
                    )
                )
            elif v.origin is ClaimOrigin.RATIONALE:
                rationale.append(text)
            elif v.origin is ClaimOrigin.INTENT:
                intent_text.append(text)
            elif v.origin is ClaimOrigin.UNCERTAINTY:
                uncertainties.append(text)
        if context.synthetic:
            labels.insert(0, "[SYNTHETIC] All evidence is synthetic test data.")
        if stats.available and not stats.measures_youtube_search:
            labels.append(
                "Demand is not measured by YouTube search interest (see trend statement)."
            )
        if not stats.available:
            labels.append("No surge episode supports a demand claim for this query.")
        cited.extend(stats.evidence_ids)
        citations = [
            self._citation(views[e].record, now) for e in dict.fromkeys(cited) if e in views
        ]
        removed_out = [
            RemovedStatement(field=v.field, text=v.text, status=v.status, reasons=v.reasons)
            for v in verdicts
            if v.action is ClaimAction.REMOVE
            or (
                v.action is ClaimAction.REPLACE_AUTHORITATIVE
                and v.status is not ClaimStatus.VERIFIED
            )
        ]
        all_violations = sorted(
            {x.value for v in verdicts for x in v.violations}
            | ({x.code.value for x in evaluation.violations} if evaluation else set())
            | {x.code.value for a in attempts for x in a.violations}
        )
        trend = stats.statement if not context.synthetic else f"[SYNTHETIC] {stats.statement}"
        recommendation = VerifiedRecommendation(
            run_id=run_id,
            target_id=context.target_id,
            search_query_id=context.query_id,
            query=context.query,
            topic_id=context.topic_id,
            category=context.category,
            verification_status=state,
            publication=publication,
            proposed_video_title=title if publish else None,
            title_revised=title_revised and publish,
            audience_search_intent=draft.audience_search_intent if draft and publish else None,
            audience_intent_description=" ".join(intent_text) if publish and intent_text else None,
            factual_claims=tuple(claims) if publish else (),
            supporting_evidence=tuple(citations) if publish else (),
            statistics=stats,
            trend_statement=trend,
            uncertainties=tuple(dict.fromkeys([*labels, *uncertainties])) if publish else (),
            recommendation_rationale=" ".join(rationale) if publish and rationale else None,
            missing_evidence=tuple(dict.fromkeys(missing)),
            removed_statements=tuple(removed_out),
            violations=tuple(all_violations),
            generator=generator.name,
            generator_model=next(
                (a.model for a in reversed(attempts) if a.model), getattr(generator, "model", None)
            ),
            attempts=len(attempts),
            verifier_version=VERIFIER_VERSION,
            configuration_version=self.loaded.version_tag,
            decided_at=self._clock(),
            synthetic=context.synthetic,
        )
        steps.append(
            GateStep(
                STEP_NAMES[9],
                "passed" if self.store is not None else "skipped",
                f"{state.value} -> {publication.value}",
            )
        )
        decision = GateDecision(
            run_id=run_id,
            recommendation=recommendation,
            attempts=attempts,
            steps=steps,
            verdicts=verdicts,
            draft=draft,
            context=context,
            generator=generator.name,
            generator_model=recommendation.generator_model,
        )
        if self.store is not None:
            self.store.save(decision)
        log_event(
            _log,
            "verification.decision",
            run_id=run_id,
            target_id=context.target_id,
            state=state.value,
            publication=publication.value,
            attempts=len(attempts),
            removed=len(removed_out),
        )
        return decision

    def _citation(self, record: EvidenceRecord, now: datetime) -> EvidenceCitation:
        return EvidenceCitation(
            evidence_id=record.evidence_id,
            provider=record.provider_name,
            source_reference=record.source_reference,
            canonical_url=record.canonical_url,
            publisher=record.publisher,
            published_at=record.published_at,
            collected_at=record.collected_at,
            evidence_kind=record.evidence_kind,
            measurement_semantics=record.measurement_semantics,
            trust_level=record.trust_level,
            freshness=self.registry.freshness(record, now),
            retention_note=record.retention_note,
            synthetic=record.is_synthetic,
        )
