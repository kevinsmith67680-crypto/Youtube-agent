"""Monitoring targets, their events, keyword candidates, provider health, plans, backfills."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.discovery_models import (
    DiscoveredEntityRow,
    DiscoveredKeywordRow,
    DiscoveryEvidenceRow,
    DiscoveryItemRow,
)
from trend_agent.db.models import SearchQueryRow
from trend_agent.db.monitoring_models import (
    DataPolicyRow,
    MonitoringBackfillRow,
    MonitoringPlanRunRow,
    MonitoringTargetEventRow,
    MonitoringTargetRow,
    ProviderHealthRow,
)
from trend_agent.discovery.models import CandidateStatus, Category
from trend_agent.schemas.monitoring import (
    HealthStatus,
    MeasurementCategory,
    MonitoringStatus,
    MonitoringTier,
)


@dataclass(frozen=True)
class KeywordCandidateView:
    keyword_id: int
    query_id: int
    canonical_query: str
    original_query: str
    category: Category
    priority: float
    status: CandidateStatus
    novelty_kind: str
    independent_origins: int
    topic_cluster_id: int | None
    intent_cluster_id: int | None
    last_discovered_at: datetime
    entity_type: str | None
    entity_first_seen_at: datetime | None
    is_synthetic: bool


class MonitoringRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ targets
    def get_target(self, target_id: int, *, lock: bool = False) -> MonitoringTargetRow | None:
        if lock:
            return self._s.execute(
                sa.select(MonitoringTargetRow)
                .where(MonitoringTargetRow.id == target_id)
                .with_for_update()
            ).scalar_one_or_none()
        return self._s.get(MonitoringTargetRow, target_id)

    def find_target(
        self,
        query_id: int,
        region: str,
        language: str | None,
        category: MeasurementCategory,
    ) -> MonitoringTargetRow | None:
        t = MonitoringTargetRow
        return self._s.execute(
            sa.select(t).where(
                t.query_id == query_id,
                t.region == region,
                t.language.is_not_distinct_from(language),
                t.measurement_category == category,
            )
        ).scalar_one_or_none()

    def create_target(self, values: dict[str, Any]) -> tuple[MonitoringTargetRow, bool]:
        stmt = (
            pg_insert(MonitoringTargetRow)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_monitoring_targets_identity")
            .returning(MonitoringTargetRow.id)
        )
        new_id = self._s.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            row = self._s.get(MonitoringTargetRow, int(new_id))
            assert row is not None
            return row, True
        existing = self.find_target(
            values["query_id"],
            values["region"],
            values["language"],
            values["measurement_category"],
        )
        assert existing is not None
        return existing, False

    def targets(
        self,
        *,
        statuses: Sequence[MonitoringStatus] | None = None,
        category: Category | None = None,
        tier: MonitoringTier | None = None,
        query_text: str | None = None,
        proxy: bool | None = None,
        include_synthetic: bool = True,
        after_id: int | None = None,
        limit: int = 500,
    ) -> list[MonitoringTargetRow]:
        t = MonitoringTargetRow
        stmt = sa.select(t)
        if statuses is not None:
            stmt = stmt.where(t.status.in_([s.value for s in statuses]))
        if category is not None:
            stmt = stmt.where(t.category == category)
        if tier is not None:
            stmt = stmt.where(t.tier == tier)
        if proxy is not None:
            stmt = stmt.where(t.is_proxy.is_(proxy))
        if not include_synthetic:
            stmt = stmt.where(t.is_synthetic.is_(False))
        if query_text is not None:
            stmt = stmt.join(SearchQueryRow, SearchQueryRow.id == t.query_id).where(
                SearchQueryRow.canonical_query == query_text
            )
        if after_id is not None:
            stmt = stmt.where(t.id > after_id)
        return list(self._s.execute(stmt.order_by(t.id).limit(limit)).scalars())

    def due_targets(
        self,
        statuses: Sequence[MonitoringStatus],
        horizon: datetime,
        *,
        after_id: int | None,
        limit: int,
    ) -> list[MonitoringTargetRow]:
        t = MonitoringTargetRow
        stmt = sa.select(t).where(
            t.status.in_([s.value for s in statuses]),
            sa.or_(t.next_collection_at.is_(None), t.next_collection_at <= horizon),
        )
        if after_id is not None:
            stmt = stmt.where(t.id > after_id)
        return list(self._s.execute(stmt.order_by(t.id).limit(limit)).scalars())

    def query_text(self, query_ids: Sequence[int]) -> dict[int, tuple[str, str]]:
        if not query_ids:
            return {}
        rows = self._s.execute(
            sa.select(
                SearchQueryRow.id, SearchQueryRow.canonical_query, SearchQueryRow.original_query
            ).where(SearchQueryRow.id.in_(list(query_ids)))
        ).all()
        return {int(r[0]): (r[1], r[2]) for r in rows}

    def count_targets(
        self, *, statuses: Sequence[MonitoringStatus], proxy: bool | None = None
    ) -> dict[Category, int]:
        t = MonitoringTargetRow
        stmt = sa.select(t.category, sa.func.count()).where(
            t.status.in_([s.value for s in statuses])
        )
        if proxy is not None:
            stmt = stmt.where(t.is_proxy.is_(proxy))
        rows = self._s.execute(stmt.group_by(t.category)).all()
        return {r[0]: int(r[1]) for r in rows}

    def target_breakdown(self) -> list[tuple[str, str, str, bool, int]]:
        t = MonitoringTargetRow
        rows = self._s.execute(
            sa.select(t.category, t.status, t.tier, t.is_proxy, sa.func.count()).group_by(
                t.category, t.status, t.tier, t.is_proxy
            )
        ).all()
        return [(r[0].value, r[1].value, r[2].value, bool(r[3]), int(r[4])) for r in rows]

    def add_event(
        self,
        target_id: int,
        *,
        kind: str,
        from_value: str | None,
        to_value: str,
        reason: str,
        actor: str,
        at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._s.add(
            MonitoringTargetEventRow(
                target_id=target_id,
                kind=kind,
                from_value=from_value,
                to_value=to_value,
                reason=reason,
                actor=actor,
                at=at,
                details=details or {},
            )
        )

    def events(self, target_id: int, limit: int = 50) -> list[MonitoringTargetEventRow]:
        e = MonitoringTargetEventRow
        return list(
            self._s.execute(
                sa.select(e)
                .where(e.target_id == target_id)
                .order_by(e.at.desc(), e.id.desc())
                .limit(limit)
            ).scalars()
        )

    # ------------------------------------------------------------------ keyword candidates
    def keyword_candidates(
        self,
        *,
        statuses: Sequence[CandidateStatus],
        min_priority: float,
        include_synthetic: bool,
        after_id: int | None,
        limit: int,
        keyword_ids: Sequence[int] | None = None,
    ) -> list[KeywordCandidateView]:
        k, q, e = DiscoveredKeywordRow, SearchQueryRow, DiscoveredEntityRow
        stmt = (
            sa.select(k, q.canonical_query, q.original_query, e.entity_type, e.first_seen_at)
            .join(q, q.id == k.query_id)
            .outerjoin(e, e.id == k.entity_id)
            .where(k.candidate_status.in_([s.value for s in statuses]))
            .where(k.priority_score >= min_priority)
        )
        if keyword_ids is not None:
            stmt = stmt.where(k.id.in_(list(keyword_ids)))
        if not include_synthetic:
            stmt = stmt.where(k.is_synthetic.is_(False))
        if after_id is not None:
            stmt = stmt.where(k.id > after_id)
        out = []
        for row in self._s.execute(stmt.order_by(k.id).limit(limit)).all():
            kw: DiscoveredKeywordRow = row[0]
            out.append(
                KeywordCandidateView(
                    keyword_id=kw.id,
                    query_id=kw.query_id,
                    canonical_query=row[1],
                    original_query=row[2],
                    category=kw.primary_category,
                    priority=kw.priority_score,
                    status=kw.candidate_status,
                    novelty_kind=kw.novelty_kind.value,
                    independent_origins=int(
                        (kw.score_components or {}).get("independent_origins", 0)
                    ),
                    topic_cluster_id=kw.topic_cluster_id,
                    intent_cluster_id=kw.intent_cluster_id,
                    last_discovered_at=kw.last_discovered_at,
                    entity_type=row[3].value if row[3] is not None else None,
                    entity_first_seen_at=row[4],
                    is_synthetic=kw.is_synthetic,
                )
            )
        return out

    def keyword(self, keyword_id: int) -> DiscoveredKeywordRow | None:
        return self._s.get(DiscoveredKeywordRow, keyword_id)

    def keywords_by_ids(self, ids: Sequence[int]) -> dict[int, DiscoveredKeywordRow]:
        if not ids:
            return {}
        rows = self._s.execute(
            sa.select(DiscoveredKeywordRow).where(DiscoveredKeywordRow.id.in_(list(ids)))
        ).scalars()
        return {r.id: r for r in rows}

    def keyword_for_query(self, canonical: str) -> DiscoveredKeywordRow | None:
        return (
            self._s.execute(
                sa.select(DiscoveredKeywordRow)
                .join(SearchQueryRow, SearchQueryRow.id == DiscoveredKeywordRow.query_id)
                .where(SearchQueryRow.canonical_query == canonical)
            )
            .scalars()
            .first()
        )

    def queries_by_text(self, canonical: str) -> list[SearchQueryRow]:
        return list(
            self._s.execute(
                sa.select(SearchQueryRow).where(SearchQueryRow.canonical_query == canonical)
            ).scalars()
        )

    # ------------------------------------------------------------------ provider health
    def health(self, provider: str) -> ProviderHealthRow | None:
        return self._s.get(ProviderHealthRow, provider)

    def all_health(self) -> list[ProviderHealthRow]:
        return list(
            self._s.execute(
                sa.select(ProviderHealthRow).order_by(ProviderHealthRow.provider_name)
            ).scalars()
        )

    def record_health(
        self,
        provider: str,
        *,
        success: bool,
        now: datetime,
        unhealthy_after: int,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> ProviderHealthRow:
        row = self._s.execute(
            sa.select(ProviderHealthRow)
            .where(ProviderHealthRow.provider_name == provider)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            self._s.execute(
                pg_insert(ProviderHealthRow)
                .values(
                    provider_name=provider,
                    status=HealthStatus.UNKNOWN,
                    consecutive_failures=0,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[ProviderHealthRow.provider_name])
            )
            row = self._s.execute(
                sa.select(ProviderHealthRow)
                .where(ProviderHealthRow.provider_name == provider)
                .with_for_update()
            ).scalar_one()
        if success:
            row.status = HealthStatus.HEALTHY
            row.consecutive_failures = 0
            row.last_success_at = now
            row.unhealthy_since = None
        else:
            row.consecutive_failures += 1
            row.last_failure_at = now
            row.last_error_category = error_category
            row.last_error_message = (error_message or "")[:500] or None
            if row.consecutive_failures >= unhealthy_after:
                if row.status is not HealthStatus.UNHEALTHY:
                    row.unhealthy_since = now
                row.status = HealthStatus.UNHEALTHY
            else:
                row.status = HealthStatus.DEGRADED
        row.updated_at = now
        return row

    # ------------------------------------------------------------------ plans / backfills
    def add_plan_run(self, values: dict[str, Any]) -> uuid.UUID:
        row = MonitoringPlanRunRow(id=uuid.uuid4(), **values)
        self._s.add(row)
        self._s.flush()
        return row.id

    def recent_plan_runs(self, limit: int = 10) -> list[MonitoringPlanRunRow]:
        p = MonitoringPlanRunRow
        return list(
            self._s.execute(sa.select(p).order_by(p.started_at.desc()).limit(limit)).scalars()
        )

    def backfill_by_signature(self, signature: str) -> MonitoringBackfillRow | None:
        return self._s.execute(
            sa.select(MonitoringBackfillRow).where(MonitoringBackfillRow.signature == signature)
        ).scalar_one_or_none()

    def add_backfill(self, values: dict[str, Any]) -> MonitoringBackfillRow:
        row = MonitoringBackfillRow(id=uuid.uuid4(), **values)
        self._s.add(row)
        self._s.flush()
        return row

    # ------------------------------------------------------------------ policies
    def upsert_policy(self, values: dict[str, Any]) -> None:
        stmt = pg_insert(DataPolicyRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[DataPolicyRow.id],
            set_={k: stmt.excluded[k] for k in values if k != "id"},
        )
        self._s.execute(stmt)

    def policies(self) -> list[DataPolicyRow]:
        return list(self._s.execute(sa.select(DataPolicyRow).order_by(DataPolicyRow.id)).scalars())

    # ------------------------------------------------------------------ discovery evidence
    def evidence_items(self, keyword_id: int, start: datetime, end: datetime) -> list[Any]:
        """Discovery items linked to a keyword, published within [start, end)."""
        e, i = DiscoveryEvidenceRow, DiscoveryItemRow
        return list(
            self._s.execute(
                sa.select(
                    i.id,
                    i.provider_name,
                    i.evidence_type,
                    i.simulated_evidence_type,
                    i.is_synthetic,
                    i.published_at,
                    i.collected_at,
                    i.expires_at,
                    i.quality_flags,
                    e.relation,
                )
                .join(e, e.item_id == i.id)
                .where(
                    e.keyword_id == keyword_id,
                    i.published_at.is_not(None),
                    i.published_at >= start,
                    i.published_at < end,
                )
                .order_by(i.id)
            ).all()
        )

    # ------------------------------------------------------------------ synthetic cleanup
    def purge_synthetic(self, providers: Sequence[str]) -> dict[str, int]:
        """Delete synthetic monitoring data only (demo reset). Real data is never touched."""
        from trend_agent.db.models import ProviderRateLimitRow
        from trend_agent.db.monitoring_models import (
            MonitoringJobRow,
            ObservationRow,
            SeriesRow,
        )

        t = MonitoringTargetRow
        synthetic_targets = sa.select(t.id).where(t.is_synthetic.is_(True))
        counts: dict[str, int] = {}

        def run(name: str, stmt: Any) -> None:
            counts[name] = int(getattr(self._s.execute(stmt), "rowcount", 0) or 0)

        run("observations", sa.delete(ObservationRow).where(ObservationRow.is_synthetic.is_(True)))
        run("series", sa.delete(SeriesRow).where(SeriesRow.is_synthetic.is_(True)))
        run(
            "jobs",
            sa.delete(MonitoringJobRow).where(MonitoringJobRow.target_id.in_(synthetic_targets)),
        )
        run("targets", sa.delete(t).where(t.is_synthetic.is_(True)))
        run(
            "provider_health",
            sa.delete(ProviderHealthRow).where(
                ProviderHealthRow.provider_name.in_(list(providers))
            ),
        )
        run(
            "quota_rows",
            sa.delete(ProviderRateLimitRow).where(
                ProviderRateLimitRow.provider_name.in_([f"monitoring:{p}" for p in providers])
            ),
        )
        return counts
