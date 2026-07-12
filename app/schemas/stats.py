"""Schemas for /stations/{id}/stats and /stations/{id}/predict."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LineStats(BaseModel):
    line: str
    departures_observed: int = Field(
        description="Unique departures (deduplicated across poll cycles) in the window."
    )
    with_realtime: int = Field(description="Of those, how many carried a real-time estimate.")
    avg_delay_seconds: float | None = Field(
        default=None, description="Mean drift (realtime - planned) over realtime departures."
    )
    max_delay_seconds: int | None = None
    on_time_percent: float | None = Field(
        default=None,
        description="Share of realtime departures between 60 s early and the configured "
        "late threshold (default 120 s).",
    )


class StationStats(BaseModel):
    station_id: int
    station_name: str
    window_minutes: int
    departures_observed: int
    with_realtime: int
    avg_delay_seconds: float | None = None
    max_delay_seconds: int | None = None
    on_time_percent: float | None = None
    on_time_threshold_seconds: int
    per_line: list[LineStats]


class LinePrediction(BaseModel):
    line: str
    hour: int = Field(ge=0, le=23, description="Hour of day (UTC) the estimate applies to.")
    expected_delay_seconds: float | None = Field(
        default=None, description="Null when there are not enough samples for this line/hour."
    )
    sample_size: int
    method: str = Field(
        default="rolling_mean",
        description="Statistical baseline: mean observed drift for this line in this hour "
        "over the retained history. Not a trained ML model.",
    )


class StationPrediction(BaseModel):
    station_id: int
    station_name: str
    hour: int
    predictions: list[LinePrediction]
