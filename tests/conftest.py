"""Shared fixtures. No test in this suite performs network I/O: the upstream
is always an ``httpx.MockTransport`` and the database is a per-test SQLite file."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.models import Line, Station, Stop

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        "cache_backend": "memory",
        "poller_enabled": False,
        "registry_auto_download": False,
        "data_dir": str(tmp_path),
        "log_json": False,
        "rate_limit_per_minute": 10_000,
        "http_max_retries": 0,
        "http_backoff_base_seconds": 0.01,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def seed_registry(session: AsyncSession) -> None:
    """Insert the same small registry the CSV fixtures describe."""
    session.add_all(
        [
            Station(
                diva=60200657,
                name="Karlsplatz",
                municipality="Wien",
                latitude=48.2006380,
                longitude=16.3695772,
            ),
            Station(
                diva=60201040,
                name="Stephansplatz",
                municipality="Wien",
                latitude=48.2086361,
                longitude=16.3720931,
            ),
            Station(
                diva=60200832,
                name="Praterstern",
                municipality="Wien",
                latitude=48.2181391,
                longitude=16.3919579,
            ),
            Line(id=301, name="U1", sorting=1, realtime=True, means_of_transport="ptMetro"),
            Line(id=304, name="U4", sorting=4, realtime=True, means_of_transport="ptMetro"),
            Line(id=950, name="N71", sorting=950, realtime=False, means_of_transport="ptBusNight"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            Stop(rbl=4109, station_diva=60200657, line_id=301, direction=1, name="Karlsplatz"),
            Stop(rbl=4120, station_diva=60200657, line_id=301, direction=2, name="Karlsplatz"),
            Stop(rbl=4416, station_diva=60200657, line_id=304, direction=1, name="Karlsplatz"),
            Stop(rbl=1234, station_diva=60201040, line_id=301, direction=1, name="Stephansplatz"),
            Stop(rbl=5555, station_diva=60200832, line_id=950, direction=1, name="Praterstern"),
        ]
    )
    await session.commit()


@pytest.fixture
async def seeded_app(app: FastAPI) -> FastAPI:
    async with app.state.session_factory() as session:
        await seed_registry(session)
    return app
