"""Evidence registry storage and verification-run persistence."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.db.session import SessionFactory, transaction
from trend_agent.db.verification_models import (
    EvidenceBundleRow,
    EvidenceRecordRow,
    VerificationAttemptRow,
    VerificationClaimRow,
    VerificationRunRow,
)
from trend_agent.schemas.verification import (
    EvidenceBundle,
    EvidenceFact,
    EvidenceRecord,
    EvidenceStatus,
)

if TYPE_CHECKING:
    from trend_agent.verification.gate import GateDecision

LOCK_NAMESPACE = 0x7E51  # advisory-lock class id for evidence registration


def record_from_row(row: EvidenceRecordRow) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=row.evidence_id,
        evidence_key=row.evidence_key,
        version=row.version,
        content_hash=row.content_hash,
        status=row.status,
        is_current=row.is_current,
        registered_at=row.registered_at,
        fresh_until=row.fresh_until,
        signer_key_id=row.signer_key_id,
        source_kind=row.source_kind,
        source_row_id=row.source_row_id,
        provider_name=row.provider_name,
        source_reference=row.source_reference,
        canonical_url=row.canonical_url,
        publisher=row.publisher,
        author=row.author,
        published_at=row.published_at,
        collected_at=row.collected_at,
        evidence_kind=row.evidence_kind,
        measurement_semantics=row.measurement_semantics,
        trust_level=row.trust_level,
        facts=tuple(EvidenceFact.model_validate(f) for f in row.facts or ()),
        excerpt=row.excerpt,
        data_policy_id=row.data_policy_id,
        retention_note=row.retention_note,
        expires_at=row.expires_at,
        is_synthetic=row.is_synthetic,
        injection_flags=tuple(row.injection_flags or ()),
    )


class SqlEvidenceStore:
    """``EvidenceStore`` backed by ``evidence_records`` / ``evidence_bundles``."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def lock(self, evidence_key: str) -> None:
        self._s.execute(
            sa.text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": LOCK_NAMESPACE, "key": evidence_key},
        )

    def current(self, evidence_key: str) -> EvidenceRecord | None:
        row = self._s.execute(
            sa.select(EvidenceRecordRow).where(
                EvidenceRecordRow.evidence_key == evidence_key, EvidenceRecordRow.is_current
            )
        ).scalar_one_or_none()
        return record_from_row(row) if row else None

    def insert(self, record: EvidenceRecord, supersedes: EvidenceRecord | None) -> None:
        if supersedes is not None:
            self._s.execute(
                sa.update(EvidenceRecordRow)
                .where(EvidenceRecordRow.evidence_id == supersedes.evidence_id)
                .values(
                    is_current=False,
                    status=EvidenceStatus.SUPERSEDED,
                    superseded_at=record.registered_at,
                )
            )
            self._s.flush()
        data = record.model_dump(mode="python")
        data["facts"] = [f.model_dump(mode="json") for f in record.facts]
        data["injection_flags"] = list(record.injection_flags)
        self._s.add(EvidenceRecordRow(**data))
        self._s.flush()

    def get_many(self, evidence_ids: Iterable[str]) -> dict[str, EvidenceRecord]:
        ids = list(dict.fromkeys(evidence_ids))
        if not ids:
            return {}
        rows = self._s.execute(
            sa.select(EvidenceRecordRow).where(EvidenceRecordRow.evidence_id.in_(ids))
        ).scalars()
        return {r.evidence_id: record_from_row(r) for r in rows}

    def save_bundle(self, bundle: EvidenceBundle) -> None:
        self._s.add(
            EvidenceBundleRow(
                id=uuid.UUID(bundle.bundle_id),
                target_id=bundle.target_id,
                evidence_ids=list(bundle.evidence_ids),
                token=bundle.token,
                issued_at=bundle.issued_at,
                expires_at=bundle.expires_at,
                is_synthetic=bundle.is_synthetic,
            )
        )
        self._s.flush()

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        row = self._s.get(EvidenceBundleRow, uuid.UUID(bundle_id))
        if row is None:
            return None
        return EvidenceBundle(
            bundle_id=str(row.id),
            evidence_ids=tuple(row.evidence_ids),
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            token=row.token,
            target_id=row.target_id,
            is_synthetic=row.is_synthetic,
        )

    def mark_source_removed(self, evidence_key: str) -> int:
        result = self._s.execute(
            sa.update(EvidenceRecordRow)
            .where(
                EvidenceRecordRow.evidence_key == evidence_key,
                EvidenceRecordRow.status != EvidenceStatus.SOURCE_REMOVED,
            )
            .values(is_current=False, status=EvidenceStatus.SOURCE_REMOVED)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class VerificationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ writes
    def save(self, decision: GateDecision) -> uuid.UUID:
        rec = decision.recommendation
        ctx = decision.context
        expiries = [r.expires_at for r in ctx.records if r.expires_at is not None]
        counts = Counter(f"{v.status.value}:{v.action.value}" for v in decision.verdicts)
        run_id = uuid.UUID(decision.run_id)
        bundle_exists = self._s.get(EvidenceBundleRow, uuid.UUID(ctx.bundle.bundle_id)) is not None
        self._s.add(
            VerificationRunRow(
                id=run_id,
                bundle_id=uuid.UUID(ctx.bundle.bundle_id) if bundle_exists else None,
                target_id=ctx.target_id if self._target_exists(ctx.target_id) else None,
                query_id=ctx.query_id if self._query_exists(ctx.query_id) else None,
                topic_cluster_id=ctx.topic_id if self._topic_exists(ctx.topic_id) else None,
                generator=decision.generator,
                generator_model=decision.generator_model,
                attempts=len(decision.attempts),
                abstention_state=rec.verification_status,
                publication=rec.publication,
                title=rec.proposed_video_title,
                draft=decision.draft.model_dump(mode="json") if decision.draft else None,
                output=rec.model_dump(mode="json"),
                steps=[
                    {"name": s.name, "outcome": s.outcome, "detail": s.detail}
                    for s in decision.steps
                ],
                violations=list(rec.violations),
                missing_evidence=list(rec.missing_evidence),
                claim_counts=dict(counts),
                verifier_version=rec.verifier_version,
                configuration_version=rec.configuration_version,
                is_synthetic=rec.synthetic,
                decided_at=rec.decided_at,
                expires_at=min(expiries) if expiries else None,
            )
        )
        self._s.flush()
        for a in decision.attempts:
            self._s.add(
                VerificationAttemptRow(
                    run_id=run_id,
                    attempt=a.attempt,
                    generation_status=a.generation_status.value,
                    parse_status=a.parse_status.value if a.parse_status else None,
                    stop_reason=a.stop_reason,
                    model=a.model,
                    response_sha256=a.response_sha256,
                    detail=a.detail[:2000],
                    violations=[v.model_dump(mode="json") for v in a.violations],
                    feedback=list(a.feedback),
                )
            )
            for v in a.verdicts:
                self._s.add(
                    VerificationClaimRow(
                        run_id=run_id,
                        attempt=a.attempt,
                        position=v.index,
                        origin=v.origin,
                        field=v.field[:80],
                        claim_type=v.claim_type,
                        certainty=v.certainty,
                        text=v.text,
                        status=v.status,
                        action=v.action,
                        core=v.core,
                        cited_ids=[c[:80] for c in v.cited_ids],
                        supporting_ids=list(v.supporting_ids),
                        contradicting_ids=list(v.contradicting_ids),
                        reasons=list(v.reasons),
                        violations=[x.value for x in v.violations],
                        uncertainty_label=v.uncertainty_label,
                        published_text=v.published_text,
                        fingerprint=v.fingerprint,
                    )
                )
        self._s.flush()
        return run_id

    def _target_exists(self, target_id: int | None) -> bool:
        if target_id is None:
            return False
        from trend_agent.db.monitoring_models import MonitoringTargetRow

        return self._s.get(MonitoringTargetRow, target_id) is not None

    def _query_exists(self, query_id: int | None) -> bool:
        if query_id is None:
            return False
        from trend_agent.db.models import SearchQueryRow

        return self._s.get(SearchQueryRow, query_id) is not None

    def _topic_exists(self, topic_id: int | None) -> bool:
        if topic_id is None:
            return False
        from trend_agent.db.discovery_models import TopicClusterRow

        return self._s.get(TopicClusterRow, topic_id) is not None

    # ------------------------------------------------------------------- reads
    def runs(
        self, *, target_id: int | None = None, limit: int = 20, include_synthetic: bool = False
    ) -> list[VerificationRunRow]:
        q = sa.select(VerificationRunRow).order_by(VerificationRunRow.decided_at.desc())
        if target_id is not None:
            q = q.where(VerificationRunRow.target_id == target_id)
        if not include_synthetic:
            q = q.where(sa.not_(VerificationRunRow.is_synthetic))
        return list(self._s.execute(q.limit(limit)).scalars())

    def run(self, run_id: str) -> VerificationRunRow | None:
        return self._s.get(VerificationRunRow, uuid.UUID(run_id))

    def claims(self, run_id: str) -> list[VerificationClaimRow]:
        return list(
            self._s.execute(
                sa.select(VerificationClaimRow)
                .where(VerificationClaimRow.run_id == uuid.UUID(run_id))
                .order_by(VerificationClaimRow.attempt, VerificationClaimRow.position)
            ).scalars()
        )

    def evidence(
        self, *, key_prefix: str | None = None, current_only: bool = True, limit: int = 50
    ) -> list[EvidenceRecord]:
        q = sa.select(EvidenceRecordRow).order_by(EvidenceRecordRow.registered_at.desc())
        if key_prefix:
            q = q.where(EvidenceRecordRow.evidence_key.startswith(key_prefix))
        if current_only:
            q = q.where(EvidenceRecordRow.is_current)
        return [record_from_row(r) for r in self._s.execute(q.limit(limit)).scalars()]

    def stats(self) -> dict[str, Any]:
        def count(stmt: Any) -> dict[str, int]:
            return {str(k): int(n) for k, n in self._s.execute(stmt).all()}

        return {
            "evidence_by_status": count(
                sa.select(EvidenceRecordRow.status, sa.func.count()).group_by(
                    EvidenceRecordRow.status
                )
            ),
            "evidence_by_trust": count(
                sa.select(EvidenceRecordRow.trust_level, sa.func.count())
                .where(EvidenceRecordRow.is_current)
                .group_by(EvidenceRecordRow.trust_level)
            ),
            "runs_by_state": count(
                sa.select(VerificationRunRow.abstention_state, sa.func.count()).group_by(
                    VerificationRunRow.abstention_state
                )
            ),
            "claims_by_status": count(
                sa.select(VerificationClaimRow.status, sa.func.count()).group_by(
                    VerificationClaimRow.status
                )
            ),
        }

    # --------------------------------------------------------------- retention
    def count_expired(self, now: datetime) -> dict[str, int]:
        def n(model: Any) -> int:
            return int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.expires_at <= now)
                ).scalar_one()
            )

        return {
            "verification_runs": n(VerificationRunRow),
            "evidence_records": n(EvidenceRecordRow),
        }

    def purge_expired(self, now: datetime) -> dict[str, int]:
        out = {}
        result = self._s.execute(
            sa.delete(VerificationRunRow).where(VerificationRunRow.expires_at <= now)
        )
        out["verification_runs"] = int(getattr(result, "rowcount", 0) or 0)
        # Bundles are audit records of what a generator was shown: kept while a run uses them.
        used = sa.select(VerificationRunRow.id).where(
            VerificationRunRow.bundle_id == EvidenceBundleRow.id
        )
        result = self._s.execute(
            sa.delete(EvidenceBundleRow).where(
                EvidenceBundleRow.expires_at <= now, ~sa.exists(used)
            )
        )
        out["evidence_bundles"] = int(getattr(result, "rowcount", 0) or 0)
        result = self._s.execute(
            sa.delete(EvidenceRecordRow).where(EvidenceRecordRow.expires_at <= now)
        )
        out["evidence_records"] = int(getattr(result, "rowcount", 0) or 0)
        return out


class SqlVerificationStore:
    """``VerificationStore`` that persists each gate decision in its own transaction."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def save(self, decision: GateDecision) -> None:
        with transaction(self._factory) as session:
            VerificationRepository(session).save(decision)
