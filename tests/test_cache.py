"""In-memory cache behaviour: TTL, hit/miss accounting, corrupt values."""

from __future__ import annotations

import pytest

from app.core import cache as cache_module
from app.core.cache import InMemoryCache, get_json, set_json


async def test_miss_then_hit() -> None:
    cache = InMemoryCache()
    assert await cache.get("k") is None
    await cache.set("k", "v", ttl_seconds=60)
    assert await cache.get("k") == "v"
    assert cache.stats.misses == 1
    assert cache.stats.hits == 1
    assert cache.stats.sets == 1


async def test_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    current = {"t": 100.0}
    monkeypatch.setattr("app.core.cache.time.monotonic", lambda: current["t"])

    cache = InMemoryCache()
    await cache.set("k", "v", ttl_seconds=30)
    current["t"] = 129.0
    assert await cache.get("k") == "v"
    current["t"] = 131.0
    assert await cache.get("k") is None
    assert cache.stats.misses == 1


async def test_json_roundtrip() -> None:
    cache = InMemoryCache()
    await set_json(cache, "k", {"a": [1, 2], "b": "ü"}, ttl_seconds=60)
    assert await get_json(cache, "k") == {"a": [1, 2], "b": "ü"}


async def test_corrupt_json_is_treated_as_miss() -> None:
    cache = InMemoryCache()
    await cache.set("k", "{not json", ttl_seconds=60)
    assert await get_json(cache, "k") is None


async def test_eviction_keeps_size_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_module, "_MEMORY_CACHE_MAX_KEYS", 10)
    cache = InMemoryCache()
    for i in range(25):
        await cache.set(f"k{i}", "v", ttl_seconds=600)
    assert len(cache._data) <= 10
    # The most recently written key must survive eviction.
    assert await cache.get("k24") == "v"
