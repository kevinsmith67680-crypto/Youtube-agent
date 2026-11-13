"""Engine and session management.

Database access is synchronous (psycopg 3). Network-bound provider calls are async and
concurrent; database writes happen afterwards in a single short transaction per
ingestion, so an async driver would add complexity without a measurable benefit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trend_agent.config import Settings
from trend_agent.logging import redact_text

SessionFactory = sessionmaker[Session]


def create_db_engine(url: str, *, connect_timeout: int = 5, echo: bool = False) -> Engine:
    return sa.create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
        connect_args={"connect_timeout": connect_timeout, "options": "-c timezone=UTC"},
    )


def engine_from_settings(settings: Settings) -> Engine:
    return create_db_engine(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.database_connect_timeout_seconds,
        echo=settings.database_echo,
    )


def create_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def transaction(factory: SessionFactory) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection(engine: Engine) -> tuple[bool, str]:
    """Return (ok, detail) without leaking credentials."""
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text("SHOW server_version")).scalar_one()
            tz = conn.execute(sa.text("SHOW timezone")).scalar_one()
            revision = None
            has_alembic = conn.execute(
                sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar_one()
            if has_alembic:
                revision = conn.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        return True, f"PostgreSQL {version}, timezone={tz}, schema revision={revision or 'none'}"
    except Exception as exc:
        return False, redact_text(f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
