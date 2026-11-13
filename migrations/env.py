"""Alembic environment: URL from application settings, metadata from the ORM models."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from trend_agent.config import Settings
from trend_agent.db import models  # noqa: F401  (registers tables on Base.metadata)
from trend_agent.db.base import Base
from trend_agent.db.session import create_db_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # An explicit URL (e.g. from the test-suite or `trend-agent db upgrade`) wins.
    override = config.attributes.get("database_url") or config.get_main_option("sqlalchemy.url")
    if override:
        return str(override)
    return Settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
