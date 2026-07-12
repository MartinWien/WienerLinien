"""Schemas for /stations."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StationOut(BaseModel):
    id: int = Field(description="Station identifier (the DIVA number from the WL registry).")
    name: str = Field(description="Station name, e.g. 'Karlsplatz'.")
    municipality: str
    latitude: float | None
    longitude: float | None
    stop_count: int = Field(
        description="Number of (RBL, line, direction) stop points at this station."
    )


class StationPage(BaseModel):
    items: list[StationOut]
    total: int = Field(description="Total number of stations matching the search.")
    limit: int
    offset: int
