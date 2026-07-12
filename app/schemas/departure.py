"""Schemas for /stations/{id}/departures."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DepartureOut(BaseModel):
    rbl: int = Field(description="Stop point (RBL/StopID) this departure was observed at.")
    line: str = Field(description="Line name, e.g. 'U1', '62', '4A'.")
    towards: str = Field(description="Destination shown on the vehicle.")
    direction: str = Field(description="'H' (Hin) or 'R' (Rück) as reported by the feed.")
    platform: str | None = None
    vehicle_type: str | None = Field(
        default=None, description="e.g. 'ptMetro', 'ptTram', 'ptBusCity'."
    )
    barrier_free: bool = False
    countdown_minutes: int = Field(description="Minutes until departure, as reported/derived.")
    planned_at: datetime = Field(description="Timetable departure time.")
    realtime_at: datetime | None = Field(
        default=None, description="Real-time estimate; null if the stop has no realtime data."
    )
    delay_seconds: int | None = Field(
        default=None,
        description="realtime_at - planned_at in seconds (positive = late); null without realtime.",
    )


class StationDepartures(BaseModel):
    station_id: int
    station_name: str
    watch_status: Literal["active", "pending"] = Field(
        default="active",
        description="'active' when data for this station exists. 'pending' when the station "
        "was just added to the poll set on demand — retry in one poll interval.",
    )
    departures: list[DepartureOut] = Field(
        description="Upcoming departures across all stop points of the station, soonest first."
    )
