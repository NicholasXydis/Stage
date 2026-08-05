import time
from dataclasses import dataclass, field
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 5
    cooldown_s: float = 30.0
    consecutive_failures: int = 0
    opened_at: float | None = None
    trips: int = 0
    _probing: bool = field(default=False, repr=False)

    def state(self, now: float | None = None) -> BreakerState:
        if self.opened_at is None:
            return BreakerState.CLOSED
        moment = now if now is not None else time.monotonic()
        if moment - self.opened_at >= self.cooldown_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allows(self, now: float | None = None) -> bool:
        current = self.state(now)
        if current is BreakerState.CLOSED:
            return True
        if current is BreakerState.HALF_OPEN and not self._probing:
            self._probing = True
            return True
        return False

    def is_open(self, now: float | None = None) -> bool:
        return self.state(now) is BreakerState.OPEN

    def release_probe(self) -> None:
        self._probing = False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self._probing = False

    def record_failure(self, now: float | None = None) -> None:
        self.consecutive_failures += 1
        self._probing = False
        if self.consecutive_failures >= self.failure_threshold and self.opened_at is None:
            self.opened_at = now if now is not None else time.monotonic()
            self.trips += 1
        elif self.opened_at is not None:
            self.opened_at = now if now is not None else time.monotonic()
