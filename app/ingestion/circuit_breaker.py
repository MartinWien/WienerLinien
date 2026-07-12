"""Minimal circuit breaker for the upstream monitor feed.

States:

* ``closed``    — normal operation; consecutive failures are counted.
* ``open``      — after ``failure_threshold`` consecutive failures, all
  requests are refused for ``cooldown_seconds`` so a struggling upstream is
  not hammered while it recovers.
* ``half_open`` — after the cooldown, exactly one probe is allowed through;
  success closes the breaker, failure re-opens it for another cooldown.

The poller is a single asyncio task, so no locking is needed; the clock is
injectable for deterministic tests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        self._maybe_transition_to_half_open()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow_request(self) -> bool:
        """True if a request may be attempted right now."""
        self._maybe_transition_to_half_open()
        return self._state is not CircuitState.OPEN

    def record_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            logger.info("circuit breaker closed", extra={"previous_state": self._state.value})
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN or (
            self._state is CircuitState.CLOSED
            and self._consecutive_failures >= self._failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            logger.warning(
                "circuit breaker opened",
                extra={
                    "consecutive_failures": self._consecutive_failures,
                    "cooldown_seconds": self._cooldown_seconds,
                },
            )

    def _maybe_transition_to_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self._cooldown_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info("circuit breaker half-open, allowing probe request")
