"""Current disruption / traffic info, as last collected by the poller."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from app.api.deps import get_cache, get_settings
from app.core.cache import DISRUPTIONS_KEY, Cache, get_json
from app.core.config import Settings
from app.models.base import utc_now
from app.schemas.common import Envelope, make_meta
from app.schemas.disruption import DisruptionOut

router = APIRouter(tags=["disruptions"])


@router.get(
    "/disruptions",
    response_model=Envelope[list[DisruptionOut]],
    summary="Active disruptions on watched feeds",
    description="Traffic information (`stoerunglang`) collected alongside the departure "
    "poll. Served from cache; an empty list with `meta.stale: true` and "
    "`meta.fetched_at: null` means no poll has succeeded yet.",
)
async def list_disruptions(
    cache: Cache = Depends(get_cache),
    settings: Settings = Depends(get_settings),
) -> Envelope[list[DisruptionOut]]:
    payload = await get_json(cache, DISRUPTIONS_KEY)
    if payload is None:
        return Envelope(
            meta=make_meta(fetched_at=None, stale=True, source="none"),
            data=[],
        )

    fetched_at = datetime.fromisoformat(payload["fetched_at"])
    age = (utc_now() - fetched_at).total_seconds()
    disruptions = [
        DisruptionOut(
            title=raw["title"],
            description=raw.get("description", ""),
            category=raw.get("category"),
            priority=raw.get("priority"),
            related_lines=raw.get("related_lines", []),
            related_stops=raw.get("related_stops", []),
            starts_at=datetime.fromisoformat(raw["starts_at"]) if raw.get("starts_at") else None,
            ends_at=datetime.fromisoformat(raw["ends_at"]) if raw.get("ends_at") else None,
        )
        for raw in payload.get("disruptions", [])
    ]
    return Envelope(
        meta=make_meta(
            fetched_at=fetched_at,
            stale=age > settings.staleness_threshold_seconds,
            source="cache",
        ),
        data=disruptions,
    )
