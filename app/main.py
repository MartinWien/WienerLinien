"""Application factory and lifespan wiring.

Startup order matters: logging first, then DB schema, then registry bootstrap
(download + load if the tables are empty), then cache/HTTP client, and the
poller last — it depends on all of the above. Shutdown reverses it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import disruptions, health, stations
from app.core.cache import build_cache
from app.core.config import Settings
from app.core.http import create_http_client
from app.core.logging import configure_logging, request_id_var
from app.core.rate_limit import RateLimitMiddleware
from app.db import create_engine, create_session_factory, init_schema
from app.ingestion.circuit_breaker import CircuitBreaker
from app.ingestion.poller import Poller
from app.registry.download import download_registry_csvs, missing_csvs
from app.registry.loader import RegistryDataError, load_registry, registry_is_empty

logger = logging.getLogger(__name__)

_DESCRIPTION = """
Real-time departures, disruptions and delay statistics for Vienna public
transport, backed by the Wiener Linien open-data feeds.

All endpoints serve pre-fetched data collected by a background poller —
no request ever waits on the upstream API. Responses include a `meta` block
with data attribution, fetch time, and an explicit `stale` flag.

**Datenquelle: Stadt Wien - data.wien.gv.at (Wiener Linien), CC BY 4.0**
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings if settings is not None else Settings()
    configure_logging(app_settings.log_level, app_settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(app_settings)
        await init_schema(engine)
        session_factory = create_session_factory(engine)
        await _bootstrap_registry(app_settings, session_factory)

        cache = await build_cache(app_settings)
        http_client = create_http_client(app_settings)
        breaker = CircuitBreaker(
            failure_threshold=app_settings.cb_failure_threshold,
            cooldown_seconds=app_settings.cb_cooldown_seconds,
        )
        poller = Poller(
            settings=app_settings,
            session_factory=session_factory,
            cache=cache,
            http_client=http_client,
            breaker=breaker,
        )

        app.state.settings = app_settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.cache = cache
        app.state.poller = poller

        if app_settings.poller_enabled:
            poller.start()
        else:
            logger.info("poller disabled (WL_POLLER_ENABLED=false); serving reads only")

        try:
            yield
        finally:
            await poller.stop()
            await http_client.aclose()
            await cache.close()
            await engine.dispose()

    app = FastAPI(
        title="wl-pulse",
        version="0.1.0",
        description=_DESCRIPTION,
        lifespan=lifespan,
        contact={"name": "wl-pulse", "url": "https://github.com/martiitov"},
        license_info={"name": "MIT"},
    )

    # Order: add_middleware stacks outward, so the rate limiter is added first
    # (innermost) and the request-ID middleware last (outermost) — every
    # response, including 429s, carries an X-Request-ID.
    app.add_middleware(RateLimitMiddleware, requests_per_minute=app_settings.rate_limit_per_minute)
    app.middleware("http")(_request_context_middleware)

    app.add_exception_handler(Exception, _unhandled_exception_handler)

    app.include_router(health.router)
    app.include_router(stations.router)
    app.include_router(disruptions.router)

    # Built-in departure board: a static single-page UI consuming this API.
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


async def _bootstrap_registry(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        if not await registry_is_empty(session):
            return
        data_dir = Path(settings.data_dir)
        if missing_csvs(data_dir) and settings.registry_auto_download:
            try:
                await download_registry_csvs(data_dir)
            except Exception:
                logger.exception("registry auto-download failed; will serve without registry")
        try:
            await load_registry(session, data_dir)
        except RegistryDataError as exc:
            logger.warning(
                "registry not loaded: %s — station endpoints will be empty until "
                "the CSVs are provided and the app restarts",
                exc,
            )


async def _request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error", "request_id": request_id_var.get()},
    )


app = create_app()
