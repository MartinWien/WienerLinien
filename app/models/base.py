"""Declarative base and shared column types.

All datetimes are stored as naive UTC. SQLite has no timezone support, so
mixing aware and naive values there causes subtle comparison bugs; storing a
single convention and converting at the edges (``UTCDateTime``) keeps SQLite
and PostgreSQL behaviour identical.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as naive UTC, returns them aware again."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: Any | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime; use aware UTC datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)
