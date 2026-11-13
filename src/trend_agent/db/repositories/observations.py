"""Search-interest observation persistence (idempotent, conflict-aware)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trend_agent.db.models import ProviderRow, SearchInterestObservationRow, SearchQueryRow
from trend_agent.schemas.observation import NormalizationContext, ValidatedObservation


class InsertResult(StrEnum):
    INSERTED = "inserted"
    ALREADY_PRESENT = "already_present"  # identical record already stored (idempotent re-run)
    CONFLICT = "conflict"  # same identity, different measurement: never overwritten


@dataclass(frozen=True)
class StoredObservation:
    id: int
    observation: ValidatedObservation
    ingestion_run_id: uuid.UUID | None
    expires_at: datetime | None
    created_at: datetime


class ObservationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def insert(
        self,
        obs: ValidatedObservation,
        *,
        query_id: int,
        provider_id: int,
        run_id: uuid.UUID | None,
        expires_at: datetime | None,
    ) -> tuple[InsertResult, str]:
        values = {
            "query_id": query_id,
            "provider_id": provider_id,
            "ingestion_run_id": run_id,
            "provider_version": obs.provider_version,
            "measurement_type": obs.measurement_type,
            "search_property": obs.search_property,
            "granularity": obs.granularity,
            "observation_start": obs.observation_start,
            "observation_end": obs.observation_end,
            "collected_at": obs.collected_at,
            "available_at": obs.available_at,
            "value": obs.value,
            "scale_type": obs.scale_type,
            "normalization_context": obs.normalization_context.model_dump(mode="json"),
            "normalization_key": obs.normalization_key,
            "geography": obs.geography,
            "language": obs.language,
            "sample_quality": obs.sample_quality,
            "data_nature": obs.data_nature,
            "is_synthetic": obs.is_synthetic,
            "source_reference": obs.source_reference,
            "expires_at": expires_at,
        }
        stmt = (
            pg_insert(SearchInterestObservationRow)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_search_interest_observations_identity")
            .returning(SearchInterestObservationRow.id)
        )
        if self._s.execute(stmt).scalar_one_or_none() is not None:
            return InsertResult.INSERTED, ""

        t = SearchInterestObservationRow
        existing = self._s.execute(
            sa.select(t.value, t.sample_quality, t.data_nature, t.id).where(
                t.query_id == query_id,
                t.provider_id == provider_id,
                t.measurement_type == obs.measurement_type,
                t.search_property == obs.search_property,
                t.geography == obs.geography,
                t.language.is_not_distinct_from(obs.language),
                t.granularity == obs.granularity,
                t.observation_start == obs.observation_start,
                t.observation_end == obs.observation_end,
                t.normalization_key == obs.normalization_key,
            )
        ).one()
        if (
            existing.value == obs.value
            and existing.sample_quality == obs.sample_quality
            and existing.data_nature == obs.data_nature
        ):
            return InsertResult.ALREADY_PRESENT, ""
        detail = (
            f"stored observation id={existing.id} has value={existing.value} "
            f"quality={existing.sample_quality.value}; incoming value={obs.value} "
            f"quality={obs.sample_quality.value}. Stored value kept."
        )
        return InsertResult.CONFLICT, detail

    def list_for_query(
        self,
        canonical_query: str,
        *,
        language: str | None = None,
        region: str | None = None,
        provider_name: str | None = None,
        include_synthetic: bool = True,
        limit: int = 5000,
    ) -> list[StoredObservation]:
        t = SearchInterestObservationRow
        q = SearchQueryRow
        stmt = (
            sa.select(t, q, ProviderRow.name)
            .join(q, q.id == t.query_id)
            .join(ProviderRow, ProviderRow.id == t.provider_id)
            .where(q.canonical_query == canonical_query)
        )
        if language is not None:
            stmt = stmt.where(q.language == language)
        if region is not None:
            stmt = stmt.where(q.region == region)
        if provider_name is not None:
            stmt = stmt.where(ProviderRow.name == provider_name)
        if not include_synthetic:
            stmt = stmt.where(t.is_synthetic.is_(False))
        stmt = stmt.order_by(
            ProviderRow.name, t.normalization_key, t.observation_start, t.id
        ).limit(limit)
        results: list[StoredObservation] = []
        for row, query, provider in self._s.execute(stmt).all():
            results.append(
                StoredObservation(
                    id=row.id,
                    observation=ValidatedObservation(
                        canonical_query=query.canonical_query,
                        original_query=query.original_query,
                        query_language=query.language,
                        query_region=query.region,
                        provider_name=provider,
                        provider_version=row.provider_version,
                        measurement_type=row.measurement_type,
                        search_property=row.search_property,
                        observation_start=row.observation_start,
                        observation_end=row.observation_end,
                        granularity=row.granularity,
                        collected_at=row.collected_at,
                        available_at=row.available_at,
                        value=row.value,
                        scale_type=row.scale_type,
                        normalization_context=NormalizationContext.model_validate(
                            row.normalization_context
                        ),
                        geography=row.geography,
                        language=row.language,
                        sample_quality=row.sample_quality,
                        data_nature=row.data_nature,
                        source_reference=row.source_reference,
                    ),
                    ingestion_run_id=row.ingestion_run_id,
                    expires_at=row.expires_at,
                    created_at=row.created_at,
                )
            )
        return results

    def count_expired(self, now: datetime) -> int:
        t = SearchInterestObservationRow
        return int(
            self._s.execute(
                sa.select(sa.func.count()).select_from(t).where(t.expires_at <= now)
            ).scalar_one()
        )

    def delete_expired(self, now: datetime) -> int:
        t = SearchInterestObservationRow
        result = self._s.execute(sa.delete(t).where(t.expires_at <= now))
        return int(getattr(result, "rowcount", 0) or 0)
