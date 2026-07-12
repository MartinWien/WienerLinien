"""Static registry entities, loaded from the Wiener Linien open-data CSVs.

Naming note: the upstream CSVs identify stations by ``DIVA`` (a stable
Austria-wide stop identifier) and stop points by ``StopID`` — the value the
real-time monitor API accepts as its ``rbl`` parameter. One station (DIVA)
fans out to many stop points: one per platform/line/direction. That fan-out
is exactly the ``stops`` table.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Station(Base):
    """A physical station/stop location (``wienerlinien-ogd-haltestellen.csv``)."""

    __tablename__ = "stations"

    diva: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))
    municipality: Mapped[str] = mapped_column(String(128))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    stops: Mapped[list[Stop]] = relationship(back_populates="station")

    # Station search is the primary read pattern for /stations.
    __table_args__ = (Index("ix_stations_name", "name"),)


class Line(Base):
    """A transport line (``wienerlinien-ogd-linien.csv``)."""

    __tablename__ = "lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(32))
    sorting: Mapped[int] = mapped_column(Integer, default=0)
    realtime: Mapped[bool] = mapped_column(Boolean, default=False)
    means_of_transport: Mapped[str] = mapped_column(String(32))


class Stop(Base):
    """One (RBL/StopID, line, direction) triple a monitor feed can be polled for.

    Derived from ``haltepunkte.csv`` (stop points) joined with
    ``fahrwegverlaeufe.csv`` (route sequences). The same RBL appears in
    multiple rows when several lines serve one platform.
    """

    __tablename__ = "stops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rbl: Mapped[int] = mapped_column(BigInteger)
    station_diva: Mapped[int] = mapped_column(ForeignKey("stations.diva", ondelete="CASCADE"))
    line_id: Mapped[int] = mapped_column(ForeignKey("lines.id", ondelete="CASCADE"))
    direction: Mapped[int] = mapped_column(Integer)  # 1 = H(in), 2 = R(ück) in WL terms
    name: Mapped[str] = mapped_column(String(255))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    station: Mapped[Station] = relationship(back_populates="stops")
    line: Mapped[Line] = relationship()

    __table_args__ = (
        UniqueConstraint("rbl", "line_id", "direction", name="uq_stops_rbl_line_direction"),
        # The poller resolves station -> RBLs; the normalizer maps RBL -> station.
        Index("ix_stops_station_diva", "station_diva"),
        Index("ix_stops_rbl", "rbl"),
    )
