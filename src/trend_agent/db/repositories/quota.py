"""Database-backed quota store, shared by all processes using the same database."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from trend_agent.db.models import ProviderRateLimitRow
from trend_agent.db.session import SessionFactory, transaction
from trend_agent.providers.quota import QuotaWindow

T = ProviderRateLimitRow


class SqlQuotaStore:
    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def _ensure_row(
        self, session: sa.orm.Session, provider: str, bucket: str, window: QuotaWindow, limit: int
    ) -> None:
        session.execute(
            pg_insert(T)
            .values(
                provider_name=provider,
                bucket=bucket,
                window_start=window.start,
                window_end=window.end,
                effective_limit=limit,
                units_used=0,
            )
            .on_conflict_do_nothing(constraint="uq_provider_rate_limits_window")
        )

    def try_consume(
        self, *, provider: str, bucket: str, window: QuotaWindow, limit: int, cost: int
    ) -> tuple[bool, int]:
        with transaction(self._factory) as session:
            self._ensure_row(session, provider, bucket, window, limit)
            # Conditional increment: atomic under concurrent writers because PostgreSQL
            # re-evaluates the WHERE clause after acquiring the row lock.
            used_after = session.execute(
                sa.update(T)
                .where(
                    T.provider_name == provider,
                    T.bucket == bucket,
                    T.window_start == window.start,
                    T.server_exhausted.is_(False),
                    T.units_used + cost <= limit,
                )
                .values(
                    units_used=T.units_used + cost, effective_limit=limit, updated_at=sa.func.now()
                )
                .returning(T.units_used)
            ).scalar_one_or_none()
            if used_after is not None:
                return True, int(used_after)
            used = session.execute(
                sa.select(T.units_used).where(
                    T.provider_name == provider, T.bucket == bucket, T.window_start == window.start
                )
            ).scalar_one()
            return False, int(used)

    def mark_exhausted(self, *, provider: str, bucket: str, window: QuotaWindow) -> None:
        with transaction(self._factory) as session:
            self._ensure_row(session, provider, bucket, window, 0)
            session.execute(
                sa.update(T)
                .where(
                    T.provider_name == provider, T.bucket == bucket, T.window_start == window.start
                )
                .values(
                    server_exhausted=True,
                    server_exhausted_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
            )

    def get_usage(self, *, provider: str, bucket: str, window: QuotaWindow) -> tuple[int, bool]:
        with transaction(self._factory) as session:
            row = session.execute(
                sa.select(T.units_used, T.server_exhausted).where(
                    T.provider_name == provider, T.bucket == bucket, T.window_start == window.start
                )
            ).one_or_none()
        if row is None:
            return 0, False
        return int(row.units_used), bool(row.server_exhausted)

    def history(
        self, provider: str | None = None, limit: int = 30
    ) -> Sequence[ProviderRateLimitRow]:
        with transaction(self._factory) as session:
            stmt = (
                sa.select(T).order_by(T.window_start.desc(), T.provider_name, T.bucket).limit(limit)
            )
            if provider is not None:
                stmt = stmt.where(T.provider_name == provider)
            return session.execute(stmt).scalars().all()
