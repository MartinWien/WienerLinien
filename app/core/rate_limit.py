"""Per-client sliding-window rate limiting middleware.

In-process on purpose: this service deploys as a single instance (the poller
must be a singleton anyway), so a Redis-backed limiter would add a network
hop to every request for no benefit. The README documents the switch to a
shared store if the API tier is ever scaled horizontally.
"""

from __future__ import annotations

import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

_WINDOW_SECONDS = 60.0
_EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})
# When the client table grows past this, windows with no recent activity are
# dropped so an IP-rotating scanner cannot grow memory unboundedly.
_CLIENT_TABLE_SWEEP_THRESHOLD = 10_000


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, requests_per_minute: int) -> None:
        super().__init__(app)
        self._limit = requests_per_minute
        self._windows: dict[str, deque[float]] = {}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in _EXEMPT_PATHS or path == "/" or path.startswith("/static/"):
            return await call_next(request)

        client_key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = self._windows.setdefault(client_key, deque())
        while window and window[0] <= now - _WINDOW_SECONDS:
            window.popleft()

        if len(window) >= self._limit:
            retry_after = max(1, int(window[0] + _WINDOW_SECONDS - now) + 1)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Rate limit exceeded: {self._limit} requests per minute. "
                        f"Retry after {retry_after} seconds."
                    )
                },
                headers={"Retry-After": str(retry_after)},
            )

        window.append(now)
        if len(self._windows) > _CLIENT_TABLE_SWEEP_THRESHOLD:
            self._sweep(now)
        return await call_next(request)

    def _sweep(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        self._windows = {
            key: window for key, window in self._windows.items() if window and window[-1] > cutoff
        }
