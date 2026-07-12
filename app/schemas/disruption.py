"""Schemas for /disruptions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DisruptionOut(BaseModel):
    title: str
    description: str = ""
    category: str | None = Field(
        default=None, description="Upstream category name, e.g. 'stoerunglang'."
    )
    priority: str | None = None
    related_lines: list[str] = Field(default_factory=list)
    related_stops: list[int] = Field(
        default_factory=list, description="RBL/StopIDs affected by the disruption."
    )
    starts_at: datetime | None = None
    ends_at: datetime | None = None
