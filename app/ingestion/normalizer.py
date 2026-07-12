"""Normalize raw monitor-API payloads into a stable internal shape.

The upstream JSON is deeply nested and occasionally omits fields. The policy
here is: a *structurally* broken payload raises
:class:`MalformedUpstreamPayload` (the whole response is untrustworthy), while
a single broken monitor/departure entry is skipped with a warning so one bad
record cannot poison an otherwise valid cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class MalformedUpstreamPayload(Exception):
    """The monitor response does not have the documented structure."""


@dataclass(frozen=True)
class NormalizedDeparture:
    rbl: int
    line: str
    towards: str
    direction: str  # "H" | "R"
    platform: str | None
    vehicle_type: str | None
    barrier_free: bool
    countdown_minutes: int
    planned_at: datetime
    realtime_at: datetime | None

    @property
    def delay_seconds(self) -> int | None:
        if self.realtime_at is None:
            return None
        return int((self.realtime_at - self.planned_at).total_seconds())


@dataclass(frozen=True)
class NormalizedStopMonitor:
    rbl: int
    station_diva: int | None
    station_title: str
    departures: list[NormalizedDeparture] = field(default_factory=list)


@dataclass(frozen=True)
class NormalizedDisruption:
    title: str
    description: str
    category: str | None
    priority: str | None
    related_lines: list[str]
    related_stops: list[int]
    starts_at: datetime | None
    ends_at: datetime | None


def parse_wl_timestamp(raw: str) -> datetime:
    """Parse WL timestamps like ``2026-07-11T23:12:00.000+0200``.

    Python 3.11+ ``fromisoformat`` accepts this variant directly; a
    ``strptime`` fallback guards against format drift.
    """
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z")


def normalize_monitor_payload(payload: Any) -> list[NormalizedStopMonitor]:
    """Extract per-stop departures from a monitor API response body."""
    monitors = _extract_data_section(payload).get("monitors")
    if not isinstance(monitors, list):
        raise MalformedUpstreamPayload("'data.monitors' missing or not a list")

    results: list[NormalizedStopMonitor] = []
    for index, monitor in enumerate(monitors):
        try:
            results.append(_normalize_monitor(monitor))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.warning(
                "skipping malformed monitor entry",
                extra={"monitor_index": index, "error": repr(exc)},
            )
    return results


def normalize_traffic_infos(payload: Any) -> list[NormalizedDisruption]:
    """Extract disruption info (``trafficInfos``) from a monitor API response.

    The section is absent when there are no active disruptions — that is a
    valid empty result, not an error.
    """
    traffic_infos = _extract_data_section(payload).get("trafficInfos")
    if traffic_infos is None:
        return []
    if not isinstance(traffic_infos, list):
        raise MalformedUpstreamPayload("'data.trafficInfos' is not a list")

    results: list[NormalizedDisruption] = []
    for index, info in enumerate(traffic_infos):
        try:
            results.append(_normalize_traffic_info(info))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.warning(
                "skipping malformed traffic info entry",
                extra={"traffic_info_index": index, "error": repr(exc)},
            )
    return results


def _extract_data_section(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MalformedUpstreamPayload("response body is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MalformedUpstreamPayload("'data' section missing or not an object")
    return data


def _normalize_monitor(monitor: dict[str, Any]) -> NormalizedStopMonitor:
    properties = monitor["locationStop"]["properties"]
    rbl = int(properties["attributes"]["rbl"])
    title = str(properties.get("title", ""))
    diva_raw = properties.get("name")
    try:
        station_diva: int | None = int(diva_raw)
    except (TypeError, ValueError):
        station_diva = None

    departures: list[NormalizedDeparture] = []
    for line in monitor.get("lines") or []:
        departures.extend(_normalize_line(rbl, line))
    departures.sort(key=lambda d: d.countdown_minutes)
    return NormalizedStopMonitor(
        rbl=rbl, station_diva=station_diva, station_title=title, departures=departures
    )


def _normalize_line(rbl: int, line: dict[str, Any]) -> list[NormalizedDeparture]:
    line_name = str(line["name"]).strip()
    towards = str(line.get("towards", "")).strip()
    direction = str(line.get("direction", "")) or "?"
    platform = line.get("platform")
    vehicle_type = line.get("type")
    barrier_free = bool(line.get("barrierFree", False))

    raw_departures = (line.get("departures") or {}).get("departure") or []
    departures: list[NormalizedDeparture] = []
    for raw in raw_departures:
        try:
            departure_time = raw["departureTime"]
            planned_raw = departure_time.get("timePlanned")
            real_raw = departure_time.get("timeReal")
            if planned_raw is None and real_raw is None:
                continue  # nothing usable on this entry
            planned = parse_wl_timestamp(planned_raw or real_raw)
            realtime = parse_wl_timestamp(real_raw) if real_raw else None
            countdown = int(departure_time.get("countdown", 0))
            # Departure-level vehicle info overrides line-level when present
            # (relevant for mixed rolling stock and short-turn services).
            vehicle = raw.get("vehicle") or {}
            resolved_line = str(vehicle.get("name", line_name)).strip()
            if not resolved_line:
                # Observed in the live feed: monitors with an empty line name
                # (e.g. auxiliary entries). Without a line identity the
                # departure is unusable for display or stats.
                continue
            departures.append(
                NormalizedDeparture(
                    rbl=rbl,
                    line=resolved_line,
                    towards=str(vehicle.get("towards", towards)).strip(),
                    direction=str(vehicle.get("direction", direction)),
                    platform=str(platform) if platform is not None else None,
                    vehicle_type=str(vehicle.get("type", vehicle_type or "")) or None,
                    barrier_free=bool(vehicle.get("barrierFree", barrier_free)),
                    countdown_minutes=countdown,
                    planned_at=planned,
                    realtime_at=realtime,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "skipping malformed departure entry",
                extra={"rbl": rbl, "line": line_name, "error": repr(exc)},
            )
    return departures


def _normalize_traffic_info(info: dict[str, Any]) -> NormalizedDisruption:
    time_section = info.get("time") or {}
    starts_raw = time_section.get("start")
    ends_raw = time_section.get("end")
    return NormalizedDisruption(
        title=str(info["title"]),
        description=str(info.get("description", "")),
        category=info.get("name"),
        priority=info.get("priority"),
        related_lines=[str(line) for line in info.get("relatedLines") or []],
        related_stops=[int(stop) for stop in info.get("relatedStops") or []],
        starts_at=parse_wl_timestamp(starts_raw) if starts_raw else None,
        ends_at=parse_wl_timestamp(ends_raw) if ends_raw else None,
    )
