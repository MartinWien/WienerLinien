"""Circuit breaker state machine, driven by a fake clock."""

from __future__ import annotations

from app.ingestion.circuit_breaker import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(clock: FakeClock, threshold: int = 3, cooldown: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock)


def current_state(breaker: CircuitBreaker) -> CircuitState:
    """Read the state through a function call so mypy cannot over-narrow the
    property between mutations."""
    return breaker.state


def test_stays_closed_below_threshold() -> None:
    breaker = make_breaker(FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow_request()


def test_opens_after_threshold_consecutive_failures() -> None:
    breaker = make_breaker(FakeClock())
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allow_request()


def test_success_resets_failure_count() -> None:
    breaker = make_breaker(FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


def test_half_open_after_cooldown_then_closes_on_success() -> None:
    clock = FakeClock()
    breaker = make_breaker(clock)
    for _ in range(3):
        breaker.record_failure()
    assert not breaker.allow_request()

    clock.advance(59.9)
    assert not breaker.allow_request()

    clock.advance(0.2)
    assert breaker.allow_request()
    assert current_state(breaker) is CircuitState.HALF_OPEN

    breaker.record_success()
    assert current_state(breaker) is CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


def test_half_open_failure_reopens_for_another_cooldown() -> None:
    clock = FakeClock()
    breaker = make_breaker(clock)
    for _ in range(3):
        breaker.record_failure()

    clock.advance(61)
    assert breaker.allow_request()  # half-open probe allowed

    breaker.record_failure()
    assert current_state(breaker) is CircuitState.OPEN
    assert not breaker.allow_request()

    clock.advance(61)
    assert breaker.allow_request()
