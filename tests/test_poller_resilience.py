"""Failure-path tests for the ingestion poller.

The upstream is an ``httpx.MockTransport`` whose behaviour can be switched
per test: healthy fixture responses, timeouts, or malformed bodies. No test
here opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import InMemoryCache, departures_key, get_json
from app.core.config import Settings
from app.db import create_engine, create_session_factory, init_schema
from app.ingestion.circuit_breaker import CircuitBreaker, CircuitState
from app.ingestion.poller import Poller
from app.models import DepartureSnapshot
from tests.conftest import FIXTURES_DIR, make_settings, seed_registry

KARLSPLATZ = 60200657


@dataclass
class FakeUpstream:
    """Scriptable upstream: 'ok', 'timeout', or 'garbage'."""

    mode: str = "ok"
    requests_seen: int = 0
    rbls_seen: list[list[str]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests_seen += 1
        self.rbls_seen.append(request.url.params.get_list("rbl"))
        if self.mode == "timeout":
            raise httpx.ReadTimeout("simulated upstream timeout", request=request)
        if self.mode == "garbage":
            return httpx.Response(200, json={"data": {"monitors": "not-a-list"}})
        body = (FIXTURES_DIR / "monitor_ok.json").read_text(encoding="utf-8")
        return httpx.Response(200, json=json.loads(body))


@dataclass
class Harness:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    cache: InMemoryCache
    upstream: FakeUpstream
    poller: Poller


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    settings = make_settings(
        tmp_path,
        cb_failure_threshold=2,
        cb_cooldown_seconds=300.0,
        watched_stations="Karlsplatz",
    )
    engine = create_engine(settings)
    await init_schema(engine)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await seed_registry(session)

    upstream = FakeUpstream()
    cache = InMemoryCache()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream.handler),
        base_url="https://upstream.test/ogd_realtime",
    )
    poller = Poller(
        settings=settings,
        session_factory=session_factory,
        cache=cache,
        http_client=client,
        breaker=CircuitBreaker(
            failure_threshold=settings.cb_failure_threshold,
            cooldown_seconds=settings.cb_cooldown_seconds,
        ),
    )
    yield Harness(
        settings=settings,
        session_factory=session_factory,
        cache=cache,
        upstream=upstream,
        poller=poller,
    )
    await client.aclose()
    await engine.dispose()


async def test_successful_cycle_populates_cache_and_history(harness: Harness) -> None:
    await harness.poller.run_cycle_once()

    payload = await get_json(harness.cache, departures_key(KARLSPLATZ))
    assert payload is not None
    assert payload["station_name"] == "Karlsplatz"
    assert len(payload["departures"]) == 4  # 3 on rbl 4109 + 1 on rbl 4120
    assert payload["departures"][0]["line"] == "U1"

    disruptions = await get_json(harness.cache, "wl:disruptions")
    assert disruptions is not None
    assert disruptions["disruptions"][0]["title"] == "U4: Verzögerungen"

    async with harness.session_factory() as session:
        count = await session.scalar(select(func.count(DepartureSnapshot.id)))
    assert count == 4
    assert harness.poller.breaker.state is CircuitState.CLOSED
    assert harness.poller.status.last_success_at is not None
    assert harness.poller.status.last_error is None


async def test_upstream_timeout_keeps_last_known_good_cache(harness: Harness) -> None:
    await harness.poller.run_cycle_once()
    good_payload = await get_json(harness.cache, departures_key(KARLSPLATZ))
    assert good_payload is not None

    harness.upstream.mode = "timeout"
    await harness.poller.run_cycle_once()

    # The failed cycle must not clobber the cache; readers keep getting the
    # last known good data (flagged stale once it ages past the threshold).
    assert await get_json(harness.cache, departures_key(KARLSPLATZ)) == good_payload
    assert harness.poller.breaker.consecutive_failures == 1
    assert harness.poller.status.last_error == "all upstream requests failed"


async def test_breaker_opens_and_stops_upstream_traffic(harness: Harness) -> None:
    harness.upstream.mode = "timeout"
    await harness.poller.run_cycle_once()
    await harness.poller.run_cycle_once()
    assert harness.poller.breaker.state is CircuitState.OPEN

    requests_before = harness.upstream.requests_seen
    await harness.poller.run_cycle_once()
    assert harness.upstream.requests_seen == requests_before  # cooldown: no calls


async def test_malformed_payload_does_not_publish_or_crash(harness: Harness) -> None:
    await harness.poller.run_cycle_once()
    good_payload = await get_json(harness.cache, departures_key(KARLSPLATZ))

    harness.upstream.mode = "garbage"
    await harness.poller.run_cycle_once()

    assert await get_json(harness.cache, departures_key(KARLSPLATZ)) == good_payload
    async with harness.session_factory() as session:
        count = await session.scalar(select(func.count(DepartureSnapshot.id)))
    assert count == 4  # only the first (good) cycle wrote snapshots


async def test_snapshot_retention_cleanup(harness: Harness) -> None:
    from datetime import timedelta

    from app.models.base import utc_now

    async with harness.session_factory() as session:
        session.add(
            DepartureSnapshot(
                poll_cycle="ancient",
                captured_at=utc_now()
                - timedelta(minutes=harness.settings.snapshot_retention_minutes + 5),
                rbl=4109,
                station_diva=KARLSPLATZ,
                station_name="Karlsplatz",
                line_name="U1",
                towards="Leopoldau",
                direction="H",
                vehicle_type="ptMetro",
                barrier_free=True,
                planned_at=utc_now(),
                realtime_at=None,
                countdown_minutes=5,
                drift_seconds=None,
            )
        )
        await session.commit()

    await harness.poller.run_cycle_once()

    async with harness.session_factory() as session:
        ancient = await session.scalar(
            select(func.count(DepartureSnapshot.id)).where(
                DepartureSnapshot.poll_cycle == "ancient"
            )
        )
    assert ancient == 0


async def test_rbls_are_batched_into_requests(harness: Harness) -> None:
    await harness.poller.run_cycle_once()
    # All three Karlsplatz RBLs fit into one batched monitor request.
    assert harness.upstream.requests_seen == 1
    assert sorted(harness.upstream.rbls_seen[0]) == ["4109", "4120", "4416"]


STEPHANSPLATZ = 60201040
PRATERSTERN = 60200832


async def test_dynamic_station_joins_next_cycle(harness: Harness) -> None:
    harness.poller.request_station(STEPHANSPLATZ, "Stephansplatz")
    await harness.poller.run_cycle_once()

    # Its realtime RBL (1234) was polled alongside the pinned station...
    assert "1234" in harness.upstream.rbls_seen[0]
    # ...and the station was published: covered by a successful chunk, so an
    # empty board is a valid "no departures right now", not a gap.
    payload = await get_json(harness.cache, departures_key(STEPHANSPLATZ))
    assert payload is not None
    assert payload["station_name"] == "Stephansplatz"
    assert payload["departures"] == []
    assert harness.poller.status.dynamic_station_count == 1
    assert "Stephansplatz" in harness.poller.status.watched_station_names


async def test_dynamic_watch_cap_evicts_least_recently_requested(harness: Harness) -> None:
    harness.settings.dynamic_watch_max = 1
    harness.poller.request_station(STEPHANSPLATZ, "Stephansplatz")
    harness.poller.request_station(PRATERSTERN, "Praterstern")

    assert set(harness.poller._dynamic) == {PRATERSTERN}


async def test_dynamic_watch_expires_after_ttl(harness: Harness) -> None:
    harness.poller.request_station(STEPHANSPLATZ, "Stephansplatz")
    watch = harness.poller._dynamic[STEPHANSPLATZ]
    watch.last_requested -= harness.settings.dynamic_watch_ttl_minutes * 60 + 1

    await harness.poller.run_cycle_once()

    assert STEPHANSPLATZ not in harness.poller._dynamic
    assert await get_json(harness.cache, departures_key(STEPHANSPLATZ)) is None
