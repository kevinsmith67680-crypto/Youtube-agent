"""Verification of opportunity-report claims (application-side approval only).

Every claim cites evidence IDs. For each claim the gate:

1. resolves the IDs in the registry and checks they belong to this analysis' bundle
   (fabricated or substituted references fail);
2. checks the record's identity, integrity and expiry (registry resolution);
3. checks freshness and relevance (FACT claims via the verification layer's claim
   verifier: entities, dates, release wording, contradictions, staleness);
4. checks the exact assertion - OBSERVATION and HYPOTHESIS numbers must equal facts of
   the cited calculation records; NARRATIVE sentences (LLM) go through the full claim
   verifier and a metadata-overreach check (no "the video explains/omits ..." when only
   titles and descriptions were inspected, no "watched");
5. applies a status: VERIFIED, CORROBORATED, UNVERIFIED, CONTRADICTED or
   INSUFFICIENT_EVIDENCE (no evidence cited at all).

The LLM never marks its own claims; unpublished claims are kept for audit only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trend_agent.schemas.content_opportunity import (
    ReportClaim,
    ReportClaimKind,
    ReportClaimStatus,
)
from trend_agent.schemas.search_result import EvidenceTier
from trend_agent.schemas.verification import (
    AuthoritativeStatistics,
    ClaimAction,
    ClaimOrigin,
    ClaimStatus,
    EvidenceBundle,
    EvidenceKind,
    FactKind,
)
from trend_agent.verification import claims as cl
from trend_agent.verification.config import VerificationConfig
from trend_agent.verification.entities import EntityCatalog, RecordView
from trend_agent.verification.registry import EvidenceRegistry
from trend_agent.verification.verifier import ClaimVerifier, VerificationInputs

_CONTENT_CLAIM = re.compile(
    r"\b(?:videos?|tutorials?|guides?|results?|creators?|it|they)\b.{0,60}\b(?:explains?|shows?|"
    r"covers?|walks? through|demonstrates?|teaches|fails? to|does(?:n'?t| not) (?:explain|show|"
    r"cover|mention|include)|omits?|misses|ignores?|skips?|lacks?)\b",
    re.IGNORECASE | re.DOTALL,
)
_HEDGE = re.compile(
    r"\b(?:titles?|descriptions?|metadata|appears?|suggests?|seems?|according to the "
    r"(?:title|description))\b",
    re.IGNORECASE,
)
_WATCHED = re.compile(r"\b(?:watched|after watching|i viewed|having viewed)\b", re.IGNORECASE)

_STATUS = {
    ClaimStatus.VERIFIED: ReportClaimStatus.VERIFIED,
    ClaimStatus.CORROBORATED: ReportClaimStatus.CORROBORATED,
    ClaimStatus.UNVERIFIED: ReportClaimStatus.UNVERIFIED,
    ClaimStatus.CONTRADICTED: ReportClaimStatus.CONTRADICTED,
}


@dataclass(frozen=True)
class DraftClaim:
    kind: ReportClaimKind
    text: str
    evidence_ids: tuple[str, ...]
    label: str | None = None


def _numbers(text: str) -> list[float]:
    return [n.value for n in cl.numbers(text) if n.kind is not cl.NumberKind.STATISTIC]


def verify_claims(
    claims: Sequence[DraftClaim],
    *,
    registry: EvidenceRegistry,
    bundle: EvidenceBundle,
    views: Mapping[str, RecordView],
    catalog: EntityCatalog,
    stats: AuthoritativeStatistics,
    query: str,
    now: datetime,
    verification_config: VerificationConfig,
    max_tier: EvidenceTier,
    allow_synthetic: bool,
) -> list[ReportClaim]:
    verifier = ClaimVerifier(
        VerificationInputs(
            views=dict(views),
            catalog=catalog,
            stats=stats,
            query=query,
            now=now,
            config=verification_config,
        )
    )
    out: list[ReportClaim] = []
    for c in claims:
        if not c.evidence_ids:
            out.append(
                ReportClaim(
                    kind=c.kind,
                    text=c.text,
                    evidence_ids=(),
                    status=ReportClaimStatus.INSUFFICIENT_EVIDENCE,
                    published=False,
                    reasons=("no evidence cited",),
                )
            )
            continue
        res = registry.resolve(c.evidence_ids, bundle, allow_synthetic=allow_synthetic, now=now)
        problems = [f"{p.evidence_id}: {p.code.value}" for p in res.problems]
        if problems:
            out.append(
                ReportClaim(
                    kind=c.kind,
                    text=c.text,
                    evidence_ids=c.evidence_ids,
                    status=ReportClaimStatus.UNVERIFIED,
                    published=False,
                    reasons=tuple(problems),
                )
            )
            continue
        records = list(res.records.values())
        if c.kind in (ReportClaimKind.OBSERVATION, ReportClaimKind.HYPOTHESIS):
            facts = {
                float(f.value)
                for r in records
                if r.evidence_kind is EvidenceKind.STATISTICAL_CALCULATION
                for f in r.facts
                if f.kind is FactKind.STATISTIC and isinstance(f.value, int | float)
            }
            missing = [n for n in _numbers(c.text) if not any(abs(n - v) < 1e-9 for v in facts)]
            if missing:
                out.append(
                    ReportClaim(
                        kind=c.kind,
                        text=c.text,
                        evidence_ids=c.evidence_ids,
                        status=ReportClaimStatus.UNVERIFIED,
                        published=False,
                        reasons=(f"numbers not in the cited observations: {missing}",),
                    )
                )
                continue
            out.append(
                ReportClaim(
                    kind=c.kind,
                    text=c.text,
                    evidence_ids=c.evidence_ids,
                    status=ReportClaimStatus.VERIFIED,
                    published=True,
                    reasons=("reproduced from deterministic observations",),
                    label=c.label,
                )
            )
            continue
        if c.kind is ReportClaimKind.NARRATIVE:
            if _WATCHED.search(c.text):
                out.append(_rejected(c, "claims to have watched videos"))
                continue
            if (
                max_tier is EvidenceTier.A_METADATA
                and _CONTENT_CLAIM.search(c.text)
                and not _HEDGE.search(c.text)
            ):
                out.append(_rejected(c, "states video contents, but only metadata was inspected"))
                continue
        verdict = verifier.verify(
            cl.CandidateClaim(
                index=len(out),
                origin=ClaimOrigin.DECLARED,
                field=c.kind.value,
                text=c.text,
                cited_ids=c.evidence_ids,
                features=cl.extract(c.text),
            )
        )
        published = verdict.action in (
            ClaimAction.KEEP,
            ClaimAction.RECITE,
            ClaimAction.KEEP_ATTRIBUTED,
        ) or (verdict.action is ClaimAction.NOT_FACTUAL and c.kind is ReportClaimKind.NARRATIVE)
        out.append(
            ReportClaim(
                kind=c.kind,
                text=verdict.published_text or c.text,
                evidence_ids=verdict.supporting_ids or c.evidence_ids,
                status=_STATUS[verdict.status]
                if verdict.action is not ClaimAction.NOT_FACTUAL
                else ReportClaimStatus.UNVERIFIED,
                published=published,
                reasons=verdict.reasons[:3],
                label=verdict.uncertainty_label or c.label,
            )
        )
    return out


def _rejected(c: DraftClaim, reason: str) -> ReportClaim:
    return ReportClaim(
        kind=c.kind,
        text=c.text,
        evidence_ids=c.evidence_ids,
        status=ReportClaimStatus.UNVERIFIED,
        published=False,
        reasons=(reason,),
    )
