from dataclasses import dataclass, replace
from datetime import datetime, timedelta

BREAKER_THRESHOLD = 5
BASE_BLOCK_S = 300.0
BLOCK_CAP_S = 24 * 3600.0
DECAY_FACTOR = 0.7
CLEAR_RATIO = 1.05


def block_duration(consecutive_failures: int) -> float:
    steps = max(0, consecutive_failures - BREAKER_THRESHOLD)
    return min(BLOCK_CAP_S, BASE_BLOCK_S * float(2 ** min(steps, 16)))


def decay(override: float, baseline: float) -> float | None:
    if override <= baseline * CLEAR_RATIO:
        return None
    relaxed = max(baseline, override * DECAY_FACTOR)
    return None if relaxed <= baseline * CLEAR_RATIO else relaxed


@dataclass(frozen=True, slots=True)
class RateState:
    bucket: str
    updated_at: datetime
    blocked_until: datetime | None = None
    min_interval_override: float | None = None
    consecutive_failures: int = 0
    last_failure_at: datetime | None = None
    reason: str = ""
    rotation_cursor: str = ""

    def __post_init__(self) -> None:
        if self.blocked_until is not None:
            ceiling = self.updated_at + timedelta(seconds=BLOCK_CAP_S)
            if self.blocked_until > ceiling:
                object.__setattr__(self, "blocked_until", ceiling)

    def is_blocked(self, now: datetime) -> bool:
        return self.blocked_until is not None and now < self.blocked_until

    def blocks_remaining_s(self, now: datetime) -> float:
        if self.blocked_until is None:
            return 0.0
        return max(0.0, (self.blocked_until - now).total_seconds())

    def cleared(self, now: datetime) -> "RateState":
        return RateState(bucket=self.bucket, updated_at=now, rotation_cursor=self.rotation_cursor)

    def with_cursor(self, cursor: str, now: datetime) -> "RateState":
        return replace(self, rotation_cursor=cursor, updated_at=now)


def blocked(
    state: RateState, *, now: datetime, consecutive_failures: int, reason: str
) -> RateState:
    return replace(
        state,
        updated_at=now,
        blocked_until=now + timedelta(seconds=block_duration(consecutive_failures)),
        consecutive_failures=consecutive_failures,
        last_failure_at=now,
        reason=reason,
    )


__all__ = [
    "BASE_BLOCK_S",
    "BLOCK_CAP_S",
    "BREAKER_THRESHOLD",
    "CLEAR_RATIO",
    "DECAY_FACTOR",
    "RateState",
    "block_duration",
    "blocked",
    "decay",
]
