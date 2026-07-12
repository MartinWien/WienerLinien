"""Cache abstraction: Redis in production, in-process fallback for local runs.

Both backends implement the same minimal async interface (get/set with TTL),
so the rest of the app never branches on which one is active. Hit/miss/set
counters are tracked per backend and surfaced through ``/health``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

from redis import asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import Settings

logger = logging.getLogger(__name__)

_MEMORY_CACHE_MAX_KEYS = 2048


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    sets: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "sets": self.sets, "errors": self.errors}


class Cache(Protocol):
    backend_name: str
    stats: CacheStats

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


@dataclass
class _Entry:
    value: str
    expires_at: float


class InMemoryCache:
    """Process-local TTL cache used when Redis is unavailable.

    Not shared across processes; documented trade-off for zero-config runs.
    Expired keys are dropped lazily on read and swept when the key count
    exceeds a cap, so memory stays bounded without a background reaper task.
    """

    backend_name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self.stats = CacheStats()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None or entry.expires_at < time.monotonic():
                self._data.pop(key, None)
                self.stats.misses += 1
                return None
            self.stats.hits += 1
            return entry.value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        async with self._lock:
            if len(self._data) >= _MEMORY_CACHE_MAX_KEYS:
                self._evict_locked()
            self._data[key] = _Entry(value=value, expires_at=time.monotonic() + ttl_seconds)
            self.stats.sets += 1

    def _evict_locked(self) -> None:
        now = time.monotonic()
        self._data = {k: v for k, v in self._data.items() if v.expires_at >= now}
        while len(self._data) >= _MEMORY_CACHE_MAX_KEYS:
            # Still full after dropping expired entries: shed oldest-inserted.
            self._data.pop(next(iter(self._data)))

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    backend_name = "redis"

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client
        self.stats = CacheStats()

    async def get(self, key: str) -> str | None:
        try:
            # decode_responses=True guarantees str, but redis-py's stubs
            # cannot express that, hence the cast.
            value = cast(str | None, await self._client.get(key))
        except RedisError:
            # A flaky cache must degrade to a miss, not break the read path.
            logger.warning("redis GET failed", extra={"key": key}, exc_info=True)
            self.stats.errors += 1
            return None
        if value is None:
            self.stats.misses += 1
        else:
            self.stats.hits += 1
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            await self._client.set(key, value, ex=ttl_seconds)
            self.stats.sets += 1
        except RedisError:
            logger.warning("redis SET failed", extra={"key": key}, exc_info=True)
            self.stats.errors += 1

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        await self._client.aclose()


async def build_cache(settings: Settings) -> Cache:
    """Select the cache backend.

    ``auto`` probes Redis once with a short timeout and falls back to the
    in-process cache, so a missing Redis never blocks startup. ``redis``
    fails fast instead of silently degrading a production deployment.
    """
    if settings.cache_backend == "memory":
        logger.info("cache backend: memory (configured)")
        return InMemoryCache()

    client = aioredis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
    )
    redis_cache = RedisCache(client)
    if await redis_cache.ping():
        logger.info("cache backend: redis", extra={"redis_url": settings.redis_url})
        return redis_cache

    await redis_cache.close()
    if settings.cache_backend == "redis":
        raise RuntimeError(
            f"WL_CACHE_BACKEND=redis but Redis at {settings.redis_url} is unreachable"
        )
    logger.warning(
        "Redis unreachable at %s; falling back to in-memory cache (not shared across processes)",
        settings.redis_url,
    )
    return InMemoryCache()


async def get_json(cache: Cache, key: str) -> Any | None:
    raw = await cache.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("corrupt JSON in cache, treating as miss", extra={"key": key})
        return None


async def set_json(cache: Cache, key: str, value: Any, ttl_seconds: int) -> None:
    await cache.set(key, json.dumps(value, ensure_ascii=False), ttl_seconds)


def departures_key(station_id: int) -> str:
    return f"wl:departures:{station_id}"


DISRUPTIONS_KEY = "wl:disruptions"
