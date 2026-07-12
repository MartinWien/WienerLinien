"""Background poller: the only component that ever talks to Wiener Linien.

Implemented as a single lifespan-managed asyncio task rather than APScheduler:
there is exactly one job, it needs app-owned resources (DB, cache, HTTP
client), and a plain task gives explicit, inspectable control over
cancellation, error handling and pacing. The fair-use interval is enforced by
the settings clamp plus the sequential sleep loop — a slow cycle delays the
next one instead of overlapping it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import DISRUPTIONS_KEY, Cache, departures_key, set_json
from app.core.config import Settings
from app.core.http import UpstreamError, fetch_json_with_retry
from app.core.logging import cycle_id_var
from app.ingestion.circuit_breaker import CircuitBreaker
from app.ingestion.normalizer import (
    MalformedUpstreamPayload,
    NormalizedDeparture,
    NormalizedDisruption,
    normalize_monitor_payload,
    normalize_traffic_infos,
)
from app.models import DepartureSnapshot, Line, Station, Stop
from app.models.base import utc_now

logger = logging.getLogger(__name__)


@dataclass
class PollerStatus:
    enabled: bool = True
    watched_station_names: list[str] = field(default_factory=list)
    watched_stop_count: int = 0
    dynamic_station_count: int = 0
    cycles_completed: int = 0
    last_cycle_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_latency_ms: int | None = None


@dataclass(frozen=True)
class _WatchedStation:
    diva: int
    name: str


@dataclass
class _DynamicWatch:
    """A station added to the poll set on demand via the API."""

    station: _WatchedStation
    last_requested: float  # time.monotonic()


class Poller:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        cache: Cache,
        http_client: httpx.AsyncClient,
        breaker: CircuitBreaker,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._cache = cache
        self._client = http_client
        self.breaker = breaker
        self.status = PollerStatus(enabled=settings.poller_enabled)
        self._task: asyncio.Task[None] | None = None
        # rbl -> station; rebuilt each cycle from pinned + dynamic stations.
        self._watchlist: dict[int, _WatchedStation] = {}
        # Configured stations, resolved from the registry once it is loaded.
        self._pinned: dict[int, _WatchedStation] = {}
        # Demand-driven stations with an LRU timestamp for TTL/cap eviction.
        self._dynamic: dict[int, _DynamicWatch] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="wl-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run_loop(self) -> None:
        logger.info(
            "poller starting",
            extra={"interval_s": self._settings.poll_interval_seconds},
        )
        # Small head start so registry bootstrap can finish first.
        await asyncio.sleep(2)
        while True:
            try:
                await self.run_cycle_once()
            except asyncio.CancelledError:
                logger.info("poller stopped")
                raise
            except Exception:
                # Internal bugs must not kill the loop; they are recorded but
                # do NOT trip the breaker, which tracks *upstream* health only.
                logger.exception("poll cycle crashed")
                self.status.last_error = "internal error (see logs)"
            # Jitter avoids synchronizing with other consumers of the feed.
            interval = self._settings.poll_interval_seconds
            await asyncio.sleep(interval + random.uniform(0, interval * 0.1))

    # -- one cycle -----------------------------------------------------------

    async def run_cycle_once(self) -> None:
        """Run a single poll cycle. Public so tests can drive cycles directly."""
        cycle_id = uuid.uuid4().hex[:12]
        token = cycle_id_var.set(cycle_id)
        try:
            await self._run_cycle(cycle_id)
        finally:
            cycle_id_var.reset(token)

    def request_station(self, diva: int, name: str) -> None:
        """Register on-demand interest in a station (called from the read API).

        Pinned stations are already covered; dynamic ones get their LRU
        timestamp refreshed. New stations join the poll set from the next
        cycle onward, evicting the least-recently-requested one when the cap
        is reached. Safe to call even when the poller is disabled — it only
        records demand.
        """
        if self._settings.dynamic_watch_max == 0 or diva in self._pinned:
            return
        now = time.monotonic()
        entry = self._dynamic.get(diva)
        if entry is not None:
            entry.last_requested = now
            return
        if len(self._dynamic) >= self._settings.dynamic_watch_max:
            evicted = min(self._dynamic, key=lambda d: self._dynamic[d].last_requested)
            logger.info(
                "dynamic watchlist full; evicting least-recently-requested station",
                extra={"evicted_diva": evicted, "for_diva": diva},
            )
            del self._dynamic[evicted]
        self._dynamic[diva] = _DynamicWatch(
            station=_WatchedStation(diva=diva, name=name), last_requested=now
        )
        logger.info("station added to dynamic watchlist", extra={"diva": diva, "station": name})

    async def _run_cycle(self, cycle_id: str) -> None:
        self.status.last_cycle_at = utc_now()

        async with self._session_factory() as session:
            if not self._pinned:
                await self._resolve_pinned(session)
            self._prune_dynamic()
            await self._rebuild_watchlist(session)
            if not self._watchlist:
                self.status.last_error = "watchlist empty (registry not loaded yet?)"
                logger.warning("no watched stops resolved; skipping cycle")
                return

            if not self.breaker.allow_request():
                logger.warning(
                    "circuit breaker open; skipping upstream poll",
                    extra={"consecutive_failures": self.breaker.consecutive_failures},
                )
                return

            started = time.perf_counter()
            chunk_results = await self._fetch_all_chunks()
            self.status.last_latency_ms = int((time.perf_counter() - started) * 1000)

            succeeded = [result for result in chunk_results if result.payload is not None]
            if not succeeded:
                self.breaker.record_failure()
                self.status.last_error = "all upstream requests failed"
                logger.error(
                    "poll cycle failed: no upstream chunk succeeded",
                    extra={"chunks": len(chunk_results), "breaker": self.breaker.state.value},
                )
                return

            # Any successful chunk proves the upstream is reachable.
            self.breaker.record_success()

            covered_rbls: set[int] = set()
            departures: list[NormalizedDeparture] = []
            disruptions: dict[tuple[str, str | None], NormalizedDisruption] = {}
            for result in succeeded:
                covered_rbls.update(result.rbls)
                try:
                    for monitor in normalize_monitor_payload(result.payload):
                        departures.extend(monitor.departures)
                    for disruption in normalize_traffic_infos(result.payload):
                        disruptions.setdefault((disruption.title, disruption.category), disruption)
                except MalformedUpstreamPayload:
                    logger.exception("discarding malformed upstream chunk")
                    covered_rbls.difference_update(result.rbls)

            fetched_at = utc_now()
            written = await self._publish(
                session,
                cycle_id=cycle_id,
                departures=departures,
                covered_rbls=covered_rbls,
                disruptions=list(disruptions.values()),
                fetched_at=fetched_at,
            )
            deleted = await self._cleanup_snapshots(session)
            await session.commit()

        self.status.cycles_completed += 1
        self.status.last_success_at = fetched_at
        self.status.last_error = None
        failed_chunks = len(chunk_results) - len(succeeded)
        logger.info(
            "poll cycle complete",
            extra={
                "stations_updated": written,
                "departures": len(departures),
                "disruptions": len(disruptions),
                "failed_chunks": failed_chunks,
                "snapshots_deleted": deleted,
                "upstream_ms": self.status.last_latency_ms,
            },
        )

    # -- upstream fetch ------------------------------------------------------

    @dataclass
    class _ChunkResult:
        rbls: list[int]
        payload: Any | None

    async def _fetch_all_chunks(self) -> list[Poller._ChunkResult]:
        rbls = sorted(self._watchlist)
        size = self._settings.monitor_max_rbls_per_request
        chunks = [rbls[i : i + size] for i in range(0, len(rbls), size)]
        semaphore = asyncio.Semaphore(self._settings.monitor_concurrency)

        async def fetch(chunk: list[int]) -> Poller._ChunkResult:
            params = [("rbl", str(rbl)) for rbl in chunk]
            params.append(("activateTrafficInfo", "stoerunglang"))
            async with semaphore:
                try:
                    payload = await fetch_json_with_retry(
                        self._client,
                        "/monitor",
                        params,
                        max_retries=self._settings.http_max_retries,
                        backoff_base_seconds=self._settings.http_backoff_base_seconds,
                    )
                except UpstreamError as exc:
                    logger.warning(
                        "chunk fetch failed",
                        extra={"rbl_count": len(chunk), "error": str(exc)},
                    )
                    return Poller._ChunkResult(rbls=chunk, payload=None)
            return Poller._ChunkResult(rbls=chunk, payload=payload)

        return list(await asyncio.gather(*(fetch(chunk) for chunk in chunks)))

    # -- publish to cache + history ------------------------------------------

    async def _publish(
        self,
        session: AsyncSession,
        *,
        cycle_id: str,
        departures: list[NormalizedDeparture],
        covered_rbls: set[int],
        disruptions: list[NormalizedDisruption],
        fetched_at: datetime,
    ) -> int:
        by_station: dict[int, list[NormalizedDeparture]] = {}
        for departure in departures:
            watched = self._watchlist.get(departure.rbl)
            if watched is None:
                continue
            by_station.setdefault(watched.diva, []).append(departure)

        # A station is publishable only if every one of its stops was covered
        # by a successful chunk — never publish a partial (hence wrong-looking
        # "no departures") view of a station.
        station_rbls: dict[int, set[int]] = {}
        for rbl, watched in self._watchlist.items():
            station_rbls.setdefault(watched.diva, set()).add(rbl)
        stations_by_diva = {w.diva: w for w in self._watchlist.values()}

        ttl = self._settings.cache_ttl_seconds
        snapshots: list[DepartureSnapshot] = []
        published = 0
        for diva, rbls in station_rbls.items():
            if not rbls <= covered_rbls:
                continue
            station = stations_by_diva[diva]
            station_departures = sorted(by_station.get(diva, []), key=lambda d: d.countdown_minutes)
            payload = {
                "station_id": diva,
                "station_name": station.name,
                "fetched_at": fetched_at.isoformat(),
                "departures": [_departure_to_dict(d) for d in station_departures],
            }
            await set_json(self._cache, departures_key(diva), payload, ttl)
            snapshots.extend(
                self._build_snapshots(cycle_id, fetched_at, station, station_departures)
            )
            published += 1

        await set_json(
            self._cache,
            DISRUPTIONS_KEY,
            {
                "fetched_at": fetched_at.isoformat(),
                "disruptions": [_disruption_to_dict(d) for d in disruptions],
            },
            ttl,
        )
        session.add_all(snapshots)
        return published

    def _build_snapshots(
        self,
        cycle_id: str,
        fetched_at: datetime,
        station: _WatchedStation,
        departures: list[NormalizedDeparture],
    ) -> list[DepartureSnapshot]:
        cap = self._settings.snapshot_max_departures_per_line
        per_line_counts: dict[tuple[int, str, str], int] = {}
        snapshots: list[DepartureSnapshot] = []
        for departure in departures:
            key = (departure.rbl, departure.line, departure.direction)
            per_line_counts[key] = per_line_counts.get(key, 0) + 1
            if per_line_counts[key] > cap:
                continue
            snapshots.append(
                DepartureSnapshot(
                    poll_cycle=cycle_id,
                    captured_at=fetched_at,
                    rbl=departure.rbl,
                    station_diva=station.diva,
                    station_name=station.name,
                    line_name=departure.line,
                    towards=departure.towards,
                    direction=departure.direction,
                    vehicle_type=departure.vehicle_type,
                    barrier_free=departure.barrier_free,
                    planned_at=departure.planned_at,
                    realtime_at=departure.realtime_at,
                    countdown_minutes=departure.countdown_minutes,
                    drift_seconds=departure.delay_seconds,
                )
            )
        return snapshots

    async def _cleanup_snapshots(self, session: AsyncSession) -> int:
        cutoff = utc_now() - timedelta(minutes=self._settings.snapshot_retention_minutes)
        result = await session.execute(
            delete(DepartureSnapshot).where(DepartureSnapshot.captured_at < cutoff)
        )
        # DELETE statements always yield a CursorResult with a rowcount.
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    # -- watchlist resolution --------------------------------------------------

    async def _resolve_pinned(self, session: AsyncSession) -> None:
        """Resolve the configured station names against the registry (retried
        each cycle until the registry is loaded)."""
        pinned: dict[int, _WatchedStation] = {}
        for name in self._settings.watched_station_names:
            station = await _find_station_by_name(session, name)
            if station is None:
                logger.warning("watched station not found in registry", extra={"station": name})
                continue
            pinned[station.diva] = _WatchedStation(diva=station.diva, name=station.name)
        if pinned:
            self._pinned = pinned
            logger.info(
                "pinned stations resolved",
                extra={"stations": [w.name for w in pinned.values()]},
            )

    def _prune_dynamic(self) -> None:
        ttl_seconds = self._settings.dynamic_watch_ttl_minutes * 60
        cutoff = time.monotonic() - ttl_seconds
        expired = [diva for diva, watch in self._dynamic.items() if watch.last_requested < cutoff]
        for diva in expired:
            watch = self._dynamic.pop(diva)
            logger.info(
                "dynamic station expired from watchlist",
                extra={"diva": diva, "station": watch.station.name},
            )

    async def _rebuild_watchlist(self, session: AsyncSession) -> None:
        """Map every watched station (pinned + dynamic) to its realtime RBLs.

        Runs every cycle: it is one indexed query, and recomputing beats
        tracking dirty state across request_station/prune/resolve paths.
        """
        stations = dict(self._pinned)
        for watch in self._dynamic.values():
            stations.setdefault(watch.station.diva, watch.station)
        if not stations:
            self._watchlist = {}
            self.status.watched_station_names = []
            self.status.watched_stop_count = 0
            return

        rows = await session.execute(
            select(Stop.rbl, Stop.station_diva)
            .join(Line, Stop.line_id == Line.id)
            .where(Stop.station_diva.in_(stations), Line.realtime.is_(True))
            .distinct()
        )
        watchlist: dict[int, _WatchedStation] = {}
        for rbl, diva in rows:
            watchlist[rbl] = stations[diva]

        self._watchlist = watchlist
        self.status.watched_station_names = sorted({watched.name for watched in watchlist.values()})
        self.status.watched_stop_count = len(watchlist)
        self.status.dynamic_station_count = len(self._dynamic)


async def _find_station_by_name(session: AsyncSession, name: str) -> Station | None:
    """Exact (case-insensitive) match first, then prefix, shortest name wins."""
    exact = await session.execute(
        select(Station).where(func.lower(Station.name) == name.lower()).limit(1)
    )
    if (station := exact.scalar_one_or_none()) is not None:
        return station
    prefix = await session.execute(
        select(Station)
        .where(Station.name.ilike(f"{name}%"))
        .order_by(func.length(Station.name))
        .limit(1)
    )
    return prefix.scalar_one_or_none()


def _departure_to_dict(departure: NormalizedDeparture) -> dict[str, Any]:
    return {
        "rbl": departure.rbl,
        "line": departure.line,
        "towards": departure.towards,
        "direction": departure.direction,
        "platform": departure.platform,
        "vehicle_type": departure.vehicle_type,
        "barrier_free": departure.barrier_free,
        "countdown_minutes": departure.countdown_minutes,
        "planned_at": departure.planned_at.isoformat(),
        "realtime_at": departure.realtime_at.isoformat() if departure.realtime_at else None,
        "delay_seconds": departure.delay_seconds,
    }


def _disruption_to_dict(disruption: NormalizedDisruption) -> dict[str, Any]:
    return {
        "title": disruption.title,
        "description": disruption.description,
        "category": disruption.category,
        "priority": disruption.priority,
        "related_lines": disruption.related_lines,
        "related_stops": disruption.related_stops,
        "starts_at": disruption.starts_at.isoformat() if disruption.starts_at else None,
        "ends_at": disruption.ends_at.isoformat() if disruption.ends_at else None,
    }
