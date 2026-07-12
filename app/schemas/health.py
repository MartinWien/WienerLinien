"""Schemas for /health."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DbHealth(BaseModel):
    ok: bool
    error: str | None = None


class CacheHealth(BaseModel):
    backend: str
    ok: bool
    hits: int
    misses: int
    sets: int
    errors: int


class PollerHealth(BaseModel):
    enabled: bool
    running: bool
    watched_stations: list[str]
    watched_stops: int
    dynamic_stations: int = Field(
        default=0, description="Stations currently watched on demand (not pinned via config)."
    )
    cycles_completed: int
    last_cycle_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


class UpstreamHealth(BaseModel):
    circuit_state: str = Field(description="closed | open | half_open")
    consecutive_failures: int
    last_latency_ms: int | None = None


class HealthOut(BaseModel):
    status: str = Field(description="'ok' or 'degraded'. Degraded still serves cached data.")
    database: DbHealth
    cache: CacheHealth
    poller: PollerHealth
    upstream: UpstreamHealth
