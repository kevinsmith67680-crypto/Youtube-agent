"""Verification service: assemble evidence for a monitored target and run the gate.

Data flow for ``recommend(target_id)``:

1. **Read** (read-only transaction): the target, its query, keyword, topic, the Phase 2
   source items and entities linked to it, the latest current Phase 3 observations and
   the open Phase 4 surge episodes (their validated alert contracts).
2. **Register** (application principal only): every record becomes a versioned, signed
   evidence entry; a bundle of the IDs is issued for this request.
3. **Gate**: the generator drafts, the gate verifies, abstains or publishes, and the
   decision is persisted. The generator never touches the database.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.config import Settings
from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
    DiscoverySourceRow,
    EntityAliasRow,
    EntityMentionRow,
    TopicClusterRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import MonitoringTargetRow, ObservationRow, SeriesRow
from trend_agent.db.repositories.surge_events import SurgeEventRepository
from trend_agent.db.repositories.verification import (
    SqlEvidenceStore,
    SqlVerificationStore,
    VerificationRepository,
)
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.discovery.models import EntityStatus, SearchIntent
from trend_agent.schemas.evidence import STAGE_RANK, SurgeClassification, combine
from trend_agent.schemas.surge import SurgeAlert, TargetSurgeView
from trend_agent.schemas.verification import (
    EvidenceBundle,
    EvidenceDraft,
    EvidenceRecord,
)
from trend_agent.utils.time import Clock, utc_now
from trend_agent.verification.authority import build_statistics
from trend_agent.verification.config import LoadedVerificationConfig
from trend_agent.verification.entities import CatalogEntity, EntityCatalog
from trend_agent.verification.gate import GateDecision, VerificationGate
from trend_agent.verification.generation import (
    GenerationContext,
    GenerationResult,
    GenerationStatus,
    RecommendationGenerator,
    ScriptedGenerator,
    TemplateGenerator,
)
from trend_agent.verification.permissions import (
    Capability,
    PermissionPolicy,
    Principal,
    read_only,
)
from trend_agent.verification.registry import EvidenceRegistry, EvidenceSigner
from trend_agent.verification.sources import (
    from_discovery_item,
    from_entity,
    from_observation,
    from_surge_alert,
)


class FactoryEvidenceStore:
    """Read path of the registry for the gate: one short transaction per call."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def lock(self, evidence_key: str) -> None:  # pragma: no cover - read-only use
        raise PermissionError("the gate's registry view is read-only")

    def current(self, evidence_key: str) -> EvidenceRecord | None:
        with transaction(self._factory) as s:
            read_only(s)
            return SqlEvidenceStore(s).current(evidence_key)

    def insert(self, record: EvidenceRecord, supersedes: EvidenceRecord | None) -> None:
        raise PermissionError("the gate's registry view is read-only")

    def get_many(self, evidence_ids: Iterable[str]) -> dict[str, EvidenceRecord]:
        with transaction(self._factory) as s:
            read_only(s)
            return SqlEvidenceStore(s).get_many(evidence_ids)

    def save_bundle(self, bundle: EvidenceBundle) -> None:
        raise PermissionError("the gate's registry view is read-only")

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        with transaction(self._factory) as s:
            read_only(s)
            return SqlEvidenceStore(s).get_bundle(bundle_id)

    def mark_source_removed(self, evidence_key: str) -> int:
        raise PermissionError("the gate's registry view is read-only")


@dataclass
class _Snapshot:
    target: MonitoringTargetRow
    query: str
    topic: str | None
    intent: SearchIntent | None
    drafts: list[EvidenceDraft]
    entities: list[tuple[DiscoveredEntityRow, list[str]]]
    topic_entity_keys: set[str]
    alerts: list[SurgeAlert]
    synthetic: bool
    unavailable: list[str]


def target_view(alerts: list[SurgeAlert]) -> TargetSurgeView | None:
    """Same combination rule as ``DetectionService.target_views`` for one target."""
    if not alerts:
        return None
    cats = tuple(sorted({a.measurement_type for a in alerts}, key=lambda m: m.value))
    combined = combine(cats)
    ranked = [a.alert_status for a in alerts if a.alert_status in STAGE_RANK]
    top = max(ranked, key=lambda s: STAGE_RANK[s]) if ranked else None
    statement = (
        "Several indirect signals rose together; not measured YouTube search growth."
        if combined is SurgeClassification.POTENTIAL_YOUTUBE_DEMAND_OPPORTUNITY
        else "Direct YouTube search-interest evidence is present."
        if combined is SurgeClassification.DIRECT_SEARCH_SURGE
        else "A single evidence category shows an increase."
    )
    return TargetSurgeView(
        target_id=alerts[0].target_id,
        query=alerts[0].query,
        category=alerts[0].category,
        topic_cluster_id=alerts[0].topic_cluster_id,
        combined_classification=combined,
        highest_stage=top,
        evidence_categories=cats,
        statement=statement,
        alerts=tuple(alerts),
        synthetic=any(a.synthetic for a in alerts),
    )


