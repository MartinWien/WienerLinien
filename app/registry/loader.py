"""Parse the four registry CSVs and persist stations, lines and stops.

Idempotent by construction: stations and lines are upserted by primary key,
and stops (a pure derivation of the other files) are replaced wholesale in
the same transaction. Re-running after a registry update converges on the new
state; a crash mid-load rolls back to the previous one.

CSV quirks handled here (verified against the live files):

* delimiter is ``;``, encoding UTF-8 with a possible BOM;
* the *stations* file calls its name column ``PlatformText`` while the *stop
  points* file uses ``StopText``;
* ``fahrwegverlaeufe.csv`` repeats a StopID once per line/pattern/sequence —
  the distinct (StopID, LineID, Direction) triples are what become ``stops``;
* stop points can reference DIVAs or appear in route courses without being in
  the current stations file; those rows are counted and skipped, not fatal.

Runnable standalone: ``python -m app.registry.loader``.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Line, Station, Stop
from app.registry.download import CSV_FILENAMES, missing_csvs

logger = logging.getLogger(__name__)

_STATIONS_CSV, _STOP_POINTS_CSV, _LINES_CSV, _ROUTES_CSV = CSV_FILENAMES


class RegistryDataError(Exception):
    """A registry CSV is missing or structurally unusable."""


@dataclass(frozen=True)
class _StopPoint:
    rbl: int
    diva: int
    name: str
    latitude: float | None
    longitude: float | None


@dataclass
class RegistryLoadResult:
    stations: int = 0
    lines: int = 0
    stops: int = 0
    skipped_stop_rows: int = 0


async def registry_is_empty(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count(Station.diva)))
    return not count


async def load_registry(session: AsyncSession, data_dir: Path) -> RegistryLoadResult:
    if missing := missing_csvs(data_dir):
        raise RegistryDataError(
            f"missing registry CSV(s) in {data_dir}: {', '.join(missing)} — "
            "run 'python -m app.registry.download' or see README"
        )

    result = RegistryLoadResult()

    stations = _parse_stations(data_dir / _STATIONS_CSV)
    for station in stations.values():
        await session.merge(station)
    result.stations = len(stations)

    lines = _parse_lines(data_dir / _LINES_CSV)
    for line in lines.values():
        await session.merge(line)
    result.lines = len(lines)

    stop_points = _parse_stop_points(data_dir / _STOP_POINTS_CSV)
    triples = _parse_route_triples(data_dir / _ROUTES_CSV)

    await session.execute(delete(Stop))
    for rbl, line_id, direction in sorted(triples):
        point = stop_points.get(rbl)
        if point is None or point.diva not in stations or line_id not in lines:
            result.skipped_stop_rows += 1
            continue
        session.add(
            Stop(
                rbl=rbl,
                station_diva=point.diva,
                line_id=line_id,
                direction=direction,
                name=point.name,
                latitude=point.latitude,
                longitude=point.longitude,
            )
        )
        result.stops += 1

    await session.commit()
    logger.info(
        "registry loaded",
        extra={
            "stations": result.stations,
            "lines": result.lines,
            "stops": result.stops,
            "skipped_stop_rows": result.skipped_stop_rows,
        },
    )
    return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    # utf-8-sig strips the BOM some exports carry.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        if not reader.fieldnames:
            raise RegistryDataError(f"{path.name}: empty file")
        return list(reader)


def _require(row: dict[str, str], column: str, path_name: str) -> str:
    value = row.get(column)
    if value is None:
        raise RegistryDataError(f"{path_name}: expected column '{column}' not found")
    return value


def _parse_stations(path: Path) -> dict[int, Station]:
    stations: dict[int, Station] = {}
    for row in _read_csv(path):
        raw_diva = _require(row, "DIVA", path.name)
        name = _require(row, "PlatformText", path.name)
        if not raw_diva.strip() or not name.strip():
            continue
        diva = int(raw_diva)
        stations[diva] = Station(
            diva=diva,
            name=name.strip(),
            municipality=(row.get("Municipality") or "").strip(),
            latitude=_parse_float(row.get("Latitude")),
            longitude=_parse_float(row.get("Longitude")),
        )
    if not stations:
        raise RegistryDataError(f"{path.name}: no usable station rows")
    return stations


def _parse_lines(path: Path) -> dict[int, Line]:
    lines: dict[int, Line] = {}
    for row in _read_csv(path):
        raw_id = _require(row, "LineID", path.name)
        name = _require(row, "LineText", path.name)
        if not raw_id.strip() or not name.strip():
            continue
        line_id = int(raw_id)
        lines[line_id] = Line(
            id=line_id,
            name=name.strip(),
            sorting=int(row.get("SortingHelp") or 0),
            realtime=(row.get("Realtime") or "").strip() == "1",
            means_of_transport=(row.get("MeansOfTransport") or "").strip(),
        )
    if not lines:
        raise RegistryDataError(f"{path.name}: no usable line rows")
    return lines


def _parse_stop_points(path: Path) -> dict[int, _StopPoint]:
    points: dict[int, _StopPoint] = {}
    for row in _read_csv(path):
        raw_rbl = _require(row, "StopID", path.name)
        raw_diva = row.get("DIVA") or ""
        if not raw_rbl.strip() or not raw_diva.strip():
            continue  # stop points without a DIVA cannot be tied to a station
        points[int(raw_rbl)] = _StopPoint(
            rbl=int(raw_rbl),
            diva=int(raw_diva),
            name=(row.get("StopText") or "").strip(),
            latitude=_parse_float(row.get("Latitude")),
            longitude=_parse_float(row.get("Longitude")),
        )
    if not points:
        raise RegistryDataError(f"{path.name}: no usable stop point rows")
    return points


def _parse_route_triples(path: Path) -> set[tuple[int, int, int]]:
    triples: set[tuple[int, int, int]] = set()
    for row in _read_csv(path):
        raw_stop = _require(row, "StopID", path.name)
        raw_line = _require(row, "LineID", path.name)
        raw_direction = row.get("Direction") or ""
        if not (raw_stop.strip() and raw_line.strip() and raw_direction.strip()):
            continue
        triples.add((int(raw_stop), int(raw_line), int(raw_direction)))
    if not triples:
        raise RegistryDataError(f"{path.name}: no usable route rows")
    return triples


def _parse_float(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings()

    async def run() -> None:
        from app.db import create_engine, create_session_factory, init_schema

        engine = create_engine(settings)
        await init_schema(engine)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await load_registry(session, Path(settings.data_dir))
        await engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
