"""Repositories for providers, queries and ingestion runs."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.models import (
    IngestionRunRow,
    ProviderRow,
    RejectedRecordRow,
    SearchQueryRow,
)
from trend_agent.logging import storable
from trend_agent.schemas.enums import IngestionStatus, RecordKind, RejectionReason
from trend_agent.schemas.provider import ProviderDescriptor


class ProviderRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def sync(self, descriptor: ProviderDescriptor) -> int:
        """Insert or update the provider row from its descriptor; return its id."""
        values = {
            "name": descriptor.name,
            "display_name": descriptor.display_name,
            "version": descriptor.version,
            "provider_type": descriptor.provider_type,
            "enabled": descriptor.enabled,
            "capabilities": sorted(c.value for c in descriptor.capabilities),
            "descriptor": descriptor.model_dump(mode="json"),
        }
        insert = pg_insert(ProviderRow).values(**values)
        stmt = insert.on_conflict_do_update(
            index_elements=[ProviderRow.name],
            set_={
                **{k: insert.excluded[k] for k in values if k != "name"},
                "updated_at": sa.func.now(),
            },
        ).returning(ProviderRow.id)
        return int(self._s.execute(stmt).scalar_one())

    def get_id(self, name: str) -> int | None:
        return self._s.execute(
            sa.select(ProviderRow.id).where(ProviderRow.name == name)
        ).scalar_one_or_none()

    def list(self) -> Sequence[ProviderRow]:
        return self._s.execute(sa.select(ProviderRow).order_by(ProviderRow.name)).scalars().all()


class QueryRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_or_create(
        self, canonical: str, original: str, language: str | None, region: str | None
    ) -> int:
        stmt = (
            pg_insert(SearchQueryRow)
            .values(
                canonical_query=canonical, original_query=original, language=language, region=region
            )
            .on_conflict_do_nothing(constraint="uq_search_queries_canonical_language_region")
            .returning(SearchQueryRow.id)
        )
        new_id = self._s.execute(stmt).scalar_one_or_none()
        if new_id is not None:
            return int(new_id)
        existing = self.find_id(canonical, language, region)
        if existing is None:  # pragma: no cover - would indicate a constraint mismatch
            raise RuntimeError("query row vanished after conflict")
        return existing

    def find_id(self, canonical: str, language: str | None, region: str | None) -> int | None:
        return self._s.execute(
            sa.select(SearchQueryRow.id).where(
                SearchQueryRow.canonical_query == canonical,
                SearchQueryRow.language.is_not_distinct_from(language),
                SearchQueryRow.region.is_not_distinct_from(region),
            )
        ).scalar_one_or_none()

    def find_all(self, canonical: str) -> Sequence[SearchQueryRow]:
        return (
            self._s.execute(
                sa.select(SearchQueryRow)
                .where(SearchQueryRow.canonical_query == canonical)
                .order_by(SearchQueryRow.id)
            )
            .scalars()
            .all()
        )


@dataclass(frozen=True)
class RejectionRecord:
    record_kind: RecordKind
    reason_code: RejectionReason
    reason_detail: str
    payload: dict[str, Any]


class IngestionRunRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def start(
        self,
        *,
        provider_id: int | None,
        provider_name: str,
        operation: str,
        request_params: dict[str, Any],
        started_at: datetime,
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        self._s.add(
            IngestionRunRow(
                id=run_id,
                provider_id=provider_id,
                provider_name=provider_name,
                operation=operation,
                request_params=storable(request_params),
                started_at=started_at,
                status=IngestionStatus.RUNNING,
            )
        )
        self._s.flush()
        return run_id

    def finish(
        self,
        run_id: uuid.UUID,
        *,
        status: IngestionStatus,
        completed_at: datetime,
        received: int,
        accepted: int,
        rejected: int,
        duplicate: int,
        withheld: int,
        quota_used: int,
        quota_detail: dict[str, int],
        error_type: str | None,
        error_message: str | None,
        summary: dict[str, Any],
    ) -> None:
        self._s.execute(
            sa.update(IngestionRunRow)
            .where(IngestionRunRow.id == run_id)
            .values(
                status=status,
                completed_at=completed_at,
                records_received=received,
                records_accepted=accepted,
                records_rejected=rejected,
                records_duplicate=duplicate,
                records_withheld=withheld,
                estimated_quota_used=quota_used,
                quota_detail=quota_detail,
                error_type=error_type,
                error_message=error_message,
                summary=storable(summary),
            )
        )

    def add_rejections(
        self,
        run_id: uuid.UUID,
        provider_id: int | None,
        rejections: Sequence[RejectionRecord],
        expires_at: datetime | None,
    ) -> int:
        for item in rejections:
            self._s.add(
                RejectedRecordRow(
                    ingestion_run_id=run_id,
                    provider_id=provider_id,
                    record_kind=item.record_kind,
                    reason_code=item.reason_code,
                    reason_detail=item.reason_detail[:2000],
                    payload=storable(item.payload),
                    expires_at=expires_at,
                )
            )
        self._s.flush()
        return len(rejections)

    def get(self, run_id: uuid.UUID) -> IngestionRunRow | None:
        return self._s.get(IngestionRunRow, run_id)

    def recent(self, limit: int = 10) -> Sequence[IngestionRunRow]:
        return (
            self._s.execute(
                sa.select(IngestionRunRow).order_by(IngestionRunRow.started_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )

    def rejections(self, run_id: uuid.UUID) -> Sequence[RejectedRecordRow]:
        return (
            self._s.execute(
                sa.select(RejectedRecordRow)
                .where(RejectedRecordRow.ingestion_run_id == run_id)
                .order_by(RejectedRecordRow.id)
            )
            .scalars()
            .all()
        )
