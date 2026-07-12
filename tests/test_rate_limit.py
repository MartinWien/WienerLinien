"""Rate limiting: budget enforcement, Retry-After, health exemption."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import create_app
from tests.conftest import make_settings


@pytest.fixture
async def limited_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(tmp_path, rate_limit_per_minute=3)
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def test_requests_over_budget_get_429(limited_client: httpx.AsyncClient) -> None:
    for _ in range(3):
        assert (await limited_client.get("/stations")).status_code == 200

    blocked = await limited_client.get("/stations")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "Rate limit exceeded" in blocked.json()["detail"]


async def test_health_is_exempt(limited_client: httpx.AsyncClient) -> None:
    for _ in range(3):
        await limited_client.get("/stations")
    assert (await limited_client.get("/stations")).status_code == 429
    # Orchestrator probes must never be throttled.
    assert (await limited_client.get("/health")).status_code == 200
