"""API contract tests: envelope, pagination, cache/DB fallback, degradation.

The core resilience claim is asserted here: with the upstream completely
absent (poller disabled, nothing fetched), every read endpoint still answers
200 — with empty data and honest ``stale`` metadata.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from fastapi import FastAPI

from app.core.cache import DISRUPTIONS_KEY, departures_key, set_json
from app.models import DepartureSnapshot
from app.models.base import utc_now

KARLSPLATZ = 60200657
ATTRIBUTION_MARKER = "data.wien.gv.at"


async def test_station_search_and_pagination(
    seeded_app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.get("/stations", params={"search": "karl"})
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["data_source"].count(ATTRIBUTION_MARKER) == 1
    assert body["data"]["total"] == 1
    station = body["data"]["items"][0]
    assert station["id"] == KARLSPLATZ
    assert station["stop_count"] == 3

    page1 = (await client.get("/stations", params={"limit": 2, "offset": 0})).json()["data"]
    page2 = (await client.get("/stations", params={"limit": 2, "offset": 2})).json()["data"]
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    names = [s["name"] for s in page1["items"] + page2["items"]]
    assert names == sorted(names)


async def test_unknown_station_is_404(seeded_app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.get("/stations/424242/departures")
    assert response.status_code == 404


async def test_departures_cache_hit(seeded_app: FastAPI, client: httpx.AsyncClient) -> None:
    fetched_at = utc_now()
    await set_json(
        seeded_app.state.cache,
        departures_key(KARLSPLATZ),
        {
            "station_id": KARLSPLATZ,
            "station_name": "Karlsplatz",
            "fetched_at": fetched_at.isoformat(),
            "departures": [
                {
                    "rbl": 4109,
                    "line": "U1",
                    "towards": "Leopoldau",
                    "direction": "H",
                    "platform": "1",
                    "vehicle_type": "ptMetro",
                    "barrier_free": True,
                    "countdown_minutes": 3,
                    "planned_at": fetched_at.isoformat(),
                    "realtime_at": (fetched_at + timedelta(seconds=45)).isoformat(),
                    "delay_seconds": 45,
                }
            ],
        },
        ttl_seconds=60,
    )

    response = await client.get(f"/stations/{KARLSPLATZ}/departures")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["source"] == "cache"
    assert body["meta"]["stale"] is False
    assert body["data"]["watch_status"] == "active"
    assert body["data"]["departures"][0]["delay_seconds"] == 45


async def test_departures_db_fallback_serves_stale_200(
    seeded_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """Cache empty (e.g. TTL passed while upstream is down) -> last persisted
    snapshot is served with stale: true — never a 5xx."""
    captured_at = utc_now() - timedelta(minutes=10)
    async with seeded_app.state.session_factory() as session:
        session.add(
            DepartureSnapshot(
                poll_cycle="cycle-1",
                captured_at=captured_at,
                rbl=4109,
                station_diva=KARLSPLATZ,
                station_name="Karlsplatz",
                line_name="U1",
                towards="Leopoldau",
                direction="H",
                vehicle_type="ptMetro",
                barrier_free=True,
                planned_at=utc_now() + timedelta(minutes=7),
                realtime_at=utc_now() + timedelta(minutes=8),
                countdown_minutes=17,
                drift_seconds=60,
            )
        )
        await session.commit()

    response = await client.get(f"/stations/{KARLSPLATZ}/departures")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["source"] == "db"
    assert body["meta"]["stale"] is True
    departure = body["data"]["departures"][0]
    assert departure["line"] == "U1"
    # Countdown was recomputed against the current clock, not replayed.
    assert departure["countdown_minutes"] <= 8


async def test_departures_with_no_data_at_all_still_200(
    seeded_app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.get(f"/stations/{KARLSPLATZ}/departures")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["departures"] == []
    assert body["data"]["watch_status"] == "pending"
    assert body["meta"]["stale"] is True
    assert body["meta"]["fetched_at"] is None
    assert body["meta"]["source"] == "none"
    # The request registered on-demand interest with the poller.
    assert KARLSPLATZ in seeded_app.state.poller._dynamic


async def test_disruptions_empty_and_cached(seeded_app: FastAPI, client: httpx.AsyncClient) -> None:
    empty = (await client.get("/disruptions")).json()
    assert empty["data"] == []
    assert empty["meta"]["stale"] is True

    await set_json(
        seeded_app.state.cache,
        DISRUPTIONS_KEY,
        {
            "fetched_at": utc_now().isoformat(),
            "disruptions": [
                {
                    "title": "U4: Verzögerungen",
                    "description": "Signalstörung",
                    "category": "stoerunglang",
                    "priority": "1",
                    "related_lines": ["U4"],
                    "related_stops": [4416],
                    "starts_at": utc_now().isoformat(),
                    "ends_at": None,
                }
            ],
        },
        ttl_seconds=60,
    )
    fresh = (await client.get("/disruptions")).json()
    assert fresh["meta"]["stale"] is False
    assert fresh["data"][0]["related_lines"] == ["U4"]


async def test_health_reports_components(seeded_app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"  # poller disabled -> freshness not required
    assert body["database"]["ok"] is True
    assert body["cache"]["backend"] == "memory"
    assert body["upstream"]["circuit_state"] == "closed"
    assert body["poller"]["running"] is False


async def test_openapi_docs_served(client: httpx.AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/stations/{station_id}/departures" in schema["paths"]


async def test_web_board_served_at_root(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert "wl-pulse" in page.text
    assert "data.wien.gv.at" in page.text  # CC BY attribution on the UI too
    for asset in ("/static/styles.css", "/static/app.js"):
        assert (await client.get(asset)).status_code == 200
