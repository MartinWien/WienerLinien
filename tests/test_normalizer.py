"""Normalizer tests against saved upstream fixtures (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.ingestion.normalizer import (
    MalformedUpstreamPayload,
    normalize_monitor_payload,
    normalize_traffic_infos,
    parse_wl_timestamp,
)
from tests.conftest import load_fixture


class TestParseTimestamp:
    def test_parses_wl_offset_format(self) -> None:
        parsed = parse_wl_timestamp("2026-07-11T23:12:00.000+0200")
        assert parsed == datetime(2026, 7, 11, 23, 12, tzinfo=timezone(timedelta(hours=2)))
        assert parsed.astimezone(UTC).hour == 21

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_wl_timestamp("not-a-timestamp")


class TestNormalizeMonitors:
    def test_ok_fixture(self) -> None:
        monitors = normalize_monitor_payload(load_fixture("monitor_ok.json"))

        assert [m.rbl for m in monitors] == [4109, 4120]
        first = monitors[0]
        assert first.station_diva == 60200657
        assert first.station_title == "Karlsplatz"
        assert len(first.departures) == 3

        departure = first.departures[0]
        assert departure.line == "U1"
        assert departure.towards == "Leopoldau"
        assert departure.direction == "H"
        assert departure.vehicle_type == "ptMetro"
        assert departure.barrier_free is True
        assert departure.countdown_minutes == 1
        assert departure.planned_at.tzinfo is not None
        assert departure.delay_seconds == 90  # 23:12:00 planned vs 23:13:30 real

    def test_departures_sorted_by_countdown(self) -> None:
        monitors = normalize_monitor_payload(load_fixture("monitor_ok.json"))
        countdowns = [d.countdown_minutes for d in monitors[0].departures]
        assert countdowns == sorted(countdowns)

    def test_planned_only_departure_has_no_delay(self) -> None:
        monitors = normalize_monitor_payload(load_fixture("monitor_ok.json"))
        planned_only = monitors[0].departures[-1]
        assert planned_only.realtime_at is None
        assert planned_only.delay_seconds is None

    def test_broken_entries_are_skipped_not_fatal(self) -> None:
        monitors = normalize_monitor_payload(load_fixture("monitor_partially_broken.json"))
        # Two of the three monitor entries are unusable; one survives with the
        # one intact departure (the second departure has a broken timestamp).
        assert len(monitors) == 1
        assert monitors[0].rbl == 4109
        assert len(monitors[0].departures) == 1

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "just a string",
            {},
            {"data": None},
            {"data": {"monitors": "not-a-list"}},
        ],
    )
    def test_structurally_malformed_raises(self, payload: object) -> None:
        with pytest.raises(MalformedUpstreamPayload):
            normalize_monitor_payload(payload)


class TestNormalizeTrafficInfos:
    def test_ok_fixture(self) -> None:
        disruptions = normalize_traffic_infos(load_fixture("monitor_ok.json"))
        assert len(disruptions) == 1
        disruption = disruptions[0]
        assert disruption.title == "U4: Verzögerungen"
        assert disruption.category == "stoerunglang"
        assert disruption.related_lines == ["U4"]
        assert disruption.related_stops == [4416, 4421]
        assert disruption.starts_at is not None
        assert disruption.ends_at is not None

    def test_missing_section_means_no_disruptions(self) -> None:
        assert normalize_traffic_infos({"data": {"monitors": []}}) == []

    def test_entry_without_title_is_skipped(self) -> None:
        disruptions = normalize_traffic_infos(load_fixture("monitor_partially_broken.json"))
        assert disruptions == []
