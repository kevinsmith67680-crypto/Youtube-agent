"""Phase 5 persistence: idempotent analysis writes, read models, retention."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.db.content_models import (
    AnalysisRunStatus,
    ContentAnalysisRunRow,
    ContentGapRow,
    ContentOpportunityRow,
    InformationRequirementRow,
    RequirementCoverageRow,
    SearchIntentAnalysisRow,
    VideoResultAnalysisRow,
)
from trend_agent.db.discovery_models import DiscoveredEntityRow
from trend_agent.db.models import SearchQueryRow

if TYPE_CHECKING:
    from trend_agent.content_analysis.orchestrator import AnalysisInputs, AnalysisOutcome


def _exists(session: Session, model: Any, key: Any) -> bool:
    return key is not None and session.get(model, key) is not None


class ContentAnalysisRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ lookups
    def completed_by_fingerprint(self, fingerprint: str) -> ContentAnalysisRunRow | None:
        return self._s.execute(
            sa.select(ContentAnalysisRunRow).where(
                ContentAnalysisRunRow.fingerprint == fingerprint,
                ContentAnalysisRunRow.status == AnalysisRunStatus.COMPLETED,
            )
        ).scalar_one_or_none()

    def latest_completed(
        self, *, inputs_fingerprint: str | None = None, query: str | None = None
    ) -> ContentAnalysisRunRow | None:
        stmt = sa.select(ContentAnalysisRunRow).where(
            ContentAnalysisRunRow.status == AnalysisRunStatus.COMPLETED
        )
        if inputs_fingerprint is not None:
            stmt = stmt.where(ContentAnalysisRunRow.inputs_fingerprint == inputs_fingerprint)
        if query is not None:
            stmt = stmt.where(sa.func.lower(ContentAnalysisRunRow.canonical_query) == query.lower())
        return self._s.execute(
            stmt.order_by(ContentAnalysisRunRow.completed_at.desc()).limit(1)
        ).scalar_one_or_none()

    def last_completed_for(
        self, *, target_id: int | None = None, event_id: int | None = None
    ) -> ContentAnalysisRunRow | None:
        r = ContentAnalysisRunRow
        stmt = sa.select(r).where(r.status == AnalysisRunStatus.COMPLETED)
        if target_id is not None:
            stmt = stmt.where(r.target_id == target_id)
        if event_id is not None:
            stmt = stmt.where(r.surge_event_id == event_id)
        return self._s.execute(
            stmt.order_by(r.completed_at.desc().nulls_last()).limit(1)
        ).scalar_one_or_none()

    def last_completed_by_target(
        self, target_ids: Sequence[int]
    ) -> dict[int, ContentAnalysisRunRow]:
        """Latest completed run per target (one query for the whole queue)."""
        r = ContentAnalysisRunRow
        if not target_ids:
            return {}
        rows = self._s.execute(
            sa.select(r)
            .where(r.status == AnalysisRunStatus.COMPLETED, r.target_id.in_(list(target_ids)))
            .order_by(r.target_id, r.completed_at.desc())
            .distinct(r.target_id)
        ).scalars()
        return {row.target_id: row for row in rows if row.target_id is not None}

    def get_run(self, run_id: uuid.UUID) -> ContentAnalysisRunRow | None:
        return self._s.get(ContentAnalysisRunRow, run_id)

    # ------------------------------------------------------------------- writes
    def save(
        self,
        outcome: AnalysisOutcome,
        inputs: AnalysisInputs,
        *,
        inputs_fingerprint: str,
        started_at: datetime,
        completed_at: datetime,
        model_version: str,
        priority: float = 0.0,
        attempts: int = 1,
    ) -> tuple[uuid.UUID, bool]:
        """Persist one analysis. Returns (run id, created); an identical completed analysis
        (same fingerprint) is returned without writing anything."""
        existing = self.completed_by_fingerprint(outcome.fingerprint)
        if existing is not None:
            return existing.id, False
        rep = outcome.report
        run_id = uuid.UUID(outcome.run_id)
        query_id = inputs.query_id if _exists(self._s, SearchQueryRow, inputs.query_id) else None
        from trend_agent.db.detection_models import SurgeEventRow
        from trend_agent.db.monitoring_models import MonitoringTargetRow
        from trend_agent.db.verification_models import EvidenceBundleRow

        event_id = rep.demand.surge_event_id
        bundle = uuid.UUID(outcome.bundle.bundle_id) if outcome.bundle else None
        self._s.add(
            ContentAnalysisRunRow(
                id=run_id,
                query_id=query_id,
                surge_event_id=event_id if _exists(self._s, SurgeEventRow, event_id) else None,
                target_id=inputs.target_id
                if _exists(self._s, MonitoringTargetRow, inputs.target_id)
                else None,
                trigger=rep.demand.trigger.value,
                canonical_query=inputs.query,
                category=rep.category,
                region=inputs.region,
                language=inputs.language,
                status=AnalysisRunStatus.COMPLETED,
                started_at=started_at,
                completed_at=completed_at,
                model_version=model_version,
                analysis_version=rep.analysis_version,
                configuration_version=rep.configuration_version,
                results_inspected=rep.inspected_results,
                inputs_fingerprint=inputs_fingerprint,
                fingerprint=outcome.fingerprint,
                bundle_id=bundle if _exists(self._s, EvidenceBundleRow, bundle) else None,
                demand_basis=rep.demand.basis.value,
                opportunity_status=rep.status.value,
                verification_state=rep.verification_state,
                priority=priority,
                attempts=attempts,
                cost=outcome.cost.as_dict(),
                is_synthetic=rep.synthetic,
                expires_at=outcome.expires_at,
            )
        )
        self._s.flush()
        entity_id = None
        if rep.entity_key:
            entity_id = self._s.execute(
                sa.select(DiscoveredEntityRow.id).where(
                    DiscoveredEntityRow.entity_key == rep.entity_key
                )
            ).scalar_one_or_none()
        intent = SearchIntentAnalysisRow(
            analysis_run_id=run_id,
            query_id=query_id,
            primary_intent=outcome.intent.primary_intent.value,
            sub_intent=outcome.intent.sub_intent.value if outcome.intent.sub_intent else None,
            secondary_intents=[i.value for i in outcome.intent.secondary_intents],
            entity_id=entity_id,
            entity_key=rep.entity_key,
            confidence_status=outcome.intent.confidence.value,
            specificity=outcome.intent.specificity.value,
            analysis_method=outcome.intent.method.value,
            evidence_references=[e for ref in outcome.intent.entities for e in ref.evidence_ids][
                :20
            ],
            ambiguity=list(outcome.intent.ambiguity),
            payload=outcome.intent.model_dump(mode="json"),
        )
        self._s.add(intent)
        self._s.flush()
        calc = {
            r.evidence_key.split(":")[2]: r.evidence_id
            for r in outcome.registered
            if r.evidence_key.startswith("calc:coverage:")
        }
        req_ids: dict[str, int] = {}
        for req in outcome.decomposition.requirements:
            row = InformationRequirementRow(
                analysis_run_id=run_id,
                query_id=query_id,
                intent_id=intent.id,
                key=req.key,
                requirement=req.requirement,
                priority=req.priority.value,
                cues=list(req.cues),
                source=req.source,
                requires_verified_entity=req.requires_verified_entity,
                blocking=req.blocking,
                evidence_reference=calc.get(req.key),
            )
            self._s.add(row)
            self._s.flush()
            req_ids[req.key] = row.id
        fresh = {f.video_id: f for f in outcome.freshness}
        by_id = {v.video_id: v for v in outcome.videos}
        for rel in outcome.relevance:
            v = by_id[rel.video_id]
            self._s.add(
                VideoResultAnalysisRow(
                    analysis_run_id=run_id,
                    video_id=rel.video_id,
                    variant=v.variant,
                    api_position=v.api_position,
                    relevance_score=rel.score,
                    relevant=rel.relevant,
                    intent_match=rel.intent_match,
                    entity_match=rel.entity_match.value,
                    video_format=rel.video_format.value,
                    freshness_status=fresh[rel.video_id].status.value,
                    evidence_level=rel.evidence_tier.value,
                    relevance_explanation=rel.explanation,
                    verification_status="deterministic",
                    evidence_id=v.evidence_id,
                    expires_at=v.expires_at,
                )
            )
        for cell in outcome.cells:
            if cell.requirement_key not in req_ids:
                continue
            self._s.add(
                RequirementCoverageRow(
                    analysis_run_id=run_id,
                    requirement_id=req_ids[cell.requirement_key],
                    video_id=cell.video_id,
                    coverage_status=cell.status.value,
                    evidence_level=cell.evidence_tier.value,
                    supporting_evidence=[cell.evidence_id] if cell.evidence_id else [],
                    matched_cues=list(cell.matched_cues),
                    note=cell.note,
                    expires_at=by_id[cell.video_id].expires_at,
                )
            )
        gap_ids: dict[str, int] = {}
        for g in outcome.gaps:
            row_gap = ContentGapRow(
                query_id=query_id,
                analysis_run_id=run_id,
                category=rep.category,
                gap_type=g.gap_type.value,
                evidence_strength=g.evidence_strength.value,
                explanation=g.statement,
                verification_status=g.verification_status.value,
                requirement_keys=list(g.requirement_keys),
                supporting_evidence=list(g.supporting_evidence)[:40],
                limitations=list(g.limitations),
                is_synthetic=rep.synthetic,
                expires_at=outcome.expires_at,
            )
            self._s.add(row_gap)
            self._s.flush()
            gap_ids[g.gap_type.value] = row_gap.id
        primary = rep.primary_gap.gap_type.value if rep.primary_gap else None
        OpportunityRepository(self._s).upsert(
            outcome,
            inputs,
            run_id=run_id,
            primary_gap_id=gap_ids.get(primary) if primary else None,
            query_id=query_id,
            surge_event_id=event_id if _exists(self._s, SurgeEventRow, event_id) else None,
        )
        self._s.flush()
        return run_id, True

    def record_failure(
        self,
        inputs: AnalysisInputs,
        *,
        inputs_fingerprint: str,
        error: str,
        started_at: datetime,
        model_version: str,
        analysis_version: str,
        configuration_version: str,
        attempts: int,
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        self._s.add(
            ContentAnalysisRunRow(
                id=run_id,
                query_id=inputs.query_id
                if _exists(self._s, SearchQueryRow, inputs.query_id)
                else None,
                trigger=inputs.demand.trigger.value,
                canonical_query=inputs.query,
                category=inputs.category,
                region=inputs.region,
                language=inputs.language,
                status=AnalysisRunStatus.FAILED,
                started_at=started_at,
                completed_at=None,
                model_version=model_version,
                analysis_version=analysis_version,
                configuration_version=configuration_version,
                inputs_fingerprint=inputs_fingerprint,
                demand_basis=inputs.demand.basis.value,
                attempts=attempts,
                error_details=error[:2000],
                is_synthetic=inputs.synthetic,
            )
        )
        self._s.flush()
        return run_id

    # -------------------------------------------------------------------- reads
    def runs(
        self, *, limit: int = 20, include_synthetic: bool = False, query: str | None = None
    ) -> list[ContentAnalysisRunRow]:
        stmt = sa.select(ContentAnalysisRunRow).order_by(ContentAnalysisRunRow.created_at.desc())
        if not include_synthetic:
            stmt = stmt.where(sa.not_(ContentAnalysisRunRow.is_synthetic))
        if query:
            stmt = stmt.where(sa.func.lower(ContentAnalysisRunRow.canonical_query) == query.lower())
        return list(self._s.execute(stmt.limit(limit)).scalars())

    def results(self, run_id: uuid.UUID) -> list[VideoResultAnalysisRow]:
        return list(
            self._s.execute(
                sa.select(VideoResultAnalysisRow)
                .where(VideoResultAnalysisRow.analysis_run_id == run_id)
                .order_by(VideoResultAnalysisRow.relevance_score.desc())
            ).scalars()
        )

    def intent(self, run_id: uuid.UUID) -> SearchIntentAnalysisRow | None:
        return self._s.execute(
            sa.select(SearchIntentAnalysisRow).where(
                SearchIntentAnalysisRow.analysis_run_id == run_id
            )
        ).scalar_one_or_none()

    def requirements(self, run_id: uuid.UUID) -> list[InformationRequirementRow]:
        return list(
            self._s.execute(
                sa.select(InformationRequirementRow)
                .where(InformationRequirementRow.analysis_run_id == run_id)
                .order_by(InformationRequirementRow.id)
            ).scalars()
        )

    def coverage(self, run_id: uuid.UUID) -> list[RequirementCoverageRow]:
        return list(
            self._s.execute(
                sa.select(RequirementCoverageRow).where(
                    RequirementCoverageRow.analysis_run_id == run_id
                )
            ).scalars()
        )

    def gaps(
        self,
        *,
        category: str | None = None,
        limit: int = 50,
        include_synthetic: bool = False,
        include_non_gaps: bool = False,
    ) -> list[tuple[ContentGapRow, str]]:
        g, r = ContentGapRow, ContentAnalysisRunRow
        stmt = sa.select(g, r.canonical_query).join(r, r.id == g.analysis_run_id)
        if category:
            stmt = stmt.where(g.category == category)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(g.is_synthetic))
        if not include_non_gaps:
            stmt = stmt.where(g.gap_type.not_in(["no_verifiable_gap", "insufficient_evidence"]))
        rows = self._s.execute(stmt.order_by(g.created_at.desc()).limit(limit)).all()
        return [(row[0], row[1]) for row in rows]

    def status(self, since: datetime) -> dict[str, Any]:
        r = ContentAnalysisRunRow
        by_status = {
            str(k): int(n)
            for k, n in self._s.execute(sa.select(r.status, sa.func.count()).group_by(r.status))
        }
        by_basis = {
            str(k): int(n)
            for k, n in self._s.execute(
                sa.select(r.demand_basis, sa.func.count()).group_by(r.demand_basis)
            )
        }
        today = list(self._s.execute(sa.select(r.cost).where(r.created_at >= since)).scalars())
        return {
            "runs_by_status": by_status,
            "runs_by_demand_basis": by_basis,
            "today": {
                "analyses": len(today),
                "search_requests": sum(int(c.get("search_requests", 0)) for c in today),
                "quota_units": sum(int(c.get("quota_units", 0)) for c in today),
                "llm_tokens": sum(
                    int(c.get("llm_input_tokens", 0)) + int(c.get("llm_output_tokens", 0))
                    for c in today
                ),
                "llm_usd": round(sum(float(c.get("llm_usd", 0.0)) for c in today), 4),
            },
        }

    def usage_since(self, since: datetime) -> tuple[int, int]:
        """(search requests, LLM tokens) used by analyses since ``since``."""
        s = self.status(since)["today"]
        return int(s["search_requests"]), int(s["llm_tokens"])

    # ----------------------------------------------------------------- retention
    _EXPIRING = (
        ("content_opportunities", ContentOpportunityRow),
        ("content_gaps", ContentGapRow),
        ("requirement_coverage", RequirementCoverageRow),
        ("video_result_analyses", VideoResultAnalysisRow),
    )

    def count_expired(self, now: datetime) -> dict[str, int]:
        return {
            name: int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.expires_at <= now)
                ).scalar_one()
            )
            for name, model in self._EXPIRING
        }

    def purge_expired(self, now: datetime) -> dict[str, int]:
        out = {}
        for name, model in self._EXPIRING:
            res = self._s.execute(sa.delete(model).where(model.expires_at <= now))
            out[name] = int(getattr(res, "rowcount", 0) or 0)
        # Runs stay as audit records (counts, versions, fingerprints - no third-party text).
        return out


class OpportunityRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(
        self,
        outcome: AnalysisOutcome,
        inputs: AnalysisInputs,
        *,
        run_id: uuid.UUID,
        primary_gap_id: int | None,
        query_id: int | None,
        surge_event_id: int | None,
    ) -> None:
        rep = outcome.report
        key = uuid.UUID(rep.opportunity_id)
        row = self._s.execute(
            sa.select(ContentOpportunityRow).where(ContentOpportunityRow.opportunity_key == key)
        ).scalar_one_or_none()
        values = {
            "query_id": query_id,
            "surge_event_id": surge_event_id,
            "analysis_run_id": run_id,
            "primary_gap_id": primary_gap_id,
            "canonical_query": inputs.query,
            "category": rep.category,
            "region": inputs.region,
            "language": inputs.language,
            "score": rep.score.score,
            "score_version": rep.score.score_version,
            "evidence_quality": rep.evidence_quality.value if rep.evidence_quality else None,
            "status": rep.status.value,
            "verification_state": rep.verification_state,
            "demand_basis": rep.demand.basis.value,
            "report": rep.model_dump(mode="json"),
            "is_synthetic": rep.synthetic,
            "expires_at": outcome.expires_at,
        }
        if row is None:
            self._s.add(ContentOpportunityRow(opportunity_key=key, **values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
            row.updated_at = rep.analysed_at

    def ranked(
        self,
        *,
        limit: int = 20,
        category: str | None = None,
        statuses: Sequence[str] = ("candidate",),
        include_synthetic: bool = False,
    ) -> list[ContentOpportunityRow]:
        o = ContentOpportunityRow
        stmt = sa.select(o).where(o.status.in_(list(statuses)))
        if category:
            stmt = stmt.where(o.category == category)
        if not include_synthetic:
            stmt = stmt.where(sa.not_(o.is_synthetic))
        return list(
            self._s.execute(
                stmt.order_by(o.score.desc().nulls_last(), o.updated_at.desc()).limit(limit)
            ).scalars()
        )

    def get(self, ident: str) -> ContentOpportunityRow | None:
        o = ContentOpportunityRow
        if ident.isdigit():
            return self._s.get(o, int(ident))
        try:
            key = uuid.UUID(ident)
        except ValueError:
            return None
        return self._s.execute(sa.select(o).where(o.opportunity_key == key)).scalar_one_or_none()
