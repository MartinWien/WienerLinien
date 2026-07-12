"""Bounded history of observed departures, used for delay statistics.

Deliberately denormalized (station/line names inlined, no FK to the registry
tables): a snapshot is an immutable observation, and keeping it self-contained
lets the registry be reloaded or corrected without touching history. The
table is bounded by time-based retention — the poller deletes rows older than
``WL_SNAPSHOT_RETENTION_MINUTES`` every cycle.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime


class DepartureSnapshot(Base):
    __tablename__ = "departure_snapshots"

    # BIGINT in PostgreSQL (this table accumulates rows fast), but plain
    # INTEGER on SQLite, whose autoincrement only works on INTEGER PKs.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    poll_cycle: Mapped[str] = mapped_column(String(36))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime())

    rbl: Mapped[int] = mapped_column(BigInteger)
    station_diva: Mapped[int] = mapped_column(BigInteger)
    station_name: Mapped[str] = mapped_column(String(255))
    line_name: Mapped[str] = mapped_column(String(32))
    towards: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(1))  # "H" | "R"
    vehicle_type: Mapped[str | None] = mapped_column(String(32))
    barrier_free: Mapped[bool] = mapped_column(Boolean, default=False)

    planned_at: Mapped[datetime] = mapped_column(UTCDateTime())
    realtime_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    countdown_minutes: Mapped[int] = mapped_column(Integer)
    # realtime - planned, in seconds; positive = late. Precomputed at write
    # time so stats aggregation never re-derives it per row.
    drift_seconds: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        # Serves both the stats window query (station + time range) and the
        # "latest snapshot for station" DB fallback on the read path.
        Index("ix_snapshots_station_captured", "station_diva", "captured_at"),
        # Serves the retention sweep (DELETE WHERE captured_at < cutoff).
        Index("ix_snapshots_captured_at", "captured_at"),
    )
