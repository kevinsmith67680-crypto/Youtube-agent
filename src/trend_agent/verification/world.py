"""In-memory evidence worlds for tests, demos and the adversarial evaluation.

A world is a small, explicit description of what is true (entities, source records,
engine statistics per target). It is registered into an in-memory registry exactly like
production evidence, so the gate under test runs unchanged. World records are marked
``test_fixture``; nothing here is ever written to the production database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from trend_agent.discovery.models import EntityOrigin, EntityStatus, SearchIntent
from trend_agent.schemas.detection import BaselineReadiness, SupportStatus
from trend_agent.schemas.evidence import AlertStage, SurgeClassification
from trend_agent.schemas.monitoring import SEARCH_INTEREST_CATEGORIES, MeasurementCategory
from trend_agent.schemas.verification import (
    AuthoritativeStatistics,
    EvidenceDraft,
    EvidenceFact,
    EvidenceKind,
    EvidenceRecord,
    EvidenceSourceKind,
    FactKind,
    MeasurementSemantics,
    TrustLevel,
    semantics_for,
)
from trend_agent.utils.time import ensure_utc
from trend_agent.verification.authority import statement
from trend_agent.verification.config import LoadedVerificationConfig
from trend_agent.verification.entities import CatalogEntity, EntityCatalog
from trend_agent.verification.generation import GenerationContext
from trend_agent.verification.registry import (
    EvidenceRegistry,
    EvidenceSigner,
    MemoryEvidenceStore,
)
from trend_agent.verification.untrusted import injection_signals, sanitize


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


@dataclass
class TargetSpec:
    name: str
    query: str
    query_id: int
    target_id: int
    topic_id: int | None
    topic: str | None
    category: str | None
    intent: SearchIntent | None
    entity_keys: tuple[str, ...]
    record_keys: tuple[str, ...]
    stats: dict[str, Any] | None
    synthetic: bool = False


@dataclass
class World:
    now: datetime
    registry: EvidenceRegistry
    catalog: EntityCatalog
    records: dict[str, EvidenceRecord]
    targets: dict[str, TargetSpec]
    loaded: LoadedVerificationConfig
    surge_records: dict[str, EvidenceRecord] = field(default_factory=dict)

    def statistics(
        self, target: TargetSpec, override: dict[str, Any] | None = None
    ) -> AuthoritativeStatistics:
        spec = dict(target.stats or {})
        if override:
            spec.update(override)
        if not spec or spec.get("available") is False:
            stats = AuthoritativeStatistics(target_id=target.target_id, available=False)
            return stats.model_copy(update={"statement": statement(stats, target.query)})
        cats = tuple(MeasurementCategory(c) for c in spec.get("categories", ()))
        classification = SurgeClassification(spec["classification"])
        surge = self.surge_records.get(target.name)
        stats = AuthoritativeStatistics(
            target_id=target.target_id,
            available=True,
            classification=classification,
            highest_stage=AlertStage(spec["stage"]),
            evidence_categories=cats,
            measures_youtube_search=classification is SurgeClassification.DIRECT_SEARCH_SURGE,
            measures_search=any(c in SEARCH_INTEREST_CATEGORIES for c in cats),
            severity=spec.get("severity"),
            maximum_severity=spec.get("severity"),
            relative_change=spec.get("relative_change"),
            observed=spec.get("observed"),
            expected=spec.get("expected"),
            scale=spec.get("scale", "relative_per_request"),
            confidence_status=SupportStatus(spec.get("confidence", "empirically_thresholded")),
            baseline_readiness=BaselineReadiness(spec.get("readiness", "ready")),
            freshness=spec.get("freshness", "fresh"),
            detected_at=_dt(spec.get("detected_at")) or self.now,
            last_updated_at=_dt(spec.get("last_updated_at")) or self.now,
            event_ids=(target.target_id,),
            evidence_ids=(surge.evidence_id,) if surge else (),
            synthetic=bool(spec.get("synthetic", target.synthetic)),
        )
        return stats.model_copy(update={"statement": statement(stats, target.query)})

    def context(
        self,
        name: str,
        *,
        stats_override: dict[str, Any] | None = None,
        extra_records: tuple[str, ...] = (),
        drop_records: tuple[str, ...] = (),
        synthetic: bool | None = None,
    ) -> GenerationContext:
        t = self.targets[name]
        keys = [k for k in (*t.record_keys, *extra_records) if k not in drop_records]
        records = [self.records[k] for k in keys]
        surge = self.surge_records.get(name)
        stats = self.statistics(t, stats_override)
        if surge is not None and stats.available:
            records.append(surge)
        is_synthetic = t.synthetic if synthetic is None else synthetic
        bundle = self.registry.issue_bundle(records, target_id=t.target_id, synthetic=is_synthetic)
        return GenerationContext(
            target_id=t.target_id,
            query_id=t.query_id,
            query=t.query,
            topic_id=t.topic_id,
            topic=t.topic,
            category=t.category,
            primary_intent=t.intent,
            bundle=bundle,
            records=tuple(records),
            catalog=self.catalog,
            stats=stats,
            now=self.now,
            topic_entity_keys=frozenset(t.entity_keys),
            synthetic=is_synthetic,
        )


_KIND_SEMANTICS = {
    EvidenceKind.SEARCH_SUGGESTION: MeasurementSemantics.SEARCH_SUGGESTION_PRESENCE,
    EvidenceKind.RELATED_QUERY: MeasurementSemantics.SEARCH_SUGGESTION_PRESENCE,
    EvidenceKind.ENTITY_RECORD: MeasurementSemantics.ENTITY_REGISTRY,
    EvidenceKind.STATISTICAL_CALCULATION: MeasurementSemantics.DERIVED_STATISTIC,
}


def build_world(spec: dict[str, Any], loaded: LoadedVerificationConfig) -> World:
    now = _dt(spec["now"])
    assert now is not None
    registry = EvidenceRegistry(
        MemoryEvidenceStore(), EvidenceSigner.development(), loaded.config, clock=lambda: now
    )
    entities = []
    for e in spec.get("entities", ()):
        entities.append(
            CatalogEntity(
                key=e["key"],
                canonical_name=e["name"],
                entity_type=e.get("type", "ai_product"),
                category=e.get("category", "ai"),
                status=EntityStatus(e.get("status", "verified")),
                origin=EntityOrigin(e.get("origin", "extracted")),
                aliases=tuple(e.get("aliases", ())),
                release_date=_dt(e["release_date"]).date() if e.get("release_date") else None,  # type: ignore[union-attr]
                release_level=e.get("release_level"),
                version=e.get("version"),
                synthetic=bool(e.get("synthetic", False)),
            )
        )
    records: dict[str, EvidenceRecord] = {}
    for r in spec.get("records", ()):
        kind = EvidenceKind(r["kind"])
        synthetic = bool(r.get("synthetic", False))
        facts = [
            EvidenceFact(kind=FactKind.MENTIONS_ENTITY, subject=m) for m in r.get("mentions", ())
        ]
        for f in r.get("facts", ()):
            facts.append(
                EvidenceFact(
                    kind=FactKind(f["kind"]),
                    subject=f.get("subject"),
                    attribute=f.get("attribute"),
                    value=f.get("value"),
                    unit=f.get("unit"),
                    as_of=_dt(f.get("as_of")),
                )
            )
        published = _dt(r.get("published_at"))
        if published is not None:
            facts.append(EvidenceFact(kind=FactKind.PUBLISHED, as_of=published))
        text = r.get("text")
        semantics = (
            MeasurementSemantics.SYNTHETIC
            if synthetic
            else MeasurementSemantics(r["semantics"])
            if r.get("semantics")
            else _KIND_SEMANTICS.get(kind, MeasurementSemantics.SOURCE_STATEMENT)
        )
        draft = EvidenceDraft(
            evidence_key=r["key"],
            source_kind=EvidenceSourceKind.TEST_FIXTURE,
            provider_name=r.get("provider", "fixture"),
            source_reference=r.get("url") or f"fixture://{r['key']}",
            canonical_url=r.get("url"),
            publisher=r.get("publisher"),
            author=r.get("author"),
            published_at=published,
            collected_at=_dt(r.get("collected_at")) or published or now,
            evidence_kind=kind,
            measurement_semantics=semantics,
            trust_level=TrustLevel.SYNTHETIC if synthetic else TrustLevel(r["trust"]),
            facts=tuple(facts),
            excerpt=sanitize(text) if text else None,
            retention_note=r.get("retention", "test fixture"),
            expires_at=_dt(r.get("expires_at")),
            is_synthetic=synthetic,
            injection_flags=injection_signals(text),
        )
        records[r["key"]] = registry.register(draft)
    for e in entities:
        rec = records.get(f"entity:{e.key}")
        if rec is not None:
            entities[entities.index(e)] = CatalogEntity(
                **{**e.__dict__, "evidence_id": rec.evidence_id}
            )
    catalog = EntityCatalog(entities)
    targets: dict[str, TargetSpec] = {}
    world = World(now, registry, catalog, records, targets, loaded)
    for name, t in spec.get("targets", {}).items():
        target = TargetSpec(
            name=name,
            query=t["query"],
            query_id=int(t["query_id"]),
            target_id=int(t.get("target_id", t["query_id"])),
            topic_id=t.get("topic_id"),
            topic=t.get("topic"),
            category=t.get("category"),
            intent=SearchIntent(t["intent"]) if t.get("intent") else None,
            entity_keys=tuple(t.get("entities", ())),
            record_keys=tuple(t.get("records", ())),
            stats=t.get("stats"),
            synthetic=bool(t.get("synthetic", False)),
        )
        targets[name] = target
        st = t.get("stats")
        if st and st.get("available", True) is not False:
            cat = MeasurementCategory(st["categories"][0])
            synthetic = bool(st.get("synthetic", target.synthetic))
            surge_facts = (
                EvidenceFact(
                    kind=FactKind.SURGE,
                    subject=target.query,
                    attribute="classification",
                    value=st["classification"],
                ),
                EvidenceFact(
                    kind=FactKind.SURGE,
                    subject=target.query,
                    attribute="alert_stage",
                    value=st["stage"],
                ),
                EvidenceFact(
                    kind=FactKind.SURGE,
                    subject=target.query,
                    attribute="severity",
                    value=st.get("severity"),
                ),
                EvidenceFact(
                    kind=FactKind.SURGE,
                    subject=target.query,
                    attribute="relative_change",
                    value=st.get("relative_change"),
                ),
            )
            updated = _dt(st.get("last_updated_at")) or now
            world.surge_records[name] = registry.register(
                EvidenceDraft(
                    evidence_key=f"surge:{name}",
                    source_kind=EvidenceSourceKind.TEST_FIXTURE,
                    provider_name="trend_agent.detection",
                    source_reference=f"fixture://surge/{name}",
                    published_at=updated,
                    collected_at=updated,
                    evidence_kind=EvidenceKind.SYNTHETIC_TEST_DATA
                    if synthetic
                    else EvidenceKind.SURGE_DETECTION,
                    measurement_semantics=MeasurementSemantics.SYNTHETIC
                    if synthetic
                    else semantics_for(cat),
                    trust_level=TrustLevel.SYNTHETIC
                    if synthetic
                    else TrustLevel.AUTHORITATIVE_DATA,
                    facts=surge_facts,
                    excerpt=None,
                    retention_note="test fixture",
                    is_synthetic=synthetic,
                )
            )
    return world


def load_world(path: str | Path, loaded: LoadedVerificationConfig) -> World:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return build_world(data.get("world", data), loaded)
