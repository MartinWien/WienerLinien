"""Response envelope shared by every endpoint.

Each response carries a ``meta`` block with the CC BY attribution required by
the data licence, the upstream fetch timestamp, and an explicit ``stale``
flag so clients can distinguish "fresh", "old but served anyway" and "never
fetched" without inspecting HTTP headers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from app.core.config import DATA_SOURCE_ATTRIBUTION
from app.models.base import utc_now

DataT = TypeVar("DataT")

CacheSource = Literal["cache", "db", "registry", "none"]


class Meta(BaseModel):
    data_source: str = Field(
        default=DATA_SOURCE_ATTRIBUTION,
        description="Attribution required by the CC BY 4.0 licence of the underlying data.",
    )
    served_at: datetime = Field(description="When this response was produced.")
    fetched_at: datetime | None = Field(
        default=None,
        description="When the underlying data was last fetched from Wiener Linien. "
        "Null if no upstream fetch has succeeded yet.",
    )
    stale: bool = Field(
        default=False,
        description="True if fetched_at is older than twice the poll interval "
        "(e.g. because the upstream feed is down and cached data is being served).",
    )
    source: CacheSource = Field(
        default="none",
        description="Where the payload came from: 'cache' (Redis/memory hit), "
        "'db' (fallback to last persisted snapshot), 'registry' (static data), 'none'.",
    )


class Envelope(BaseModel, Generic[DataT]):
    meta: Meta
    data: DataT


def make_meta(
    *,
    fetched_at: datetime | None,
    stale: bool,
    source: CacheSource,
) -> Meta:
    return Meta(served_at=utc_now(), fetched_at=fetched_at, stale=stale, source=source)
