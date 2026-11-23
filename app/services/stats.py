"""Delay statistics over the bounded snapshot history.

The same physical departure is observed once per poll cycle, so naive
averaging would weight departures by how long they sat on the board. The
window query therefore deduplicates to *unique departures* — identified by
(rbl, line, direction, planned time) — keeping only the observation closest
to the actual departure (max ``captured_at``), which is the most accurate
drift measurement available for it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import DepartureSnapshot, Station
from app.models.base import utc_now
from app.schemas.stats import LinePrediction, LineStats, StationPrediction, StationStats

_EARLY_TOLERANCE_SECONDS = 60
_MIN_PREDICTION_SAMPLES = 5


class _Observation(NamedTuple):
    line: str
    drift_seconds: int | None
    planned_at: datetime


def _latest_observation_query(
    station_diva: int, since: datetime
) -> Select[str, int | None, datetime]:
    """Select one row per unique departure: its last observation in the window."""
    snapshot = DepartureSnapshot
    last_seen = (
        select(
            snapshot.rbl.label("rbl"),
            snapshot.line_name.label("line_name"),
            snapshot.direction.label("direction"),
            snapshot.planned_at.label("planned_at"),
            func.max(snapshot.captured_at).label("last_captured_at"),
        )
        .where(snapshot.station_diva == station_diva, snapshot.captured_at >= since)
        .group_by(snapshot.rbl, snapshot.line_name, snapshot.direction, snapshot.planned_at)
        .subquery()
    )
    return (
        select(snapshot.line_name, snapshot.drift_seconds, snapshot.planned_at)
        .join(
            last_seen,
            and_(
                snapshot.rbl == last_seen.c.rbl,
                snapshot.line_name == last_seen.c.line_name,
                snapshot.direction == last_seen.c.direction,
                snapshot.planned_at == last_seen.c.planned_at,
                snapshot.captured_at == last_seen.c.last_captured_at,
            ),
        )
        .distinct()
    )


async def compute_station_stats(
    *,
    session: AsyncSession,
    settings: Settings,
    station: Station,
    window_minutes: int,
) -> StationStats:
    since = utc_now() - timedelta(minutes=window_minutes)
    rows = await session.execute(_latest_observation_query(station.diva, since))
    observations = [_Observation(line, drift, planned) for line, drift, planned in rows]

    threshold = settings.stats_on_time_threshold_seconds
    by_line: dict[str, list[_Observation]] = {}
    for observation in observations:
        by_line.setdefault(observation.line, []).append(observation)

    per_line = [
        _line_stats(line, line_observations, threshold)
        for line, line_observations in sorted(by_line.items())
    ]
    totals = _aggregate([o.drift_seconds for o in observations], threshold)
    return StationStats(
        station_id=station.diva,
        station_name=station.name,
        window_minutes=window_minutes,
        departures_observed=len(observations),
        with_realtime=totals.with_realtime,
        avg_delay_seconds=totals.avg_delay,
        max_delay_seconds=totals.max_delay,
        on_time_percent=totals.on_time_percent,
        on_time_threshold_seconds=threshold,
        per_line=per_line,
    )


async def compute_station_prediction(
    *,
    session: AsyncSession,
    settings: Settings,
    station: Station,
    hour: int,
    line: str | None,
) -> StationPrediction:
    """Rolling statistical baseline over the retained history.

    Groups observed drift by (line, hour of planned departure, UTC) and
    returns the mean. Honest about its limits: with the default 3-hour
    retention it only ever covers the recent past, and lines/hours with fewer
    than 5 samples return ``null``.
    """
    since = utc_now() - timedelta(minutes=settings.snapshot_retention_minutes)
    rows = await session.execute(_latest_observation_query(station.diva, since))

    samples: dict[str, list[int]] = {}
    for row_line, drift, planned_at in rows:
        if drift is None or planned_at.hour != hour:
            continue
        if line is not None and row_line != line:
            continue
        samples.setdefault(row_line, []).append(drift)

    if line is not None and line not in samples:
        samples[line] = []

    predictions = [
        LinePrediction(
            line=sample_line,
            hour=hour,
            expected_delay_seconds=(
                round(sum(drifts) / len(drifts), 1)
                if len(drifts) >= _MIN_PREDICTION_SAMPLES
                else None
            ),
            sample_size=len(drifts),
        )
        for sample_line, drifts in sorted(samples.items())
    ]
    return StationPrediction(
        station_id=station.diva, station_name=station.name, hour=hour, predictions=predictions
    )


class _Totals(NamedTuple):
    with_realtime: int
    avg_delay: float | None
    max_delay: int | None
    on_time_percent: float | None


def _aggregate(drifts: list[int | None], threshold: int) -> _Totals:
    realtime_drifts = [d for d in drifts if d is not None]
    if not realtime_drifts:
        return _Totals(with_realtime=0, avg_delay=None, max_delay=None, on_time_percent=None)
    on_time = sum(1 for d in realtime_drifts if -_EARLY_TOLERANCE_SECONDS <= d <= threshold)
    return _Totals(
        with_realtime=len(realtime_drifts),
        avg_delay=round(sum(realtime_drifts) / len(realtime_drifts), 1),
        max_delay=max(realtime_drifts),
        on_time_percent=round(100.0 * on_time / len(realtime_drifts), 1),
    )


def _line_stats(line: str, observations: list[_Observation], threshold: int) -> LineStats:
    totals = _aggregate([o.drift_seconds for o in observations], threshold)
    return LineStats(
        line=line,
        departures_observed=len(observations),
        with_realtime=totals.with_realtime,
        avg_delay_seconds=totals.avg_delay,
        max_delay_seconds=totals.max_delay,
        on_time_percent=totals.on_time_percent,
    )
