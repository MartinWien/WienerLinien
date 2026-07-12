"""Shared upstream HTTP client with timeouts and retry/backoff.

Every call to Wiener Linien goes through :func:`fetch_json_with_retry`:
per-attempt timeouts, bounded retries on *transient* failures only (network
errors, 5xx, 429), and exponential backoff with full jitter so a fleet of
instances does not retry in lockstep.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

USER_AGENT = "wl-pulse/0.1 (+https://github.com/martiitov/wl-pulse; polite polling per WL fair use)"

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class UpstreamError(Exception):
    """Raised when the upstream call failed after all retries."""


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.monitor_base_url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(connect=5.0, read=settings.http_timeout_seconds, write=5.0, pool=5.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


async def fetch_json_with_retry(
    client: httpx.AsyncClient,
    path: str,
    params: list[tuple[str, str]],
    *,
    max_retries: int,
    backoff_base_seconds: float,
) -> Any:
    """GET ``path`` and return the decoded JSON body.

    Raises :class:`UpstreamError` once retries are exhausted. 4xx responses
    other than 429 are treated as permanent (a bad RBL will not become valid
    by retrying) and fail immediately.
    """
    # tuple() sidesteps list invariance against httpx's PrimitiveData union.
    query = httpx.QueryParams(tuple(params))
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            # Full jitter: anywhere between 0 and base * 2^(attempt-1).
            delay = random.uniform(0, backoff_base_seconds * 2 ** (attempt - 1))
            logger.info(
                "retrying upstream request",
                extra={"attempt": attempt, "delay_s": round(delay, 2), "path": path},
            )
            await asyncio.sleep(delay)
        started = time.perf_counter()
        try:
            response = await client.get(path, params=query)
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                "upstream transport error",
                extra={
                    "attempt": attempt,
                    "path": path,
                    "error": repr(exc),
                    "latency_ms": _elapsed_ms(started),
                },
            )
            continue

        latency_ms = _elapsed_ms(started)
        if response.status_code in _RETRYABLE_STATUS:
            last_error = UpstreamError(f"upstream returned {response.status_code}")
            logger.warning(
                "upstream transient error status",
                extra={
                    "attempt": attempt,
                    "status": response.status_code,
                    "path": path,
                    "latency_ms": latency_ms,
                },
            )
            continue
        if response.status_code >= 400:
            raise UpstreamError(
                f"upstream returned non-retryable status {response.status_code} for {path}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            # A non-JSON 200 (e.g. an HTML maintenance page) is transient.
            last_error = exc
            logger.warning(
                "upstream returned non-JSON body",
                extra={"attempt": attempt, "path": path, "latency_ms": latency_ms},
            )
            continue
        logger.debug(
            "upstream request ok",
            extra={"path": path, "latency_ms": latency_ms, "attempt": attempt},
        )
        return body

    raise UpstreamError(f"upstream request failed after {max_retries + 1} attempts") from last_error


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
