"""Surge episodes in PostgreSQL: the production ``EpisodeStore`` and read queries.

Concurrency: ``open_episode`` first takes a transaction-scoped advisory lock on the
episode key, so two workers processing different series of the same target (e.g. two
normalisation batches) serialise their episode updates; the partial unique index on open
episodes is the last line of defence against duplicates.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from trend_agent.db.detection_models import (
    SurgeEventRow,
    SurgeEventTransitionRow,
    SurgeEvidenceRow,
)
from trend_agent.detection.events import Episode, EvidenceItem, TransitionRecord
from trend_agent.discovery.models import Category
from trend_agent.schemas.evidence import OPEN_STAGES, AlertStage

KEY_LOCK_NAMESPACE = 0x7D3F

_FIELDS = (
    "key",
    "target_id",
    "series_id",
    "query_id",
    "keyword_id",
    "topic_cluster_id",
    "category",
    "provider",
    "measurement",
    "is_proxy",
    "classification",
    "region",
    "language",
    "status",
    "confidence",
    "episode_start",
    "first_detected_at",
    "last_updated_at",
    "last_observation_period_end",
    "severity",
    "maximum_severity",
    "peak_value",
    "peak_period_start",
    "peak_observation_id",
    "baseline_expected",
    "baseline_dispersion",
    "algorithm_version",
    "configuration_version",
    "is_synthetic",
    "resolved_at",
    "reopen_count",
    "notifications",
    "counters",
    "explanation",
    "payload",
    "expires_at",
)
_COLUMN = {
    "key": "episode_key",
    "provider": "provider_name",
    "measurement": "measurement_category",
    "classification": "evidence_classification",
    "confidence": "confidence_status",
}


def to_episode(row: SurgeEventRow) -> Episode:
    data = {f: getattr(row, _COLUMN.get(f, f)) for f in _FIELDS}
    data["counters"] = dict(row.counters or {})
    data["payload"] = dict(row.payload or {})
    return Episode(id=row.id, **data)


def _values(ep: Episode) -> dict[str, object]:
    out = {_COLUMN.get(f, f): getattr(ep, f) for f in _FIELDS}
    out["severity"] = round(float(ep.severity), 4)
    out["maximum_severity"] = round(max(float(ep.maximum_severity), float(ep.severity)), 4)
    return out


class SqlEpisodeStore:
    def __init__(self, session: Session, run_id: object | None = None) -> None:
        self._s = session
        self._run_id = run_id

    def lock_key(self, key: str) -> None:
        self._s.execute(
            sa.text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": KEY_LOCK_NAMESPACE, "key": key},
        )

    def open_episode(self, key: str) -> Episode | None:
        self.lock_key(key)
        row = self._s.execute(
            sa.select(SurgeEventRow)
            .where(SurgeEventRow.episode_key == key, SurgeEventRow.status.in_(list(OPEN_STAGES)))
            .with_for_update()
        ).scalar_one_or_none()
        return to_episode(row) if row is not None else None

    def last_closed(self, key: str) -> Episode | None:
        row = self._s.execute(
            sa.select(SurgeEventRow)
            .where(
                SurgeEventRow.episode_key == key,
                SurgeEventRow.status.not_in(list(OPEN_STAGES)),
            )
            .order_by(SurgeEventRow.resolved_at.desc().nulls_last(), SurgeEventRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return to_episode(row) if row is not None else None

    def open_episodes(self) -> list[Episode]:
        rows = self._s.execute(
            sa.select(SurgeEventRow).where(SurgeEventRow.status.in_(list(OPEN_STAGES)))
        ).scalars()
        return [to_episode(r) for r in rows]

    def insert(self, episode: Episode) -> Episode:
        row = SurgeEventRow(**_values(episode))
        self._s.add(row)
        self._s.flush()
        episode.id = row.id
        return episode

    def update(self, episode: Episode) -> None:
        assert episode.id is not None
        self._s.execute(
            sa.update(SurgeEventRow)
            .where(SurgeEventRow.id == episode.id)
            .values(**_values(episode))
        )

    def transition(self, episode: Episode, record: TransitionRecord) -> None:
        assert episode.id is not None
        self._s.add(
            SurgeEventTransitionRow(
                surge_event_id=episode.id,
                run_id=self._run_id,
                kind=record.kind,
                from_status=record.from_status,
                to_status=record.to_status,
                severity=round(record.severity, 4),
                is_notification=record.notification,
                reason=record.reason,
                at=record.at,
                is_synthetic=episode.is_synthetic,
            )
        )

    def evidence(self, episode: Episode, items: list[EvidenceItem]) -> None:
        assert episode.id is not None
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        for item in items:
            self._s.execute(
                pg_insert(SurgeEvidenceRow)
                .values(
                    surge_event_id=episode.id,
                    observation_id=item.observation_id,
                    detector_result_id=item.detector_result_id,
                    evidence_type=item.evidence_type,
                    source_reference=item.source_reference,
                    contribution=item.contribution,
                    details=item.details,
                    created_at=episode.last_updated_at,
                )
                .on_conflict_do_nothing(constraint="uq_surge_evidence_link")
            )


class SurgeEventRepository:
    """Read side for CLI, Phase 5 feed and statistics."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def events(
        self,
        *,
        active: bool = False,
        statuses: Sequence[AlertStage] | None = None,
        category: Category | None = None,
        include_synthetic: bool = False,
        target_ids: Sequence[int] | None = None,
        limit: int = 100,
    ) -> list[SurgeEventRow]:
        t = SurgeEventRow
        stmt = sa.select(t)
        if active:
            stmt = stmt.where(t.status.in_(list(OPEN_STAGES)))
        if statuses:
            stmt = stmt.where(t.status.in_(list(statuses)))
        if category is not None:
            stmt = stmt.where(t.category == category)
        if not include_synthetic:
            stmt = stmt.where(t.is_synthetic.is_(False))
        if target_ids is not None:
            stmt = stmt.where(t.target_id.in_(list(target_ids)))
        stmt = stmt.order_by(t.maximum_severity.desc(), t.id.desc()).limit(limit)
        return list(self._s.execute(stmt).scalars())

    def get(self, event_id: int) -> SurgeEventRow | None:
        return self._s.get(SurgeEventRow, event_id)

    def transitions(self, event_id: int) -> list[SurgeEventTransitionRow]:
        t = SurgeEventTransitionRow
        return list(
            self._s.execute(
                sa.select(t).where(t.surge_event_id == event_id).order_by(t.at, t.id)
            ).scalars()
        )

    def evidence(self, event_id: int) -> list[SurgeEvidenceRow]:
        t = SurgeEvidenceRow
        return list(
            self._s.execute(
                sa.select(t).where(t.surge_event_id == event_id).order_by(t.created_at, t.id)
            ).scalars()
        )

    def notifications_since(self, since: datetime, *, synthetic: bool) -> int:
        t = SurgeEventTransitionRow
        return int(
            self._s.execute(
                sa.select(sa.func.count())
                .select_from(t)
                .where(t.is_notification.is_(True), t.at > since, t.is_synthetic.is_(synthetic))
            ).scalar_one()
        )

    def status_counts(self, *, include_synthetic: bool = False) -> dict[str, int]:
        t = SurgeEventRow
        stmt = sa.select(t.status, sa.func.count()).group_by(t.status)
        if not include_synthetic:
            stmt = stmt.where(t.is_synthetic.is_(False))
        return {s.value: int(n) for s, n in self._s.execute(stmt).all()}

    def count_expired(self, now: datetime) -> int:
        t = SurgeEventRow
        return int(
            self._s.execute(
                sa.select(sa.func.count()).select_from(t).where(t.expires_at <= now)
            ).scalar_one()
        )

    def purge_expired(self, now: datetime) -> int:
        result = self._s.execute(sa.delete(SurgeEventRow).where(SurgeEventRow.expires_at <= now))
        return int(getattr(result, "rowcount", 0) or 0)
