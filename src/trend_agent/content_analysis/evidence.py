"""Evidence records for Phase 5: video metadata and deterministic observations.

Video metadata is registered in the existing evidence registry (no parallel store):
key ``youtube_video:<id>``, kind VIDEO_METADATA, trust USER_GENERATED, retention copied
from the stored YouTube record (deleted with it, at most 30 days). Observations computed
by the analysis (counts of inspected/relevant results, coverage tallies) are registered
as STATISTICAL_CALCULATION records so every number in a report has a citable source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from trend_agent.schemas.search_result import CandidateVideo
from trend_agent.schemas.verification import (
    EvidenceDraft,
    EvidenceFact,
    EvidenceKind,
    EvidenceSourceKind,
    FactKind,
    MeasurementSemantics,
    TrustLevel,
)

YOUTUBE_RETENTION = (
    "YouTube API Services Developer Policies: delete or refresh within 30 days; expires with "
    "the stored video record"
)


def video_draft(video: CandidateVideo) -> EvidenceDraft:
    text = (
        video.title if not video.description else f"{video.title.rstrip('.')}. {video.description}"
    )
    facts = []
    if video.published_at is not None:
        facts.append(EvidenceFact(kind=FactKind.PUBLISHED, as_of=video.published_at))
    if video.duration_seconds is not None:
        facts.append(
            EvidenceFact(
                kind=FactKind.STATISTIC,
                subject=video.video_id,
                attribute="duration_seconds",
                value=float(video.duration_seconds),
                unit="seconds",
            )
        )
    synthetic = video.synthetic
    return EvidenceDraft(
        evidence_key=f"youtube_video:{video.video_id}",
        source_kind=EvidenceSourceKind.TEST_FIXTURE
        if synthetic
        else EvidenceSourceKind.DISCOVERY_ITEM,
        provider_name="fixture" if synthetic else "youtube_data_api",
        source_reference=video.url,
        canonical_url=video.url,
        publisher=video.channel_title,
        published_at=video.published_at,
        collected_at=video.collected_at,
        evidence_kind=EvidenceKind.SYNTHETIC_TEST_DATA
        if synthetic
        else EvidenceKind.VIDEO_METADATA,
        measurement_semantics=(
            MeasurementSemantics.SYNTHETIC if synthetic else MeasurementSemantics.SOURCE_STATEMENT
        ),
        trust_level=TrustLevel.SYNTHETIC if synthetic else TrustLevel.USER_GENERATED,
        facts=tuple(facts),
        excerpt=text,
        retention_note=YOUTUBE_RETENTION,
        expires_at=video.expires_at,
        is_synthetic=synthetic,
        injection_flags=video.injection_flags,
    )


def observation_draft(
    name: str,
    values: Mapping[str, float | int],
    *,
    subject: str,
    computed_at: datetime,
    inputs: Sequence[str],
    expires_at: datetime | None,
    synthetic: bool,
) -> EvidenceDraft:
    """A deterministic observation over inspected evidence (counts, shares)."""
    digest = hashlib.sha256(
        json.dumps({"n": name, "v": dict(values), "i": sorted(inputs)}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return EvidenceDraft(
        evidence_key=f"calc:{name}:{digest}",
        source_kind=EvidenceSourceKind.CALCULATION,
        provider_name="trend_agent.content_analysis",
        source_reference=f"calculation/{name}",
        published_at=computed_at,
        collected_at=computed_at,
        evidence_kind=EvidenceKind.STATISTICAL_CALCULATION,
        measurement_semantics=MeasurementSemantics.DERIVED_STATISTIC,
        trust_level=TrustLevel.SYNTHETIC if synthetic else TrustLevel.AUTHORITATIVE_DATA,
        facts=tuple(
            EvidenceFact(kind=FactKind.STATISTIC, subject=subject, attribute=k, value=float(v))
            for k, v in sorted(values.items())
        ),
        excerpt=None,
        retention_note="derived from inspected results; expires with the earliest of them",
        expires_at=expires_at,
        is_synthetic=synthetic,
    )
