"""Discovery persistence. All writes are idempotent upserts or insert-if-absent."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
    DiscoveryRunRow,
    DiscoveryRunSourceRow,
    DiscoverySourceRow,
    EntityAliasRow,
    EntityMentionRow,
    IntentClusterRow,
    KeywordStatusHistoryRow,
    TopicClusterRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.discovery.budget import SourceState
from trend_agent.discovery.lifecycle import Transition
from trend_agent.discovery.models import (
    CandidateStatus,
    Category,
    DiscoverySource,
    EntityStatus,
    EvidenceRelation,
    EvidenceType,
    SourceHealth,
)
from trend_agent.discovery.text import normalize_name
from trend_agent.logging import storable
from trend_agent.schemas.enums import IngestionStatus

YIELD_ALPHA = 0.3  # EWMA weight of the latest fetch's new-item count


@dataclass(frozen=True)
class SourceRecord:
    id: int
    state: SourceState
    etag: str | None
    last_modified: str | None


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class DiscoveryRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ sources
    def sync_source(
        self, source: DiscoverySource, provider_id: int | None, *, is_synthetic: bool = False
    ) -> int:
        values: dict[str, Any] = {
            "key": source.key,
            "provider_name": source.provider,
            "provider_id": provider_id,
            "name": source.name,
            "source_type": source.source_type,
            "source_reference": source.reference,
            "category": source.category,
            "pool": source.pool,
            "enabled": source.enabled,
            "reliability": source.reliability,
            "min_poll_interval_minutes": source.min_poll_interval_minutes,
            "health_status": SourceHealth.UNKNOWN,
            "is_synthetic": is_synthetic,
            "config": storable(source.model_dump(mode="json")),
        }
        insert = pg_insert(DiscoverySourceRow).values(**values)
        configurable = {k for k in values if k not in {"key", "health_status"}}
        stmt = insert.on_conflict_do_update(
            index_elements=[DiscoverySourceRow.key],
            set_={**{k: insert.excluded[k] for k in configurable}, "updated_at": sa.func.now()},
        ).returning(DiscoverySourceRow.id)
        return int(self._s.execute(stmt).scalar_one())

    def source_records(self, keys: Iterable[str]) -> dict[str, SourceRecord]:
        rows = self._s.execute(
            sa.select(DiscoverySourceRow).where(DiscoverySourceRow.key.in_(list(keys)))
        ).scalars()
        return {
            r.key: SourceRecord(
                id=r.id,
                state=SourceState(
                    key=r.key,
                    last_checked_at=r.last_checked_at,
                    last_success_at=r.last_success_at,
                    consecutive_failures=r.consecutive_failures,
                    health=r.health_status,
                    expected_yield=r.expected_yield,
                ),
                etag=r.etag,
                last_modified=r.last_modified,
            )
            for r in rows
        }

    def mark_source_health(self, source_id: int, health: SourceHealth, detail: str) -> None:
        self._s.execute(
            sa.update(DiscoverySourceRow)
            .where(DiscoverySourceRow.id == source_id)
            .values(health_status=health, last_error=detail[:2000] or None)
        )

    def record_fetch(
        self,
        source_id: int,
        *,
        now: datetime,
        success: bool,
        new_items: int,
        health: SourceHealth,
        error: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        row = self._s.get(DiscoverySourceRow, source_id)
        if row is None:  # pragma: no cover
            return
        row.last_checked_at = now
        row.health_status = health
        if success:
            row.last_success_at = now
            row.consecutive_failures = 0
            row.last_error = None
            row.expected_yield = round(
                YIELD_ALPHA * new_items + (1 - YIELD_ALPHA) * (row.expected_yield or 0.0), 4
            )
            if new_items:
                row.last_new_items_at = now
            if etag is not None:
                row.etag = etag
            if last_modified is not None:
                row.last_modified = last_modified
        else:
            row.consecutive_failures += 1
            row.last_error = (error or "")[:2000] or None

    def sources(self) -> Sequence[DiscoverySourceRow]:
        return (
            self._s.execute(
                sa.select(DiscoverySourceRow).order_by(
                    DiscoverySourceRow.category, DiscoverySourceRow.key
                )
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------ runs
    def start_run(
        self,
        *,
        category: Category | None,
        started_at: datetime,
        scoring_version: str,
        is_synthetic: bool,
        plan: dict[str, Any],
    ) -> uuid.UUID:
        run = DiscoveryRunRow(
            id=uuid.uuid4(),
            started_at=started_at,
            category=category,
            status=IngestionStatus.RUNNING,
            scoring_version=scoring_version,
            is_synthetic=is_synthetic,
            plan=storable(plan),
        )
        self._s.add(run)
        self._s.flush()
        return run.id

    def finish_run(self, run_id: uuid.UUID, **values: Any) -> None:
        if "errors" in values:
            values["errors"] = storable(values["errors"])
        if "summary" in values:
            values["summary"] = storable(values["summary"])
        self._s.execute(
            sa.update(DiscoveryRunRow).where(DiscoveryRunRow.id == run_id).values(**values)
        )

    def add_run_source(self, **values: Any) -> None:
        self._s.add(DiscoveryRunSourceRow(**values))

    def recent_runs(self, limit: int = 20) -> Sequence[DiscoveryRunRow]:
        return (
            self._s.execute(
                sa.select(DiscoveryRunRow).order_by(DiscoveryRunRow.started_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------ items
    def insert_item(self, values: dict[str, Any]) -> tuple[int, bool]:
        """Insert-if-absent by (provider, source_reference). Returns (id, created)."""
        values = {**values, "extra": storable(values.get("extra", {}))}
        stmt = (
            pg_insert(DiscoveryItemRow)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_discovery_items_reference")
            .returning(DiscoveryItemRow.id)
        )
        new_id = self._s.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            return int(new_id), True
        existing = self._s.execute(
            sa.select(DiscoveryItemRow.id).where(
                DiscoveryItemRow.provider_name == values["provider_name"],
                DiscoveryItemRow.source_reference == values["source_reference"],
            )
        ).scalar_one()
        return int(existing), False

    def items(self, ids: Iterable[int]) -> list[DiscoveryItemRow]:
        id_list = list(set(ids))
        if not id_list:
            return []
        return list(
            self._s.execute(
                sa.select(DiscoveryItemRow).where(DiscoveryItemRow.id.in_(id_list))
            ).scalars()
        )

    def source_reliability(self, source_ids: Iterable[int]) -> dict[int, float]:
        ids = list(set(source_ids))
        if not ids:
            return {}
        rows = self._s.execute(
            sa.select(DiscoverySourceRow.id, DiscoverySourceRow.reliability).where(
                DiscoverySourceRow.id.in_(ids)
            )
        ).all()
        return {int(r.id): float(r.reliability) for r in rows}

    # ------------------------------------------------------------------ entities
    def entity_by_key(self, key: str) -> DiscoveredEntityRow | None:
        return self._s.execute(
            sa.select(DiscoveredEntityRow).where(DiscoveredEntityRow.entity_key == key)
        ).scalar_one_or_none()

    def entities_by_name(self, category: Category, name: str) -> list[DiscoveredEntityRow]:
        normalized = normalize_name(name)
        alias_ids = sa.select(EntityAliasRow.entity_id).where(
            EntityAliasRow.alias_normalized == normalized
        )
        return list(
            self._s.execute(
                sa.select(DiscoveredEntityRow).where(
                    DiscoveredEntityRow.category == category,
                    sa.or_(
                        DiscoveredEntityRow.entity_key.like(f"%|{normalized}"),
                        DiscoveredEntityRow.id.in_(alias_ids),
                    ),
                )
            ).scalars()
        )

    def find_entity_any_category(self, name: str) -> list[DiscoveredEntityRow]:
        normalized = normalize_name(name)
        alias_ids = sa.select(EntityAliasRow.entity_id).where(
            EntityAliasRow.alias_normalized == normalized
        )
        return list(
            self._s.execute(
                sa.select(DiscoveredEntityRow)
                .where(
                    sa.or_(
                        DiscoveredEntityRow.entity_key.like(f"%|{normalized}"),
                        DiscoveredEntityRow.id.in_(alias_ids),
                    )
                )
                .order_by(DiscoveredEntityRow.first_seen_at.desc())
            ).scalars()
        )

    def create_entity(self, values: dict[str, Any]) -> DiscoveredEntityRow:
        row = DiscoveredEntityRow(**values)
        self._s.add(row)
        self._s.flush()
        return row

    def add_alias(
        self, entity_id: int, alias: str, source: str, language: str | None = None
    ) -> None:
        normalized = normalize_name(alias)
        if not normalized:
            return
        self._s.execute(
            pg_insert(EntityAliasRow)
            .values(
                entity_id=entity_id,
                alias=alias,
                alias_normalized=normalized,
                language=language,
                source=source,
            )
            .on_conflict_do_nothing(constraint="uq_entity_aliases_alias")
        )

    def add_mention(self, values: dict[str, Any]) -> bool:
        stmt = (
            pg_insert(EntityMentionRow)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_entity_mentions_pair")
            .returning(EntityMentionRow.id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def mentions_for_entity(
        self, entity_id: int
    ) -> list[tuple[EntityMentionRow, DiscoveryItemRow]]:
        rows = self._s.execute(
            sa.select(EntityMentionRow, DiscoveryItemRow)
            .join(DiscoveryItemRow, DiscoveryItemRow.id == EntityMentionRow.item_id)
            .where(EntityMentionRow.entity_id == entity_id)
            .order_by(DiscoveryItemRow.id)
        ).all()
        return [(m, i) for m, i in rows]

    def children(self, parent_id: int) -> list[DiscoveredEntityRow]:
        return list(
            self._s.execute(
                sa.select(DiscoveredEntityRow).where(
                    DiscoveredEntityRow.parent_entity_id == parent_id
                )
            ).scalars()
        )

    def recent_entities(
        self, since: datetime, category: Category | None, include_synthetic: bool, limit: int
    ) -> Sequence[DiscoveredEntityRow]:
        stmt = sa.select(DiscoveredEntityRow).where(DiscoveredEntityRow.first_seen_at >= since)
        if category is not None:
            stmt = stmt.where(DiscoveredEntityRow.category == category)
        if not include_synthetic:
            stmt = stmt.where(DiscoveredEntityRow.is_synthetic.is_(False))
        return (
            self._s.execute(
                stmt.order_by(
                    DiscoveredEntityRow.first_seen_at.desc(), DiscoveredEntityRow.id
                ).limit(limit)
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------ clusters
    def topic_cluster(
        self, key: str, canonical: str, category: Category, entity_id: int | None
    ) -> tuple[int, bool]:
        stmt = (
            pg_insert(TopicClusterRow)
            .values(
                topic_key=key,
                canonical_topic=canonical,
                category=category,
                representative_entity_id=entity_id,
            )
            .on_conflict_do_nothing(index_elements=[TopicClusterRow.topic_key])
            .returning(TopicClusterRow.id)
        )
        new_id = self._s.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            return int(new_id), True
        return int(
            self._s.execute(
                sa.select(TopicClusterRow.id).where(TopicClusterRow.topic_key == key)
            ).scalar_one()
        ), False

    def intent_cluster(
        self, topic_id: int, key: str, intent: Any, representative: str
    ) -> tuple[int, bool]:
        stmt = (
            pg_insert(IntentClusterRow)
            .values(
                topic_cluster_id=topic_id,
                cluster_key=key,
                canonical_intent=intent,
                representative_query=representative,
            )
            .on_conflict_do_nothing(constraint="uq_intent_clusters_key")
            .returning(IntentClusterRow.id)
        )
        new_id = self._s.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            return int(new_id), True
        return int(
            self._s.execute(
                sa.select(IntentClusterRow.id).where(
                    IntentClusterRow.topic_cluster_id == topic_id,
                    IntentClusterRow.cluster_key == key,
                )
            ).scalar_one()
        ), False

    def cluster_members(self) -> list[tuple[str, str, str, str, str | None, Any]]:
        """(topic_key, canonical_topic, intent_cluster_key, query, entity_context, intent).

        ``entity_context`` is the entity name plus its parent's name, i.e. the words that
        identify the subject rather than the information need.
        """
        parent = sa.orm.aliased(DiscoveredEntityRow)
        rows = self._s.execute(
            sa.select(
                TopicClusterRow.topic_key,
                TopicClusterRow.canonical_topic,
                IntentClusterRow.cluster_key,
                SearchQueryRow.original_query,
                DiscoveredEntityRow.canonical_name,
                parent.canonical_name,
                DiscoveredKeywordRow.primary_intent,
            )
            .join(DiscoveredKeywordRow, DiscoveredKeywordRow.topic_cluster_id == TopicClusterRow.id)
            .join(IntentClusterRow, IntentClusterRow.id == DiscoveredKeywordRow.intent_cluster_id)
            .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
            .outerjoin(
                DiscoveredEntityRow, DiscoveredEntityRow.id == DiscoveredKeywordRow.entity_id
            )
            .outerjoin(parent, parent.id == DiscoveredEntityRow.parent_entity_id)
        ).all()
        return [
            (
                str(r[0]),
                str(r[1]),
                str(r[2]),
                str(r[3]),
                " ".join(n for n in (r[4], r[5]) if n) or None,
                r[6],
            )
            for r in rows
        ]

    def topic_keys(self) -> list[tuple[str, str]]:
        return [
            (str(r.topic_key), str(r.canonical_topic))
            for r in self._s.execute(
                sa.select(TopicClusterRow.topic_key, TopicClusterRow.canonical_topic)
            )
        ]

    # ------------------------------------------------------------------ keywords
    def keyword_by_query(self, query_id: int) -> DiscoveredKeywordRow | None:
        return self._s.execute(
            sa.select(DiscoveredKeywordRow).where(DiscoveredKeywordRow.query_id == query_id)
        ).scalar_one_or_none()

    def cluster_first_seen(
        self, intent_cluster_id: int | None, topic_cluster_id: int | None
    ) -> tuple[datetime | None, datetime | None]:
        """Earliest discovery time of any keyword in the intent cluster / the topic."""

        def earliest(column: Any, value: int | None) -> datetime | None:
            if value is None:
                return None
            found: datetime | None = self._s.execute(
                sa.select(sa.func.min(DiscoveredKeywordRow.first_discovered_at)).where(
                    column == value
                )
            ).scalar_one()
            return found

        return (
            earliest(DiscoveredKeywordRow.intent_cluster_id, intent_cluster_id),
            earliest(DiscoveredKeywordRow.topic_cluster_id, topic_cluster_id),
        )

    def ancestor_known_before(self, entity: DiscoveredEntityRow, max_depth: int = 3) -> bool:
        """Was a parent (or grandparent) entity first seen before ``entity``?"""
        current = entity
        for _ in range(max_depth):
            if current.parent_entity_id is None:
                return False
            parent = self._s.get(DiscoveredEntityRow, current.parent_entity_id)
            if parent is None:
                return False
            if parent.first_seen_at < entity.first_seen_at:
                return True
            current = parent
        return False

    def create_keyword(self, values: dict[str, Any]) -> DiscoveredKeywordRow:
        row = DiscoveredKeywordRow(**values)
        self._s.add(row)
        self._s.flush()
        return row

    def add_evidence(
        self,
        keyword_id: int,
        item: DiscoveryItemRow,
        relation: EvidenceRelation,
        quality: float,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        stmt = (
            pg_insert(DiscoveryEvidenceRow)
            .values(
                keyword_id=keyword_id,
                item_id=item.id,
                provider_name=item.provider_name,
                source_reference=item.source_reference,
                evidence_type=item.evidence_type,
                relation=relation,
                observed_at=item.published_at or item.collected_at,
                collected_at=item.collected_at,
                source_published_at=item.published_at,
                evidence_quality=quality,
                extra=storable(extra or {}),
            )
            .on_conflict_do_nothing(constraint="uq_discovery_evidence_link")
            .returning(DiscoveryEvidenceRow.id)
        )
        return self._s.execute(stmt).scalar_one_or_none() is not None

    def evidence_items(self, keyword_id: int) -> list[DiscoveryItemRow]:
        return list(
            self._s.execute(
                sa.select(DiscoveryItemRow)
                .join(DiscoveryEvidenceRow, DiscoveryEvidenceRow.item_id == DiscoveryItemRow.id)
                .where(DiscoveryEvidenceRow.keyword_id == keyword_id)
                .distinct()
                .order_by(DiscoveryItemRow.id)
            ).scalars()
        )

    def evidence_rows(self, keyword_id: int) -> Sequence[DiscoveryEvidenceRow]:
        return (
            self._s.execute(
                sa.select(DiscoveryEvidenceRow)
                .where(DiscoveryEvidenceRow.keyword_id == keyword_id)
                .order_by(DiscoveryEvidenceRow.id)
            )
            .scalars()
            .all()
        )

    def add_history(self, keyword_id: int, t: Transition, run_id: uuid.UUID | None) -> None:
        self._s.add(
            KeywordStatusHistoryRow(
                keyword_id=keyword_id,
                from_status=t.from_status,
                to_status=t.to_status,
                reason=t.reason,
                actor=t.actor,
                run_id=run_id,
                changed_at=t.at,
            )
        )

    def history(self, keyword_id: int) -> Sequence[KeywordStatusHistoryRow]:
        return (
            self._s.execute(
                sa.select(KeywordStatusHistoryRow)
                .where(KeywordStatusHistoryRow.keyword_id == keyword_id)
                .order_by(KeywordStatusHistoryRow.id)
            )
            .scalars()
            .all()
        )

    def dormancy_candidates(self, cutoff: datetime) -> Sequence[DiscoveredKeywordRow]:
        return (
            self._s.execute(
                sa.select(DiscoveredKeywordRow).where(
                    DiscoveredKeywordRow.last_discovered_at < cutoff,
                    DiscoveredKeywordRow.candidate_status.in_(
                        [CandidateStatus.VALIDATED, CandidateStatus.PRIORITIZED]
                    ),
                )
            )
            .scalars()
            .all()
        )

    def keywords(
        self,
        *,
        category: Category | None = None,
        statuses: Sequence[CandidateStatus] | None = None,
        include_synthetic: bool = True,
        limit: int = 50,
    ) -> list[tuple[DiscoveredKeywordRow, SearchQueryRow, DiscoveredEntityRow | None]]:
        stmt = (
            sa.select(DiscoveredKeywordRow, SearchQueryRow, DiscoveredEntityRow)
            .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
            .outerjoin(
                DiscoveredEntityRow, DiscoveredEntityRow.id == DiscoveredKeywordRow.entity_id
            )
        )
        if category is not None:
            stmt = stmt.where(DiscoveredKeywordRow.primary_category == category)
        if statuses:
            stmt = stmt.where(DiscoveredKeywordRow.candidate_status.in_(list(statuses)))
        if not include_synthetic:
            stmt = stmt.where(DiscoveredKeywordRow.is_synthetic.is_(False))
        stmt = stmt.order_by(
            DiscoveredKeywordRow.priority_score.desc(), SearchQueryRow.canonical_query
        ).limit(limit)
        return [(k, q, e) for k, q, e in self._s.execute(stmt).all()]

    def evidence_breakdown(self, keyword_ids: Sequence[int]) -> dict[int, dict[str, int]]:
        if not keyword_ids:
            return {}
        rows = self._s.execute(
            sa.select(
                DiscoveryEvidenceRow.keyword_id,
                sa.func.coalesce(
                    DiscoveryItemRow.simulated_evidence_type, DiscoveryItemRow.evidence_type
                ),
                sa.func.count(),
            )
            .join(DiscoveryItemRow, DiscoveryItemRow.id == DiscoveryEvidenceRow.item_id)
            .where(DiscoveryEvidenceRow.keyword_id.in_(list(keyword_ids)))
            .group_by(DiscoveryEvidenceRow.keyword_id, sa.text("2"))
        ).all()
        out: dict[int, dict[str, int]] = {}
        for keyword_id, evidence_type, count in rows:
            value = (
                evidence_type.value
                if isinstance(evidence_type, EvidenceType)
                else str(evidence_type)
            )
            out.setdefault(int(keyword_id), {})[value] = int(count)
        return out

    # ------------------------------------------------------------------ stats / retention
    def stats(self, since: datetime) -> dict[str, Any]:
        def count(model: type, *where: Any) -> int:
            return int(
                self._s.execute(
                    sa.select(sa.func.count()).select_from(model).where(*where)
                ).scalar_one()
            )

        by_category = self._grouped(
            sa.select(DiscoveredKeywordRow.primary_category, sa.func.count()).group_by(
                DiscoveredKeywordRow.primary_category
            )
        )
        by_status = self._grouped(
            sa.select(DiscoveredKeywordRow.candidate_status, sa.func.count()).group_by(
                DiscoveredKeywordRow.candidate_status
            )
        )
        by_origin = self._grouped(
            sa.select(DiscoveredKeywordRow.origin, sa.func.count()).group_by(
                DiscoveredKeywordRow.origin
            )
        )
        provenance = self._grouped(
            sa.select(
                sa.func.coalesce(
                    DiscoveryItemRow.simulated_evidence_type, DiscoveryItemRow.evidence_type
                ),
                sa.func.count(sa.distinct(DiscoveryEvidenceRow.keyword_id)),
            )
            .join(DiscoveryItemRow, DiscoveryItemRow.id == DiscoveryEvidenceRow.item_id)
            .group_by(sa.text("1"))
        )
        runs = self._s.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(DiscoveryRunRow.sources_checked), 0),
                sa.func.coalesce(sa.func.sum(DiscoveryRunRow.sources_failed), 0),
                sa.func.coalesce(sa.func.sum(DiscoveryRunRow.merged_candidates), 0),
                sa.func.coalesce(sa.func.sum(DiscoveryRunRow.new_candidates), 0),
                sa.func.coalesce(sa.func.sum(DiscoveryRunRow.rejected_candidates), 0),
            ).where(DiscoveryRunRow.started_at >= since)
        ).one()

        return {
            "total_keywords": count(DiscoveredKeywordRow),
            "new_keywords_since": count(
                DiscoveredKeywordRow, DiscoveredKeywordRow.first_discovered_at >= since
            ),
            "new_entities_since": count(
                DiscoveredEntityRow, DiscoveredEntityRow.first_seen_at >= since
            ),
            "total_entities": count(DiscoveredEntityRow),
            "items_total": count(DiscoveryItemRow),
            "sources_checked_since": int(runs[0]),
            "provider_failures_since": int(runs[1]),
            "duplicates_merged_since": int(runs[2]),
            "candidates_accepted_since": int(runs[3]),
            "candidates_rejected_since": int(runs[4]),
            "by_category": by_category,
            "by_status": by_status,
            "by_origin": by_origin,
            "provenance_by_evidence_type": provenance,
        }

    def _grouped(self, stmt: Any) -> dict[str, int]:
        return {
            getattr(key, "value", str(key)): int(count)
            for key, count in self._s.execute(stmt).all()
        }

    def purge_expired_items(self, now: datetime) -> int:
        return _rowcount(
            self._s.execute(sa.delete(DiscoveryItemRow).where(DiscoveryItemRow.expires_at <= now))
        )

    def count_expired_items(self, now: datetime) -> int:
        return int(
            self._s.execute(
                sa.select(sa.func.count())
                .select_from(DiscoveryItemRow)
                .where(DiscoveryItemRow.expires_at <= now)
            ).scalar_one()
        )


def entity_status_rank(status: EntityStatus) -> int:
    return {EntityStatus.REJECTED: 0, EntityStatus.NEEDS_VERIFICATION: 1, EntityStatus.VERIFIED: 2}[
        status
    ]


def window_start(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
