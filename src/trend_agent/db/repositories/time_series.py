"""Time-series storage: series registry and append-only, revision-aware observations.

Write semantics (``TimeSeriesRepository.write``) for one (series, window):

* nothing stored                          -> insert revision 1 (``NEW``)
* identical value + quality stored        -> ``UNCHANGED``; only confirmation metadata moves
* different value, source may revise      -> new revision becomes current (``REVISED``);
                                             the previous one is kept with ``superseded_at``
* different value, source is immutable    -> stored as a non-current ``CONFLICT`` row
                                             (``CONFLICTING``); the original stays current

Concurrency: the partial unique index (one *current* row per series+window) plus
``ON CONFLICT DO NOTHING`` and row locks make concurrent or repeated writes of the same
observation idempotent; a lost race re-reads and compares instead of duplicating.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.monitoring_models import ObservationRow, SeriesRow
from trend_agent.schemas.enums import SampleQuality
from trend_agent.schemas.monitoring import (
    MeasurementCategory,
    QualityStatus,
    RevisionKind,
    WriteOutcome,
)
from trend_agent.schemas.scheduling import RevisionBehaviour
from trend_agent.schemas.time_series import ObservationDraft, StoredPoint


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    observation_id: int
    series_id: int
    revision: int


@dataclass(frozen=True)
class SeriesSummary:
    series_id: int
    series_key: str
    target_id: int | None
    query_id: int
    provider_name: str
    measurement_category: MeasurementCategory
    simulated_category: MeasurementCategory | None
    is_proxy: bool
    is_synthetic: bool
    region: str
    language: str | None
    granularity: str
    scale_type: str
    unit: str
    normalization_key: str
    normalization_context: dict[str, Any]
    observations: int  # current revisions
    with_value: int
    missing_values: int
    low_volume: int
    partial: int
    revisions: int
    conflicts: int
    first_period: datetime | None
    last_period_end: datetime | None
    last_collected: datetime | None

    @property
    def effective_category(self) -> MeasurementCategory:
        return self.simulated_category or self.measurement_category


def _dedup_key(series_key: str, start: datetime, end: datetime) -> str:
    raw = f"{series_key}|{start.isoformat()}|{end.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _point(row: ObservationRow) -> StoredPoint:
    return StoredPoint(
        id=row.id,
        series_id=row.series_id,
        target_id=row.target_id,
        provider_name=row.provider_name,
        measurement_category=row.measurement_category,
        simulated_category=row.simulated_category,
        period_start=row.period_start,
        period_end=row.period_end,
        granularity=row.granularity,
        value=row.value,
        sample_quality=row.sample_quality,
        scale_type=row.scale_type,
        normalization_key=row.normalization_key,
        unit=row.unit,
        region=row.region,
        language=row.language,
        provider_version=row.provider_version,
        provider_published_at=row.provider_published_at,
        first_available_at=row.first_available_at,
        collected_at=row.collected_at,
        ingested_at=row.ingested_at,
        revision=row.revision,
        revision_kind=row.revision_kind.value,
        is_current=row.is_current,
        supersedes_id=row.supersedes_id,
        quality_status=row.quality_status,
        is_synthetic=row.is_synthetic,
        is_proxy=row.is_proxy,
        source_reference=row.source_reference,
        data_policy_id=row.data_policy_id,
        job_id=str(row.job_id) if row.job_id else None,
        expires_at=row.expires_at,
    )


class TimeSeriesRepository:
    def __init__(self, session: Session) -> None:
        self._s = session
        # series_key -> id for this unit of work: one job's values share a few series, so
        # the series upsert runs once per series instead of once per observation.
        self._series_ids: dict[str, int] = {}

    # ------------------------------------------------------------------ series
    def ensure_series(self, draft: ObservationDraft) -> tuple[int, str]:
        identity = draft.identity()
        key = identity.key()
        cached = self._series_ids.get(key)
        if cached is not None:
            return cached, key
        series_id = self._upsert_series(draft, identity, key)
        self._series_ids[key] = series_id
        return series_id, key

    def _upsert_series(self, draft: ObservationDraft, identity: Any, key: str) -> int:
        stmt = (
            pg_insert(SeriesRow)
            .values(
                series_key=key,
                target_id=draft.target_id,
                query_id=draft.query_id,
                provider_name=draft.provider_name,
                measurement_category=draft.measurement_category,
                simulated_category=draft.simulated_category,
                is_proxy=draft.is_proxy,
                is_synthetic=draft.is_synthetic,
                search_property=draft.search_property,
                region=draft.region,
                language=draft.language,
                granularity=draft.granularity,
                scale_type=draft.scale_type,
                unit=draft.unit,
                normalization_key=identity.normalization_key,
                normalization_context=draft.normalization_context.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=[SeriesRow.series_key])
            .returning(SeriesRow.id)
        )
        new_id = self._s.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            return int(new_id)
        row = self._s.execute(
            sa.select(SeriesRow.id, SeriesRow.target_id).where(SeriesRow.series_key == key)
        ).one()
        if row.target_id is None and draft.target_id is not None:
            self._s.execute(
                sa.update(SeriesRow).where(SeriesRow.id == row.id).values(target_id=draft.target_id)
            )
        return int(row.id)

    # ------------------------------------------------------------------ writes
    def write(
        self,
        draft: ObservationDraft,
        *,
        revision_behaviour: RevisionBehaviour,
        ingested_at: datetime,
        job_id: uuid.UUID | None = None,
    ) -> WriteResult:
        series_id, series_key = self.ensure_series(draft)
        t = ObservationRow
        base: dict[str, Any] = {
            "series_id": series_id,
            "target_id": draft.target_id,
            "query_id": draft.query_id,
            "provider_name": draft.provider_name,
            "provider_version": draft.provider_version,
            "measurement_category": draft.measurement_category,
            "simulated_category": draft.simulated_category,
            "is_proxy": draft.is_proxy,
            "unit": draft.unit,
            "dedup_key": _dedup_key(series_key, draft.period_start, draft.period_end),
            "provider_observation_id": draft.provider_observation_id,
            "period_start": draft.period_start,
            "period_end": draft.period_end,
            "granularity": draft.granularity,
            "event_time": draft.event_time,
            "source_timezone": draft.source_timezone,
            "provider_published_at": draft.provider_published_at,
            "first_available_at": draft.first_available_at,
            "collected_at": draft.collected_at,
            "ingested_at": max(ingested_at, draft.collected_at),
            "value": draft.value,
            "raw_value": draft.raw_value,
            "scale_type": draft.scale_type,
            "normalization_key": draft.normalization_context.key(),
            "region": draft.region,
            "language": draft.language,
            "search_property": draft.search_property,
            "data_nature": draft.data_nature,
            "sample_quality": draft.sample_quality,
            "source_reference": draft.source_reference,
            "data_policy_id": draft.data_policy_id,
            "is_synthetic": draft.is_synthetic,
            "last_confirmed_at": draft.collected_at,
            "confirm_count": 1,
            "job_id": job_id,
            "expires_at": draft.expires_at,
        }
        inserted = self._s.execute(
            pg_insert(t)
            .values(
                **base,
                revision=1,
                revision_kind=RevisionKind.INITIAL,
                is_current=True,
                quality_status=draft.quality_status(),
            )
            .on_conflict_do_nothing(
                index_elements=[t.series_id, t.period_start, t.period_end],
                index_where=sa.text("is_current"),
            )
            .returning(t.id)
        ).scalar_one_or_none()
        if inserted is not None:
            return WriteResult(WriteOutcome.NEW, int(inserted), series_id, 1)

        current = self._s.execute(
            sa.select(t)
            .where(
                t.series_id == series_id,
                t.period_start == draft.period_start,
                t.period_end == draft.period_end,
                t.is_current.is_(True),
            )
            .with_for_update()
        ).scalar_one()
        if draft.same_measurement(current.value, current.sample_quality):
            current.confirm_count += 1
            current.last_confirmed_at = max(current.last_confirmed_at, draft.collected_at)
            if current.expires_at is not None and draft.expires_at is not None:
                current.expires_at = max(current.expires_at, draft.expires_at)
            return WriteResult(WriteOutcome.UNCHANGED, current.id, series_id, current.revision)

        max_revision = int(
            self._s.execute(
                sa.select(sa.func.max(t.revision)).where(
                    t.series_id == series_id,
                    t.period_start == draft.period_start,
                    t.period_end == draft.period_end,
                )
            ).scalar_one()
        )
        in_progress = current.sample_quality is SampleQuality.PARTIAL_PERIOD
        revisable = in_progress or revision_behaviour in (
            RevisionBehaviour.REVISABLE,
            RevisionBehaviour.UNKNOWN,
        )
        if not revisable:
            # Immutable source contradicted itself: keep the original as current, store the
            # contradiction (once) for lineage and quality reporting.
            same_conflict = self._s.execute(
                sa.select(t.id).where(
                    t.series_id == series_id,
                    t.period_start == draft.period_start,
                    t.period_end == draft.period_end,
                    t.revision_kind == RevisionKind.CONFLICT,
                    t.value.is_not_distinct_from(draft.value),
                    t.sample_quality == draft.sample_quality,
                )
            ).scalar_one_or_none()
            if same_conflict is not None:
                return WriteResult(WriteOutcome.UNCHANGED, int(same_conflict), series_id, 0)
            conflict_id = self._s.execute(
                sa.insert(t)
                .values(
                    **base,
                    revision=max_revision + 1,
                    revision_kind=RevisionKind.CONFLICT,
                    is_current=False,
                    supersedes_id=current.id,
                    quality_status=QualityStatus.CONFLICTING,
                )
                .returning(t.id)
            ).scalar_one()
            return WriteResult(
                WriteOutcome.CONFLICTING, int(conflict_id), series_id, max_revision + 1
            )

        current.is_current = False
        current.superseded_at = base["ingested_at"]
        self._s.flush()
        first_available = current.first_available_at or current.provider_published_at
        new_id = self._s.execute(
            sa.insert(t)
            .values(
                **{**base, "first_available_at": base["first_available_at"] or first_available},
                revision=max_revision + 1,
                revision_kind=RevisionKind.REVISION,
                is_current=True,
                supersedes_id=current.id,
                quality_status=draft.quality_status(),
            )
            .returning(t.id)
        ).scalar_one()
        return WriteResult(WriteOutcome.REVISED, int(new_id), series_id, max_revision + 1)

    # ------------------------------------------------------------------ reads
    def points(
        self,
        *,
        series_ids: Sequence[int] | None = None,
        target_id: int | None = None,
        query_id: int | None = None,
        provider_name: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        current_only: bool = True,
        after_id: int | None = None,
        limit: int = 10_000,
    ) -> list[StoredPoint]:
        """Historical range query (keyset-paginated by period then id)."""
        t = ObservationRow
        stmt = sa.select(t)
        if series_ids is not None:
            stmt = stmt.where(t.series_id.in_(list(series_ids)))
        if target_id is not None:
            stmt = stmt.where(t.target_id == target_id)
        if query_id is not None:
            stmt = stmt.where(t.query_id == query_id)
        if provider_name is not None:
            stmt = stmt.where(t.provider_name == provider_name)
        if start is not None:
            stmt = stmt.where(t.period_end > start)
        if end is not None:
            stmt = stmt.where(t.period_start < end)
        if current_only:
            stmt = stmt.where(t.is_current.is_(True))
        if after_id is not None:
            stmt = stmt.where(t.id > after_id)
        stmt = stmt.order_by(t.series_id, t.period_start, t.revision, t.id).limit(limit)
        return [_point(r) for r in self._s.execute(stmt).scalars()]

    def lineage(self, observation_id: int) -> list[StoredPoint]:
        """All revisions/conflicts for the window of ``observation_id`` (oldest first)."""
        t = ObservationRow
        anchor = self._s.get(t, observation_id)
        if anchor is None:
            return []
        rows = self._s.execute(
            sa.select(t)
            .where(
                t.series_id == anchor.series_id,
                t.period_start == anchor.period_start,
                t.period_end == anchor.period_end,
            )
            .order_by(t.revision)
        ).scalars()
        return [_point(r) for r in rows]

    def series_summaries(
        self,
        *,
        target_ids: Iterable[int] | None = None,
        query_id: int | None = None,
        series_ids: Iterable[int] | None = None,
    ) -> list[SeriesSummary]:
        s, t = SeriesRow, ObservationRow
        current = t.is_current.is_(True)
        agg = (
            sa.select(
                t.series_id.label("sid"),
                sa.func.count().filter(current).label("n"),
                sa.func.count().filter(current, t.value.is_not(None)).label("with_value"),
                sa.func.count()
                .filter(current, t.sample_quality.in_(["missing", "suppressed"]))
                .label("missing"),
                sa.func.count().filter(current, t.sample_quality == "low_volume").label("low"),
                sa.func.count().filter(current, t.sample_quality == "partial_period").label("part"),
                sa.func.count().filter(t.revision_kind == RevisionKind.REVISION).label("rev"),
                sa.func.count().filter(t.revision_kind == RevisionKind.CONFLICT).label("conf"),
                sa.func.min(t.period_start).filter(current).label("first"),
                sa.func.max(t.period_end).filter(current).label("last"),
                sa.func.max(t.collected_at).label("collected"),
            )
            .group_by(t.series_id)
            .subquery()
        )
        stmt = sa.select(s, agg).outerjoin(agg, agg.c.sid == s.id)
        if target_ids is not None:
            stmt = stmt.where(s.target_id.in_(list(target_ids)))
        if query_id is not None:
            stmt = stmt.where(s.query_id == query_id)
        if series_ids is not None:
            stmt = stmt.where(s.id.in_(list(series_ids)))
        out: list[SeriesSummary] = []
        for row in self._s.execute(stmt.order_by(s.id)).all():
            sr: SeriesRow = row[0]
            out.append(
                SeriesSummary(
                    series_id=sr.id,
                    series_key=sr.series_key,
                    target_id=sr.target_id,
                    query_id=sr.query_id,
                    provider_name=sr.provider_name,
                    measurement_category=sr.measurement_category,
                    simulated_category=sr.simulated_category,
                    is_proxy=sr.is_proxy,
                    is_synthetic=sr.is_synthetic,
                    region=sr.region,
                    language=sr.language,
                    granularity=sr.granularity.value,
                    scale_type=sr.scale_type.value,
                    unit=sr.unit,
                    normalization_key=sr.normalization_key,
                    normalization_context=sr.normalization_context,
                    observations=int(row.n or 0),
                    with_value=int(row.with_value or 0),
                    missing_values=int(row.missing or 0),
                    low_volume=int(row.low or 0),
                    partial=int(row.part or 0),
                    revisions=int(row.rev or 0),
                    conflicts=int(row.conf or 0),
                    first_period=row.first,
                    last_period_end=row.last,
                    last_collected=row.collected,
                )
            )
        return out

    def latest_period_end(self, target_id: int, provider_name: str) -> datetime | None:
        t = ObservationRow
        value: datetime | None = self._s.execute(
            sa.select(sa.func.max(t.period_end)).where(
                t.target_id == target_id,
                t.provider_name == provider_name,
                t.is_current.is_(True),
                t.sample_quality != SampleQuality.PARTIAL_PERIOD,
            )
        ).scalar_one()
        return value

    def delay_samples(
        self, provider_name: str, *, target_id: int | None = None, limit: int = 200
    ) -> list[tuple[datetime, datetime | None, datetime]]:
        """(period_end, provider_published_at, first collected_at) of recent first revisions."""
        t = ObservationRow
        stmt = sa.select(t.period_end, t.provider_published_at, t.collected_at).where(
            t.provider_name == provider_name,
            t.revision == 1,
            t.sample_quality != SampleQuality.PARTIAL_PERIOD,
        )
        if target_id is not None:
            stmt = stmt.where(t.target_id == target_id)
        rows = self._s.execute(stmt.order_by(t.period_end.desc()).limit(limit)).all()
        return [(r[0], r[1], r[2]) for r in rows]

    def arrival_order(self, series_id: int) -> list[tuple[datetime, datetime]]:
        """(period_start, ingested_at) of first revisions, by ingestion order."""
        t = ObservationRow
        rows = self._s.execute(
            sa.select(t.period_start, t.ingested_at)
            .where(t.series_id == series_id, t.revision == 1)
            .order_by(t.ingested_at, t.id)
        ).all()
        return [(r[0], r[1]) for r in rows]

    def provider_versions(self, series_id: int) -> list[str]:
        t = ObservationRow
        return [
            v
            for (v,) in self._s.execute(
                sa.select(t.provider_version)
                .where(t.series_id == series_id)
                .group_by(t.provider_version)
                .order_by(sa.func.min(t.collected_at))
            ).all()
        ]

    def count(self) -> int:
        return int(
            self._s.execute(sa.select(sa.func.count()).select_from(ObservationRow)).scalar_one()
        )

    def count_expired(self, now: datetime) -> int:
        t = ObservationRow
        return int(
            self._s.execute(
                sa.select(sa.func.count()).select_from(t).where(t.expires_at <= now)
            ).scalar_one()
        )

    def purge_expired(self, now: datetime) -> int:
        """Delete expired observations (all revisions are covered by their own expiry)."""
        t = ObservationRow
        result = self._s.execute(sa.delete(t).where(t.expires_at <= now))
        return int(getattr(result, "rowcount", 0) or 0)

    def coverage_counts(self) -> dict[str, Any]:
        s_, t = SeriesRow, ObservationRow
        series = self._s.execute(
            sa.select(
                s_.measurement_category, s_.is_proxy, s_.is_synthetic, sa.func.count()
            ).group_by(s_.measurement_category, s_.is_proxy, s_.is_synthetic)
        ).all()
        obs = self._s.execute(
            sa.select(
                t.measurement_category,
                sa.func.count(),
                sa.func.count().filter(t.is_current.is_(True)),
                sa.func.count().filter(t.revision_kind == RevisionKind.REVISION),
                sa.func.count().filter(t.revision_kind == RevisionKind.CONFLICT),
            ).group_by(t.measurement_category)
        ).all()
        return {
            "series": [
                {
                    "category": r[0].value,
                    "proxy": bool(r[1]),
                    "synthetic": bool(r[2]),
                    "count": int(r[3]),
                }
                for r in series
            ],
            "observations": {
                r[0].value: {
                    "rows": int(r[1]),
                    "current": int(r[2]),
                    "revisions": int(r[3]),
                    "conflicts": int(r[4]),
                }
                for r in obs
            },
        }
