"""Registry loader: CSV parsing, idempotency, quirk handling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import create_engine, create_session_factory, init_schema
from app.models import Line, Station, Stop
from app.registry.loader import RegistryDataError, load_registry
from tests.conftest import FIXTURES_DIR, make_settings

CSV_DIR = FIXTURES_DIR / "csv"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    settings = make_settings(tmp_path)
    engine = create_engine(settings)
    await init_schema(engine)
    yield engine
    await engine.dispose()


async def test_load_from_fixture_csvs(engine: AsyncEngine) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        result = await load_registry(session, CSV_DIR)

    assert result.stations == 3
    assert result.lines == 4
    # 6 valid distinct (rbl, line, direction) triples; the ghost-DIVA stop and
    # the route row pointing at a nonexistent StopID are skipped.
    assert result.stops == 6
    assert result.skipped_stop_rows == 2

    async with session_factory() as session:
        karlsplatz = await session.get(Station, 60200657)
        assert karlsplatz is not None
        assert karlsplatz.name == "Karlsplatz"

        u1 = await session.get(Line, 301)
        assert u1 is not None and u1.realtime is True
        n71 = await session.get(Line, 950)
        assert n71 is not None and n71.realtime is False

        # One station name -> several RBLs: the documented WL quirk.
        rbls = (
            (await session.execute(select(Stop.rbl).where(Stop.station_diva == 60200657)))
            .scalars()
            .all()
        )
        assert sorted(rbls) == [4109, 4120, 4416]


async def test_reload_is_idempotent(engine: AsyncEngine) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        first = await load_registry(session, CSV_DIR)
    async with session_factory() as session:
        second = await load_registry(session, CSV_DIR)

    assert (first.stations, first.lines, first.stops) == (
        second.stations,
        second.lines,
        second.stops,
    )
    async with session_factory() as session:
        stop_count = await session.scalar(select(func.count(Stop.id)))
        assert stop_count == first.stops


async def test_missing_csvs_raise_with_guidance(engine: AsyncEngine, tmp_path: Path) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        with pytest.raises(RegistryDataError, match=r"app\.registry\.download"):
            await load_registry(session, tmp_path / "empty")