class VerificationService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        loaded: LoadedVerificationConfig,
        signer: EvidenceSigner,
        clock: Clock = utc_now,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self._factory = session_factory
        self.loaded = loaded
        self.config = loaded.config
        self.signer = signer
        self._clock = clock
        self.policy = policy or PermissionPolicy.default()

    # -------------------------------------------------------------- assembly
    def _snapshot(self, s: Session, target_id: int, include_synthetic: bool) -> _Snapshot:
        target = s.get(MonitoringTargetRow, target_id)
        if target is None:
            raise LookupError(f"monitoring target {target_id} not found")
        if target.is_synthetic and not include_synthetic:
            raise PermissionError(
                f"target {target_id} is synthetic; use include_synthetic for test data"
            )
        query_row = s.get(SearchQueryRow, target.query_id)
        query = query_row.original_query if query_row else f"query {target.query_id}"
        keyword = s.get(DiscoveredKeywordRow, target.keyword_id) if target.keyword_id else None
        topic_row = (
            s.get(TopicClusterRow, target.topic_cluster_id) if target.topic_cluster_id else None
        )
        cfg = self.config
        unavailable: list[str] = []
        topic_entities: set[int] = set()
        if keyword is not None and keyword.entity_id:
            topic_entities.add(keyword.entity_id)
        if topic_row is not None and topic_row.representative_entity_id:
            topic_entities.add(topic_row.representative_entity_id)
        # Source items: those linked to the keyword, then those mentioning topic entities.
        item_ids: list[int] = []
        if keyword is not None:
            item_ids += list(
                s.execute(
                    sa.select(DiscoveryEvidenceRow.item_id)
                    .where(DiscoveryEvidenceRow.keyword_id == keyword.id)
                    .order_by(
                        DiscoveryEvidenceRow.evidence_quality.desc(),
                        DiscoveryEvidenceRow.collected_at.desc(),
                    )
                    .limit(cfg.max_bundle_records)
                ).scalars()
            )
        if topic_entities:
            item_ids += list(
                s.execute(
                    sa.select(EntityMentionRow.item_id)
                    .where(EntityMentionRow.entity_id.in_(topic_entities))
                    .order_by(EntityMentionRow.item_id.desc())
                    .limit(cfg.max_bundle_records)
                ).scalars()
            )
        item_ids = list(dict.fromkeys(item_ids))[: cfg.max_bundle_records]
        now = self._clock()
        items = [
            i
            for i in s.execute(
                sa.select(DiscoveryItemRow).where(DiscoveryItemRow.id.in_(item_ids))
            ).scalars()
            if (include_synthetic or not i.is_synthetic)
            and (i.expires_at is None or i.expires_at > now)
        ]
        mention_rows = s.execute(
            sa.select(EntityMentionRow, DiscoveredEntityRow)
            .join(DiscoveredEntityRow, DiscoveredEntityRow.id == EntityMentionRow.entity_id)
            .where(
                EntityMentionRow.item_id.in_([i.id for i in items]),
                EntityMentionRow.status != EntityStatus.REJECTED,
            )
        ).all()
        mentions: dict[int, list[tuple[str, str]]] = {}
        entity_ids = set(topic_entities)
        for m, e in mention_rows:
            mentions.setdefault(m.item_id, []).append((e.canonical_name, m.matched_text))
            entity_ids.add(e.id)
        sources = {
            r.id: bool((r.config or {}).get("verified", False))
            for r in s.execute(
                sa.select(DiscoverySourceRow).where(
                    DiscoverySourceRow.id.in_({i.source_id for i in items})
                )
            ).scalars()
        }
        drafts: list[EvidenceDraft] = [
            from_discovery_item(
                i,
                mentions=mentions.get(i.id, ()),
                source_verified=sources.get(i.source_id, False),
                max_chars=cfg.matching.max_excerpt_chars,
            )
            for i in items
        ]
        entity_rows = list(
            s.execute(
                sa.select(DiscoveredEntityRow).where(DiscoveredEntityRow.id.in_(entity_ids))
            ).scalars()
        )
        entity_rows = [e for e in entity_rows if include_synthetic or not e.is_synthetic]
        aliases: dict[int, list[str]] = {}
        for a in s.execute(
            sa.select(EntityAliasRow).where(EntityAliasRow.entity_id.in_(entity_ids))
        ).scalars():
            aliases.setdefault(a.entity_id, []).append(a.alias)
        entities = [(e, aliases.get(e.id, [])) for e in entity_rows]
        drafts += [from_entity(e, al) for e, al in entities]
        # Latest current observations of the target's series.
        series = list(
            s.execute(sa.select(SeriesRow).where(SeriesRow.target_id == target.id)).scalars()
        )
        for sr in series:
            if sr.is_synthetic and not include_synthetic:
                continue
            obs = s.execute(
                sa.select(ObservationRow)
                .where(ObservationRow.series_id == sr.id, ObservationRow.is_current)
                .order_by(ObservationRow.period_end.desc())
                .limit(cfg.max_observations)
            ).scalars()
            drafts += [
                from_observation(o, sr, query)
                for o in obs
                if o.value is not None and (o.expires_at is None or o.expires_at > now)
            ]
        alerts: list[SurgeAlert] = []
        for row in SurgeEventRepository(s).events(
            active=True, include_synthetic=include_synthetic, target_ids=[target.id], limit=50
        ):
            if (
                row.payload
                and "schema_version" in row.payload
                and "contract_error" not in row.payload
            ):
                alert = SurgeAlert.model_validate(row.payload)
                alerts.append(alert)
                drafts.append(from_surge_alert(alert, expires_at=row.expires_at))
        if not series:
            unavailable.append("no time series has been collected for this target")
        synthetic = target.is_synthetic or any(d.is_synthetic for d in drafts)
        intent = keyword.primary_intent if keyword is not None else None
        return _Snapshot(
            target=target,
            query=query,
            topic=topic_row.canonical_topic if topic_row else None,
            intent=intent,
            drafts=drafts,
            entities=entities,
            topic_entity_keys={e.entity_key for e, _ in entities if e.id in topic_entities},
            alerts=alerts,
            synthetic=synthetic,
            unavailable=unavailable,
        )

    def build_context(
        self, target_id: int, *, include_synthetic: bool = False
    ) -> GenerationContext:
        self.policy.require(Principal.APPLICATION, Capability.READ_EVIDENCE)
        with transaction(self._factory) as s:
            read_only(s)  # the assembly step cannot write, whatever the sources contain
            snap = self._snapshot(s, target_id, include_synthetic)
            target_category = snap.target.category.value
            topic_id = snap.target.topic_cluster_id
            query_id = snap.target.query_id
        self.policy.require(Principal.APPLICATION, Capability.REGISTER_EVIDENCE)
        with transaction(self._factory) as s:
            registry = EvidenceRegistry(SqlEvidenceStore(s), self.signer, self.config, self._clock)
            records = [registry.register(d) for d in snap.drafts]
            bundle = registry.issue_bundle(records, target_id=target_id, synthetic=snap.synthetic)
        by_key = {r.evidence_key: r for r in records}
        catalog = EntityCatalog(
            CatalogEntity(
                key=e.entity_key,
                canonical_name=e.canonical_name,
                entity_type=e.entity_type.value,
                category=e.category.value,
                status=e.status,
                origin=e.origin,
                aliases=tuple(al),
                release_date=e.release_date.date() if e.release_date else None,
                release_level=e.release_level,
                version=e.version,
                synthetic=e.is_synthetic,
                evidence_id=(
                    r.evidence_id if (r := by_key.get(f"entity:{e.entity_key}")) else None
                ),
            )
            for e, al in snap.entities
        )
        event_ids = {
            a.event_id: by_key[f"surge_event:{a.event_id}"].evidence_id
            for a in snap.alerts
            if f"surge_event:{a.event_id}" in by_key
        }
        stats = build_statistics(
            target_view(snap.alerts), target_id=target_id, query=snap.query, evidence_ids=event_ids
        )
        return GenerationContext(
            target_id=target_id,
            query_id=query_id,
            query=snap.query,
            topic_id=topic_id,
            topic=snap.topic,
            category=target_category,
            primary_intent=snap.intent,
            bundle=bundle,
            records=tuple(records),
            catalog=catalog,
            stats=stats,
            now=self._clock(),
            topic_entity_keys=frozenset(snap.topic_entity_keys),
            synthetic=snap.synthetic,
            unavailable=tuple(snap.unavailable) if not records else (),
        )

    # ------------------------------------------------------------------ gate
    def gate(self, *, persist: bool = True) -> VerificationGate:
        registry = EvidenceRegistry(
            FactoryEvidenceStore(self._factory), self.signer, self.config, self._clock
        )
        return VerificationGate(
            registry,
            self.loaded,
            policy=self.policy,
            store=SqlVerificationStore(self._factory) if persist else None,
            clock=self._clock,
        )

    def recommend(
        self,
        target_id: int,
        generator: RecommendationGenerator | None = None,
        *,
        include_synthetic: bool = False,
        persist: bool = True,
    ) -> GateDecision:
        context = self.build_context(target_id, include_synthetic=include_synthetic)
        return self.gate(persist=persist).run(context, generator or TemplateGenerator())

    def check(
        self,
        target_id: int,
        raw_draft: str,
        *,
        include_synthetic: bool = False,
        persist: bool = True,
    ) -> GateDecision:
        """Verify an externally produced draft (one attempt, no regeneration)."""
        context = self.build_context(target_id, include_synthetic=include_synthetic)
        generator = ScriptedGenerator(
            [GenerationResult(GenerationStatus.OK, raw_draft, "external", stop_reason="external")],
            name="external",
        )
        gate = self.gate(persist=persist)
        gate.config = gate.config.model_copy(
            update={"retry": gate.config.retry.model_copy(update={"max_attempts": 1})}
        )
        return gate.run(context, generator)

    # ------------------------------------------------------------------ reads
    def runs(
        self, *, target_id: int | None = None, limit: int = 20, include_synthetic: bool = False
    ) -> list[dict[str, Any]]:
        with transaction(self._factory) as s:
            read_only(s)
            return [
                {
                    "run_id": str(r.id),
                    "target_id": r.target_id,
                    "decided_at": r.decided_at.isoformat(),
                    "state": r.abstention_state.value,
                    "publication": r.publication.value,
                    "generator": r.generator,
                    "attempts": r.attempts,
                    "title": r.title,
                    "violations": list(r.violations),
                }
                for r in VerificationRepository(s).runs(
                    target_id=target_id, limit=limit, include_synthetic=include_synthetic
                )
            ]

    def explain(self, run_id: str) -> dict[str, Any] | None:
        with transaction(self._factory) as s:
            read_only(s)
            repo = VerificationRepository(s)
            run = repo.run(run_id)
            if run is None:
                return None
            return {
                "run_id": run_id,
                "state": run.abstention_state.value,
                "publication": run.publication.value,
                "steps": run.steps,
                "missing_evidence": list(run.missing_evidence),
                "output": run.output,
                "claims": [
                    {
                        "attempt": c.attempt,
                        "field": c.field,
                        "text": c.text,
                        "status": c.status.value,
                        "action": c.action.value,
                        "core": c.core,
                        "supporting_ids": list(c.supporting_ids),
                        "contradicting_ids": list(c.contradicting_ids),
                        "violations": list(c.violations),
                        "reasons": list(c.reasons),
                    }
                    for c in repo.claims(run_id)
                ],
            }

    def evidence(self, *, prefix: str | None = None, limit: int = 50) -> list[EvidenceRecord]:
        with transaction(self._factory) as s:
            read_only(s)
            return VerificationRepository(s).evidence(key_prefix=prefix, limit=limit)

    def stats(self) -> dict[str, Any]:
        with transaction(self._factory) as s:
            read_only(s)
            return VerificationRepository(s).stats()

    def verify_record(self, evidence_id: str) -> tuple[EvidenceRecord | None, str | None]:
        with transaction(self._factory) as s:
            read_only(s)
            found = SqlEvidenceStore(s).get_many([evidence_id])
        rec = found.get(evidence_id)
        if rec is None:
            return None, "no such evidence"
        return rec, self.signer.verify_record(rec)


def make_generator(
    name: str, settings: Settings, loaded: LoadedVerificationConfig
) -> RecommendationGenerator:
    if name == "template":
        return TemplateGenerator()
    if name == "anthropic":
        from trend_agent.verification.anthropic_generator import AnthropicGenerator

        g = loaded.config.generator
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        return AnthropicGenerator(
            api_key=key,
            model=g.anthropic_model,
            max_tokens=g.anthropic_max_tokens,
            timeout_seconds=g.anthropic_timeout_seconds,
            server_side_fallbacks=g.server_side_fallbacks,
        )
    raise ValueError(f"unknown generator {name!r} (template, anthropic)")
