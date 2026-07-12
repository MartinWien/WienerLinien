"""Departure read path: cache first, DB snapshot fallback, never upstream.

Freshness contract: a payload older than twice the poll interval is served
with ``stale: true`` (HTTP 200). Countdowns from a DB fallback are recomputed
against the current clock and already-departed entries dropped, so even stale
data is internally consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache, departures_key, get_json
from app.core.config import Settings
from app.models import DepartureSnapshot, Station
from app.models.base import utc_now
from app.schemas.common import CacheSource
from app.schemas.departure import DepartureOut, StationDepartures


@dataclass(frozen=True)
class DeparturesReadResult:
    data: StationDepartures
    fetched_at: datetime | None
    stale: bool
    source: CacheSource


async def get_station(session: AsyncSession, station_id: int) -> Station | None:
    return await session.get(Station, station_id)


async def read_departures(
    *,
    session: AsyncSession,
    cache: Cache,
    settings: Settings,
    station: Station,
) -> DeparturesReadResult:
    cached = await get_json(cache, departures_key(station.diva))
    if cached is not None:
        return _from_cache_payload(station, cached, settings)

    return await _from_db_snapshots(session, station, settings)


def _from_cache_payload(
    station: Station, payload: dict[str, Any], settings: Settings
) -> DeparturesReadResult:
    fetched_at = datetime.fromisoformat(payload["fetched_at"])
    departures = [
        DepartureOut(
            rbl=raw["rbl"],
            line=raw["line"],
            towards=raw["towards"],
            direction=raw["direction"],
            platform=raw.get("platform"),
            vehicle_type=raw.get("vehicle_type"),
            barrier_free=raw.get("barrier_free", False),
            countdown_minutes=raw["countdown_minutes"],
            planned_at=datetime.fromisoformat(raw["planned_at"]),
            realtime_at=(
                datetime.fromisoformat(raw["realtime_at"]) if raw.get("realtime_at") else None
            ),
            delay_seconds=raw.get("delay_seconds"),
        )
        for raw in payload.get("departures", [])
    ]
    return DeparturesReadResult(
        data=StationDepartures(
            station_id=station.diva,
            station_name=payload.get("station_name") or station.name,
            departures=departures,
        ),
        fetched_at=fetched_at,
        stale=_is_stale(fetched_at, settings),
        source="cache",
    )


async def _from_db_snapshots(
    session: AsyncSession, station: Station, settings: Settings
) -> DeparturesReadResult:
    """Rebuild the last published view from the newest persisted poll cycle."""
    latest = await session.execute(
        select(DepartureSnapshot.poll_cycle, DepartureSnapshot.captured_at)
        .where(DepartureSnapshot.station_diva == station.diva)
        .order_by(DepartureSnapshot.captured_at.desc())
        .limit(1)
    )
    row = latest.first()
    if row is None:
        # Nothing polled for this station yet. The router has just registered
        # it with the poller, so data typically arrives within one interval.
        return DeparturesReadResult(
            data=StationDepartures(
                station_id=station.diva,
                station_name=station.name,
                watch_status="pending",
                departures=[],
            ),
            fetched_at=None,
            stale=True,
            source="none",
        )

    poll_cycle, captured_at = row
    snapshots = await session.execute(
        select(DepartureSnapshot)
        .where(
            DepartureSnapshot.station_diva == station.diva,
            DepartureSnapshot.poll_cycle == poll_cycle,
        )
        .order_by(DepartureSnapshot.countdown_minutes)
    )

    now = utc_now()
    departures: list[DepartureOut] = []
    for snapshot in snapshots.scalars():
        best_estimate = snapshot.realtime_at or snapshot.planned_at
        if best_estimate < now - timedelta(minutes=1):
            continue  # already departed; do not resurrect it
        departures.append(
            DepartureOut(
                rbl=snapshot.rbl,
                line=snapshot.line_name,
                towards=snapshot.towards,
                direction=snapshot.direction,
                platform=None,  # not persisted in history; cache payload has it
                vehicle_type=snapshot.vehicle_type,
                barrier_free=snapshot.barrier_free,
                countdown_minutes=max(0, int((best_estimate - now).total_seconds() // 60)),
                planned_at=snapshot.planned_at,
                realtime_at=snapshot.realtime_at,
                delay_seconds=snapshot.drift_seconds,
            )
        )
    return DeparturesReadResult(
        data=StationDepartures(
            station_id=station.diva, station_name=station.name, departures=departures
        ),
        fetched_at=captured_at,
        stale=_is_stale(captured_at, settings),
        source="db",
    )


def _is_stale(fetched_at: datetime, settings: Settings) -> bool:
    age = (utc_now() - fetched_at).total_seconds()
    return age > settings.staleness_threshold_seconds
