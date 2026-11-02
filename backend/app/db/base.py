"""Declarative base, shared column types and naming conventions."""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB on PostgreSQL, plain JSON elsewhere (unit tests run on SQLite).
JSONType = JSON().with_variant(JSONB(), "postgresql")

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING)


def utcnow_col() -> Mapped[datetime]:
    """A timezone-aware, server-defaulted creation timestamp column."""
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_col() -> Mapped[datetime]:
    """Like utcnow_col but refreshed on every ORM UPDATE."""
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
