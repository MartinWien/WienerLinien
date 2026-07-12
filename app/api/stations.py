"""Station endpoints: search, departures, delay stats, prediction baseline."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_cache, get_poller, get_session, get_settings
from app.core.cache import Cache
from app.core.config import Settings
from app.ingestion.poller import Poller
from app.models import Station, Stop
from app.models.base import utc_now
from app.schemas.common import Envelope, make_meta
from app.schemas.departure import StationDepartures
from app.schemas.station import StationOut, StationPage
from app.schemas.stats import StationPrediction, StationStats
from app.services.departures import get_station, read_departures
from app.services.stats import compute_station_prediction, compute_station_stats

router = APIRouter(prefix="/stations", tags=["stations"])


async def _require_station(station_id: int, session: AsyncSession) -> Station:
    station = await get_station(session, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail=f"unknown station id {station_id}")
    return station


@router.get(
    "",
    response_model=Envelope[StationPage],
    summary="Search stations by name (paginated)",
)
async def list_stations(
    search: str | None = Query(
        default=None,
        min_length=1,
        max_length=100,
        description="Case-insensitive substring match on station name.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> Envelope[StationPage]:
    filters = []
    # Alphabetical for browsing; relevance (exact > prefix > substring, then
    # shortest name) when searching, so "Karlsplatz" beats "Oper, Karlsplatz".
    ordering: list[Any] = [Station.name, Station.diva]
    if search:
        filters.append(Station.name.ilike(f"%{search}%"))
        relevance = case(
            (func.lower(Station.name) == search.lower(), 0),
            (Station.name.ilike(f"{search}%"), 1),
            else_=2,
        )
        ordering = [relevance, func.length(Station.name), Station.name, Station.diva]

    total = await session.scalar(select(func.count(Station.diva)).where(*filters)) or 0
    rows = await session.execute(
        select(Station, func.count(Stop.id))
        .outerjoin(Stop, Stop.station_diva == Station.diva)
        .where(*filters)
        .group_by(Station.diva)
        .order_by(*ordering)
        .limit(limit)
        .offset(offset)
    )
    items = [
        StationOut(
            id=station.diva,
            name=station.name,
            municipality=station.municipality,
            latitude=station.latitude,
            longitude=station.longitude,
            stop_count=stop_count,
        )
        for station, stop_count in rows
    ]
    return Envelope(
        meta=make_meta(fetched_at=None, stale=False, source="registry"),
        data=StationPage(items=items, total=total, limit=limit, offset=offset),
    )


@router.get(
    "/{station_id}/departures",
    response_model=Envelope[StationDepartures],
    summary="Upcoming departures for a station",
    description="Served from the poller's cache, falling back to the last persisted "
    "snapshot. Never triggers an upstream call: if the Wiener Linien feed is down, "
    "the last known data is returned with `meta.stale: true`. Requesting a station "
    "that is not being watched adds it to the poll set on demand: the first response "
    "has `watch_status: pending` and data appears within one poll interval.",
)
async def station_departures(
    station_id: int,
    session: AsyncSession = Depends(get_session),
    cache: Cache = Depends(get_cache),
    settings: Settings = Depends(get_settings),
    poller: Poller = Depends(get_poller),
) -> Envelope[StationDepartures]:
    station = await _require_station(station_id, session)
    # Register demand before reading: a miss below then means "not yet",
    # not "never" — the station joins the next poll cycle.
    poller.request_station(station.diva, station.name)
    result = await read_departures(session=session, cache=cache, settings=settings, station=station)
    return Envelope(
        meta=make_meta(fetched_at=result.fetched_at, stale=result.stale, source=result.source),
        data=result.data,
    )


@router.get(
    "/{station_id}/stats",
    response_model=Envelope[StationStats],
    summary="Aggregated delay statistics from recent history",
    description="Deduplicates repeated observations of the same departure across poll "
    "cycles and aggregates drift (realtime minus planned) per line.",
)
async def station_stats(
    station_id: int,
    window: int = Query(default=60, ge=5, le=1440, description="Look-back window in minutes."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Envelope[StationStats]:
    station = await _require_station(station_id, session)
    stats = await compute_station_stats(
        session=session, settings=settings, station=station, window_minutes=window
    )
    return Envelope(meta=make_meta(fetched_at=None, stale=False, source="db"), data=stats)


@router.get(
    "/{station_id}/predict",
    response_model=Envelope[StationPrediction],
    summary="Expected delay per line (statistical baseline)",
    description="Rolling-mean drift per (line, hour of day, UTC) over the retained "
    "snapshot history. A baseline, not a trained model; lines with fewer than 5 "
    "samples return null.",
)
async def station_predict(
    station_id: int,
    hour: int | None = Query(
        default=None, ge=0, le=23, description="Hour of day (UTC); defaults to the current hour."
    ),
    line: str | None = Query(
        default=None, max_length=32, description="Restrict the estimate to one line, e.g. U1."
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Envelope[StationPrediction]:
    station = await _require_station(station_id, session)
    prediction = await compute_station_prediction(
        session=session,
        settings=settings,
        station=station,
        hour=hour if hour is not None else utc_now().hour,
        line=line,
    )
    return Envelope(meta=make_meta(fetched_at=None, stale=False, source="db"), data=prediction)
