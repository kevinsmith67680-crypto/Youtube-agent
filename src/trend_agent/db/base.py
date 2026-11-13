"""SQLAlchemy declarative base and column helpers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: sa.DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def str_enum(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """Store a StrEnum as VARCHAR + CHECK constraint (not a native PG enum).

    Native enums require ALTER TYPE migrations to extend; a CHECK constraint is just as
    strict and simpler to evolve.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=40,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
    )


def utc_now_sql() -> sa.TextClause:
    return sa.text("now()")
