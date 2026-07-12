"""Liveness/readiness endpoint.

Returns 200 with ``status: "degraded"`` while the upstream feed is down —
degraded is a state this service is designed to serve traffic in. Only a
genuinely broken dependency of our own (the database) yields 503.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.core.cache import Cache
from app.ingestion.poller import Poller
from app.models.base import utc_now
from app.schemas.health import CacheHealth, DbHealth, HealthOut, PollerHealth, UpstreamHealth

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut, summary="Liveness and readiness")
async def health(request: Request, response: Response) -> HealthOut:
    db_health = await _check_db(request)
    cache: Cache = request.app.state.cache
    poller: Poller = request.app.state.poller

    poller_health = PollerHealth(
        enabled=poller.status.enabled,
        running=poller.running,
        watched_stations=poller.status.watched_station_names,
        watched_stops=poller.status.watched_stop_count,
        dynamic_stations=poller.status.dynamic_station_count,
        cycles_completed=poller.status.cycles_completed,
        last_cycle_at=poller.status.last_cycle_at,
        last_success_at=poller.status.last_success_at,
        last_error=poller.status.last_error,
    )
    upstream_health = UpstreamHealth(
        circuit_state=poller.breaker.state.value,
        consecutive_failures=poller.breaker.consecutive_failures,
        last_latency_ms=poller.status.last_latency_ms,
    )

    settings = request.app.state.settings
    poll_is_fresh = (
        poller.status.last_success_at is not None
        and (utc_now() - poller.status.last_success_at).total_seconds()
        <= settings.staleness_threshold_seconds
    )
    degraded = not db_health.ok or (poller.status.enabled and not poll_is_fresh)

    if not db_health.ok:
        response.status_code = 503

    return HealthOut(
        status="degraded" if degraded else "ok",
        database=db_health,
        cache=CacheHealth(
            backend=cache.backend_name,
            ok=await cache.ping(),
            **cache.stats.as_dict(),
        ),
        poller=poller_health,
        upstream=upstream_health,
    )


async def _check_db(request: Request) -> DbHealth:
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return DbHealth(ok=True)
    except Exception as exc:  # health must report failures, not raise them
        return DbHealth(ok=False, error=repr(exc))
