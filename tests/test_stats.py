"""Delay statistics and the prediction baseline."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI

from app.models import DepartureSnapshot
from app.models.base import utc_now

KARLSPLATZ = 60200657


def snapshot(
    *,
    cycle: str,
    captured_at: datetime,
    planned_at: datetime,
    line: str = "U1",
    drift: int | None = 60,
    rbl: int = 4109,
) -> DepartureSnapshot:
    return DepartureSnapshot(
        poll_cycle=cycle,
        captured_at=captured_at,
        rbl=rbl,
        station_diva=KARLSPLATZ,
        station_name="Karlsplatz",
        line_name=line,
        towards="Leopoldau",
        direction="H",
        vehicle_type="ptMetro",
        barrier_free=True,
        planned_at=planned_at,
        realtime_at=planned_at + timedelta(seconds=drift) if drift is not None else None,
        countdown_minutes=5,
        drift_seconds=drift,
    )


async def test_stats_deduplicate_observations_per_departure(
    seeded_app: FastAPI, client: httpx.AsyncClient
) -> None:
    now = utc_now()
    planned = now + timedelta(minutes=5)
    async with seeded_app.state.session_factory() as session:
        # The same physical departure observed in two consecutive cycles: the
        # early observation says 300 s late, the later (authoritative) one 30 s.
        session.add_all(
            [
                snapshot(
                    cycle="c1",
                    captured_at=now - timedelta(minutes=2),
                    planned_at=planned,
                    drift=300,
                ),
                snapshot(
                    cycle="c2", captured_at=now - timedelta(minutes=1), planned_at=planned, drift=30
                ),
                # A second, distinct departure on another line, 200 s late.
                snapshot(
                    cycle="c2",
                    captured_at=now - timedelta(minutes=1),
                    planned_at=planned + timedelta(minutes=3),
                    line="U4",
                    drift=200,
                    rbl=4416,
                ),
                # Planned-only observation: counted, but excluded from delay math.
                snapshot(
                    cycle="c2",
                    captured_at=now - timedelta(minutes=1),
                    planned_at=planned + timedelta(minutes=6),
                    line="U4",
                    drift=None,
                    rbl=4416,
                ),
            ]
        )
        await session.commit()

    response = await client.get(f"/stations/{KARLSPLATZ}/stats", params={"window": 30})
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["departures_observed"] == 3  # deduplicated
    assert data["with_realtime"] == 2
    assert data["avg_delay_seconds"] == 115.0  # (30 + 200) / 2 — NOT the stale 300
    assert data["max_delay_seconds"] == 200
    assert data["on_time_percent"] == 50.0  # 30 s on time; 200 s over the 120 s threshold

    per_line = {entry["line"]: entry for entry in data["per_line"]}
    assert per_line["U1"]["avg_delay_seconds"] == 30.0
    assert per_line["U1"]["on_time_percent"] == 100.0
    assert per_line["U4"]["departures_observed"] == 2
    assert per_line["U4"]["with_realtime"] == 1


async def test_stats_window_excludes_old_observations(
    seeded_app: FastAPI, client: httpx.AsyncClient
) -> None:
    now = utc_now()
    async with seeded_app.state.session_factory() as session:
        session.add(
            snapshot(
                cycle="old",
                captured_at=now - timedelta(minutes=90),
                planned_at=now - timedelta(minutes=85),
                drift=500,
            )
        )
        await session.commit()

    data = (await client.get(f"/stations/{KARLSPLATZ}/stats", params={"window": 30})).json()["data"]
    assert data["departures_observed"] == 0
    assert data["avg_delay_seconds"] is None
    assert data["on_time_percent"] is None


async def test_predict_baseline_needs_enough_samples(
    seeded_app: FastAPI, client: httpx.AsyncClient
) -> None:
    now = utc_now()
    async with seeded_app.state.session_factory() as session:
        # Six distinct U1 departures this hour, all 120 s late -> usable sample.
        for i in range(6):
            session.add(
                snapshot(
                    cycle=f"c{i}",
                    captured_at=now - timedelta(minutes=i + 1),
                    planned_at=now.replace(minute=0, second=0, microsecond=0)
                    + timedelta(minutes=i),
                    drift=120,
                )
            )
        # Only two U4 samples -> below the minimum, must yield null.
        for i in range(2):
            session.add(
                snapshot(
                    cycle=f"u4-{i}",
                    captured_at=now - timedelta(minutes=i + 1),
                    planned_at=now.replace(minute=0, second=0, microsecond=0)
                    + timedelta(minutes=30 + i),
                    line="U4",
                    drift=60,
                    rbl=4416,
                )
            )
        await session.commit()

    hour = now.replace(minute=0, second=0, microsecond=0).hour
    response = await client.get(f"/stations/{KARLSPLATZ}/predict", params={"hour": hour})
    assert response.status_code == 200
    predictions = {p["line"]: p for p in response.json()["data"]["predictions"]}

    assert predictions["U1"]["expected_delay_seconds"] == 120.0
    assert predictions["U1"]["sample_size"] == 6
    assert predictions["U4"]["expected_delay_seconds"] is None
    assert predictions["U4"]["sample_size"] == 2

    filtered = (
        await client.get(f"/stations/{KARLSPLATZ}/predict", params={"hour": hour, "line": "U1"})
    ).json()["data"]
    assert [p["line"] for p in filtered["predictions"]] == ["U1"]
