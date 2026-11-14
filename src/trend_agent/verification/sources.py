"""Turn stored Phase 1-4 records into evidence drafts with deterministic facts.

Facts are copied from database columns and from the Phase 4 alert contract by code; no
fact is ever taken from generator output. Source text (titles, descriptions, excerpts)
travels as an untrusted excerpt with injection flags attached.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveryItemRow,
)
from trend_agent.db.monitoring_models import ObservationRow, SeriesRow
from trend_agent.discovery.models import EntityOrigin, EntityStatus, EvidenceType
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.schemas.surge import SurgeAlert
from trend_agent.schemas.verification import (
    EvidenceDraft,
    EvidenceFact,
    EvidenceKind,
    EvidenceSourceKind,
    FactKind,
    MeasurementSemantics,
    TrustLevel,
    semantics_for,
)
from trend_agent.verification.untrusted import injection_signals, sanitize

_ITEM_KIND: dict[EvidenceType, EvidenceKind] = {
    EvidenceType.OFFICIAL_ANNOUNCEMENT: EvidenceKind.OFFICIAL_ANNOUNCEMENT,
    EvidenceType.CODE_RELEASE: EvidenceKind.CODE_RELEASE,
    EvidenceType.REPOSITORY_METADATA: EvidenceKind.REPOSITORY_METADATA,
    EvidenceType.GAME_CATALOG: EvidenceKind.GAME_CATALOG,
    EvidenceType.VIDEO_TITLE: EvidenceKind.VIDEO_METADATA,
    EvidenceType.VIDEO_DESCRIPTION: EvidenceKind.VIDEO_METADATA,
    EvidenceType.COMMUNITY_DISCUSSION: EvidenceKind.COMMUNITY_POST,
    EvidenceType.RELATED_QUERY: EvidenceKind.RELATED_QUERY,
    EvidenceType.SEARCH_SUGGESTION: EvidenceKind.SEARCH_SUGGESTION,
    EvidenceType.CURATED_SEED: EvidenceKind.CURATED_REFERENCE,
    EvidenceType.SYNTHETIC_TEST_DATA: EvidenceKind.SYNTHETIC_TEST_DATA,
}

METRIC_NAME: dict[MeasurementCategory, str] = {
    MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST: "youtube_search_interest_index",
    MeasurementCategory.EXTERNAL_SEARCH_INTEREST: "external_search_interest_index",
    MeasurementCategory.SEARCH_SUGGESTION_PRESENCE: "suggestion_presence",
    MeasurementCategory.VIDEO_ACTIVITY: "video_activity",
    MeasurementCategory.OFFICIAL_EVENT_SIGNAL: "official_event_count",
    MeasurementCategory.COMMUNITY_ACTIVITY: "community_activity",
}


def retention_note(expires_at: datetime | None, policy: str | None = None) -> str:
    if expires_at is None:
        return f"no retention limit (policy {policy})" if policy else "no retention limit"
    suffix = f" under policy {policy}" if policy else ""
    return f"must be deleted with its source by {expires_at.isoformat()}{suffix}"


def item_trust(
    evidence_type: EvidenceType, *, synthetic: bool, source_verified: bool
) -> TrustLevel:
    if synthetic or evidence_type is EvidenceType.SYNTHETIC_TEST_DATA:
        return TrustLevel.SYNTHETIC
    if evidence_type in (EvidenceType.OFFICIAL_ANNOUNCEMENT, EvidenceType.CODE_RELEASE):
        return TrustLevel.OFFICIAL if source_verified else TrustLevel.PUBLISHER
    if evidence_type is EvidenceType.GAME_CATALOG:
        # The platform verifies the game's name and ID; it is not an official announcement.
        return TrustLevel.PUBLISHER if source_verified else TrustLevel.USER_GENERATED
    if evidence_type is EvidenceType.REPOSITORY_METADATA:
        # GitHub verifies the repository exists under this name; the description is the
        # owner's own claim, so it is publisher-level at best.
        return TrustLevel.PUBLISHER if source_verified else TrustLevel.USER_GENERATED
    if evidence_type is EvidenceType.CURATED_SEED:
        return TrustLevel.CURATED
    if evidence_type in (EvidenceType.RELATED_QUERY, EvidenceType.SEARCH_SUGGESTION):
        return TrustLevel.PROVIDER_SIGNAL
    return TrustLevel.USER_GENERATED


def from_discovery_item(
    row: DiscoveryItemRow,
    *,
    mentions: Sequence[tuple[str, str]] = (),
    source_verified: bool = False,
    max_chars: int = 4000,
) -> EvidenceDraft:
    """``mentions`` are (canonical entity name, matched text) pairs from entity_mentions."""
    kind = _ITEM_KIND[row.evidence_type]
    text = row.title if not row.excerpt else f"{row.title.rstrip('.')}. {row.excerpt}"
    facts: list[EvidenceFact] = []
    if row.published_at is not None:
        facts.append(EvidenceFact(kind=FactKind.PUBLISHED, as_of=row.published_at))
    for name, matched in mentions:
        facts.append(EvidenceFact(kind=FactKind.MENTIONS_ENTITY, subject=name, value=matched))
    for q in row.observed_queries or ():
        facts.append(EvidenceFact(kind=FactKind.OBSERVED_QUERY, value=q))
    semantics = (
        MeasurementSemantics.SEARCH_SUGGESTION_PRESENCE
        if kind in (EvidenceKind.SEARCH_SUGGESTION, EvidenceKind.RELATED_QUERY)
        else MeasurementSemantics.SYNTHETIC
        if row.is_synthetic
        else MeasurementSemantics.SOURCE_STATEMENT
    )
    return EvidenceDraft(
        evidence_key=f"discovery_item:{row.id}",
        source_kind=EvidenceSourceKind.DISCOVERY_ITEM,
        source_row_id=row.id,
        provider_name=row.provider_name,
        source_reference=row.source_reference,
        canonical_url=row.canonical_url or row.url,
        publisher=row.publisher,
        author=row.author,
        published_at=row.published_at,
        collected_at=row.collected_at,
        evidence_kind=kind,
        measurement_semantics=semantics,
        trust_level=item_trust(
            row.evidence_type, synthetic=row.is_synthetic, source_verified=source_verified
        ),
        facts=tuple(facts),
        excerpt=sanitize(text, max_chars),
        retention_note=retention_note(row.expires_at),
        expires_at=row.expires_at,
        is_synthetic=row.is_synthetic,
        injection_flags=injection_signals(text),
    )


def entity_trust(status: EntityStatus, origin: EntityOrigin, synthetic: bool) -> TrustLevel:
    if synthetic:
        return TrustLevel.SYNTHETIC
    if status is EntityStatus.VERIFIED and origin is EntityOrigin.GAZETTEER:
        return TrustLevel.CURATED
    if status is EntityStatus.VERIFIED:
        return TrustLevel.PUBLISHER
    return TrustLevel.USER_GENERATED


def from_entity(row: DiscoveredEntityRow, aliases: Iterable[str] = ()) -> EvidenceDraft:
    facts = [
        EvidenceFact(
            kind=FactKind.ENTITY,
            subject=row.canonical_name,
            attribute=row.entity_type.value,
            value=row.category.value,
        ),
        EvidenceFact(
            kind=FactKind.ENTITY_STATUS, subject=row.canonical_name, value=row.status.value
        ),
    ]
    for alias in sorted(set(aliases)):
        facts.append(
            EvidenceFact(
                kind=FactKind.ENTITY, subject=row.canonical_name, attribute="alias", value=alias
            )
        )
    if row.release_date is not None:
        facts.append(
            EvidenceFact(
                kind=FactKind.RELEASE_DATE,
                subject=row.canonical_name,
                value=row.release_date.date().isoformat(),
                as_of=row.release_date,
            )
        )
    if row.release_level:
        facts.append(
            EvidenceFact(
                kind=FactKind.RELEASE_LEVEL, subject=row.canonical_name, value=row.release_level
            )
        )
    if row.version:
        facts.append(
            EvidenceFact(kind=FactKind.VERSION, subject=row.canonical_name, value=row.version)
        )
    return EvidenceDraft(
        evidence_key=f"entity:{row.entity_key}",
        source_kind=EvidenceSourceKind.DISCOVERED_ENTITY,
        source_row_id=row.id,
        provider_name="trend_agent.discovery",
        source_reference=f"discovered_entities/{row.id}",
        published_at=row.first_published_at,
        collected_at=row.last_seen_at,
        evidence_kind=EvidenceKind.ENTITY_RECORD,
        measurement_semantics=MeasurementSemantics.ENTITY_REGISTRY,
        trust_level=entity_trust(row.status, row.origin, row.is_synthetic),
        facts=tuple(facts),
        excerpt=None,
        retention_note=retention_note(None),
        is_synthetic=row.is_synthetic,
    )


def from_observation(obs: ObservationRow, series: SeriesRow, query: str) -> EvidenceDraft:
    category = obs.simulated_category or obs.measurement_category
    synthetic = obs.is_synthetic
    metric = METRIC_NAME.get(category, category.value)
    fact = EvidenceFact(
        kind=FactKind.MEASUREMENT,
        subject=query,
        attribute=metric,
        value=obs.value,
        unit=obs.unit,
        as_of=obs.period_end,
        qualifier=(
            f"region={obs.region};granularity={obs.granularity.value};"
            f"scale={obs.scale_type.value};period_start={obs.period_start.isoformat()}"
        ),
    )
    return EvidenceDraft(
        # Keyed by period, so a provider revision becomes a new version of the same evidence.
        evidence_key=f"ts_obs:{series.id}:{obs.period_start.isoformat()}",
        source_kind=EvidenceSourceKind.TS_OBSERVATION,
        source_row_id=obs.id,
        provider_name=obs.provider_name,
        source_reference=obs.source_reference,
        published_at=obs.provider_published_at,
        collected_at=obs.collected_at,
        evidence_kind=EvidenceKind.SYNTHETIC_TEST_DATA if synthetic else EvidenceKind.MEASUREMENT,
        measurement_semantics=(
            MeasurementSemantics.SYNTHETIC
            if synthetic
            else MeasurementSemantics.VIDEO_ACTIVITY
            if obs.is_proxy and category is not MeasurementCategory.VIDEO_ACTIVITY
            else semantics_for(category)
        ),
        trust_level=TrustLevel.SYNTHETIC if synthetic else TrustLevel.AUTHORITATIVE_DATA,
        facts=(fact,),
        data_policy_id=obs.data_policy_id,
        retention_note=retention_note(obs.expires_at, obs.data_policy_id),
        expires_at=obs.expires_at,
        is_synthetic=synthetic,
    )


def surge_facts(alert: SurgeAlert) -> tuple[EvidenceFact, ...]:
    subject = alert.query
    at = alert.last_updated_at

    def f(
        attribute: str, value: str | float | bool | None, unit: str | None = None
    ) -> EvidenceFact:
        return EvidenceFact(
            kind=FactKind.SURGE,
            subject=subject,
            attribute=attribute,
            value=value,
            unit=unit,
            as_of=at,
        )

    return (
        f("classification", alert.classification.value),
        f("alert_stage", alert.alert_status.value),
        f("measurement_category", alert.measurement_type.value),
        f("is_proxy", alert.is_proxy),
        f("severity", alert.severity, "index_0_100"),
        f("maximum_severity", alert.maximum_severity, "index_0_100"),
        f("relative_change", alert.baseline.relative_difference, "ratio"),
        f("observed", alert.baseline.observed, alert.baseline.unit),
        f("expected", alert.baseline.expected, alert.baseline.unit),
        f("confidence_status", alert.confidence_status.value),
        f("baseline_readiness", alert.baseline.readiness.value),
        f("freshness", alert.freshness),
        f("detected_at", alert.detected_at.isoformat()),
    )


def from_surge_alert(alert: SurgeAlert, *, expires_at: datetime | None = None) -> EvidenceDraft:
    category = alert.measurement_type
    return EvidenceDraft(
        evidence_key=f"surge_event:{alert.event_id}",
        source_kind=EvidenceSourceKind.SURGE_EVENT,
        source_row_id=alert.event_id,
        provider_name=alert.provider,
        source_reference=f"surge_events/{alert.event_id}",
        published_at=alert.last_updated_at,
        collected_at=alert.last_updated_at,
        evidence_kind=EvidenceKind.SYNTHETIC_TEST_DATA
        if alert.synthetic
        else EvidenceKind.SURGE_DETECTION,
        measurement_semantics=(
            MeasurementSemantics.SYNTHETIC
            if alert.synthetic
            else MeasurementSemantics.VIDEO_ACTIVITY
            if alert.is_proxy and category is not MeasurementCategory.VIDEO_ACTIVITY
            else semantics_for(category)
        ),
        trust_level=TrustLevel.SYNTHETIC if alert.synthetic else TrustLevel.AUTHORITATIVE_DATA,
        facts=surge_facts(alert),
        excerpt=alert.explanation,
        retention_note=retention_note(expires_at),
        expires_at=expires_at,
        is_synthetic=alert.synthetic,
    )


def from_calculation(
    name: str,
    inputs: dict[str, Any],
    value: float,
    *,
    subject: str,
    unit: str | None,
    computed_at: datetime,
    input_evidence: Sequence[str] = (),
) -> EvidenceDraft:
    """A deterministic computation over stored data (inputs are recorded with it)."""
    digest = hashlib.sha256(
        json.dumps({"name": name, "inputs": inputs}, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return EvidenceDraft(
        evidence_key=f"calc:{name}:{digest}",
        source_kind=EvidenceSourceKind.CALCULATION,
        provider_name="trend_agent.verification",
        source_reference=f"calculation/{name}?inputs={','.join(input_evidence)}",
        collected_at=computed_at,
        published_at=computed_at,
        evidence_kind=EvidenceKind.STATISTICAL_CALCULATION,
        measurement_semantics=MeasurementSemantics.DERIVED_STATISTIC,
        trust_level=TrustLevel.AUTHORITATIVE_DATA,
        facts=(
            EvidenceFact(
                kind=FactKind.STATISTIC, subject=subject, attribute=name, value=value, unit=unit
            ),
        ),
        retention_note=retention_note(None),
    )
