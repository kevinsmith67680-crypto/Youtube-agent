"""Verification-layer tables: evidence registry, bundles, gate runs, attempts and claims.

``evidence_records`` is append-only for content: a trigger (migration 0005) rejects any
UPDATE that changes a content column; only ``is_current``, ``status`` and
``superseded_at`` may change (versioning and source removal). Records carry the retention
of their source (``expires_at``) and are purged with it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trend_agent.db.base import Base, str_enum, utc_now_sql
from trend_agent.schemas.verification import (
    AbstentionState,
    ClaimAction,
    ClaimCertainty,
    ClaimOrigin,
    ClaimStatus,
    ClaimType,
    EvidenceKind,
    EvidenceSourceKind,
    EvidenceStatus,
    MeasurementSemantics,
    PublicationDecision,
    TrustLevel,
)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=utc_now_sql(), nullable=False)


class EvidenceRecordRow(Base):
    __tablename__ = "evidence_records"
    __table_args__ = (
        sa.UniqueConstraint("evidence_key", "version", name="uq_evidence_records_key_version"),
        sa.Index(
            "uq_evidence_records_current",
            "evidence_key",
            unique=True,
            postgresql_where=sa.text("is_current"),
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("NOT (is_current AND status <> 'recorded')", name="current_is_recorded"),
        sa.CheckConstraint(
            "is_synthetic = (trust_level = 'synthetic')", name="synthetic_trust_consistent"
        ),
        sa.Index("ix_evidence_records_source", "source_kind", "source_row_id"),
        sa.Index(
            "ix_evidence_records_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(sa.String(40), unique=True)
    evidence_key: Mapped[str] = mapped_column(sa.String(200))
    version: Mapped[int] = mapped_column(sa.Integer)
    is_current: Mapped[bool] = mapped_column(sa.Boolean)
    status: Mapped[EvidenceStatus] = mapped_column(str_enum(EvidenceStatus, "evidence_status"))
    superseded_at: Mapped[datetime | None]
    source_kind: Mapped[EvidenceSourceKind] = mapped_column(
        str_enum(EvidenceSourceKind, "evidence_source_kind")
    )
    source_row_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    provider_name: Mapped[str] = mapped_column(sa.String(64))
    source_reference: Mapped[str] = mapped_column(sa.Text)
    canonical_url: Mapped[str | None] = mapped_column(sa.Text)
    publisher: Mapped[str | None] = mapped_column(sa.Text)
    author: Mapped[str | None] = mapped_column(sa.Text)
    published_at: Mapped[datetime | None]
    collected_at: Mapped[datetime]
    registered_at: Mapped[datetime]
    evidence_kind: Mapped[EvidenceKind] = mapped_column(str_enum(EvidenceKind, "evidence_kind"))
    measurement_semantics: Mapped[MeasurementSemantics] = mapped_column(
        str_enum(MeasurementSemantics, "evidence_semantics")
    )
    trust_level: Mapped[TrustLevel] = mapped_column(str_enum(TrustLevel, "evidence_trust"))
    facts: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    excerpt: Mapped[str | None] = mapped_column(sa.Text)
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    signer_key_id: Mapped[str] = mapped_column(sa.String(16))
    fresh_until: Mapped[datetime | None]
    data_policy_id: Mapped[str | None] = mapped_column(sa.String(80))
    retention_note: Mapped[str] = mapped_column(sa.Text, server_default="")
    expires_at: Mapped[datetime | None]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    injection_flags: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    created_at: Mapped[datetime] = _created()


class EvidenceBundleRow(Base):
    __tablename__ = "evidence_bundles"
    __table_args__ = (
        sa.Index("ix_evidence_bundles_target", "target_id", "issued_at"),
        sa.Index("ix_evidence_bundles_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="SET NULL")
    )
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)))
    token: Mapped[str] = mapped_column(sa.String(64))
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    created_at: Mapped[datetime] = _created()


class VerificationRunRow(Base):
    __tablename__ = "verification_runs"
    __table_args__ = (
        sa.Index("ix_verification_runs_target", "target_id", "decided_at"),
        sa.Index("ix_verification_runs_state", "abstention_state", "decided_at"),
        sa.Index(
            "ix_verification_runs_expires_at",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "publication = 'withhold' OR abstention_state IN "
            "('verified_result', 'partially_verified')",
            name="publish_only_verified",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("evidence_bundles.id", ondelete="SET NULL")
    )
    target_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_targets.id", ondelete="SET NULL")
    )
    query_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("search_queries.id", ondelete="SET NULL")
    )
    topic_cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("topic_clusters.id", ondelete="SET NULL")
    )
    generator: Mapped[str] = mapped_column(sa.String(64))
    generator_model: Mapped[str | None] = mapped_column(sa.String(80))
    attempts: Mapped[int] = mapped_column(sa.Integer)
    abstention_state: Mapped[AbstentionState] = mapped_column(
        str_enum(AbstentionState, "verification_state")
    )
    publication: Mapped[PublicationDecision] = mapped_column(
        str_enum(PublicationDecision, "verification_publication")
    )
    title: Mapped[str | None] = mapped_column(sa.Text)
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    steps: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    violations: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    missing_evidence: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    claim_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    verifier_version: Mapped[str] = mapped_column(sa.String(40))
    configuration_version: Mapped[str] = mapped_column(sa.String(80))
    is_synthetic: Mapped[bool] = mapped_column(sa.Boolean)
    decided_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created()


class VerificationAttemptRow(Base):
    __tablename__ = "verification_attempts"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "attempt", name="uq_verification_attempts_attempt"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("verification_runs.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(sa.Integer)
    generation_status: Mapped[str] = mapped_column(sa.String(16))
    parse_status: Mapped[str | None] = mapped_column(sa.String(16))
    stop_reason: Mapped[str | None] = mapped_column(sa.String(40))
    model: Mapped[str | None] = mapped_column(sa.String(80))
    response_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    detail: Mapped[str] = mapped_column(sa.Text, server_default="")
    violations: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    feedback: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")


class VerificationClaimRow(Base):
    __tablename__ = "verification_claims"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "attempt", "position", name="uq_verification_claims_pos"),
        sa.Index("ix_verification_claims_status", "status"),
        sa.Index("ix_verification_claims_fingerprint", "fingerprint"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("verification_runs.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(sa.Integer)
    position: Mapped[int] = mapped_column(sa.Integer)
    origin: Mapped[ClaimOrigin] = mapped_column(str_enum(ClaimOrigin, "claim_origin"))
    field: Mapped[str] = mapped_column(sa.String(80))
    claim_type: Mapped[ClaimType] = mapped_column(str_enum(ClaimType, "claim_type"))
    certainty: Mapped[ClaimCertainty] = mapped_column(str_enum(ClaimCertainty, "claim_certainty"))
    text: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[ClaimStatus] = mapped_column(str_enum(ClaimStatus, "claim_status"))
    action: Mapped[ClaimAction] = mapped_column(str_enum(ClaimAction, "claim_action"))
    core: Mapped[bool] = mapped_column(sa.Boolean)
    cited_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.String(80)), server_default="{}")
    supporting_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    contradicting_ids: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    reasons: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default="{}")
    violations: Mapped[list[str]] = mapped_column(ARRAY(sa.String(40)), server_default="{}")
    uncertainty_label: Mapped[str | None] = mapped_column(sa.Text)
    published_text: Mapped[str | None] = mapped_column(sa.Text)
    fingerprint: Mapped[str] = mapped_column(sa.String(24))
