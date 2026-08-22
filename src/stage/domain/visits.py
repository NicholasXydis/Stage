from dataclasses import dataclass
from datetime import datetime, timedelta

REST_AFTER_FAILURES = 3
REST_BASE_HOURS = 6
REST_MAX_HOURS = 168
_MAX_BACKOFF_STEPS = 8


@dataclass(frozen=True, slots=True)
class CompanyVisit:
    board: str
    succeeded: bool
    error: str = ""
    label: str = ""


@dataclass(frozen=True, slots=True)
class SourceVisit:
    source: str
    board: str
    last_attempt_at: datetime
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: str = ""
    label: str = ""

    @property
    def never_succeeded(self) -> bool:
        return self.last_success_at is None


def rested_until(visit: SourceVisit) -> datetime | None:
    if visit.consecutive_failures < REST_AFTER_FAILURES:
        return None
    steps = min(visit.consecutive_failures - REST_AFTER_FAILURES, _MAX_BACKOFF_STEPS)
    hours = min(REST_BASE_HOURS * 2**steps, REST_MAX_HOURS)
    return visit.last_attempt_at + timedelta(hours=hours)


def is_resting(visit: SourceVisit, now: datetime) -> bool:
    until = rested_until(visit)
    return until is not None and now < until


@dataclass(frozen=True, slots=True)
class DetailFetch:
    id: str
    resolved: bool = False
    failed: bool = False


__all__ = [
    "REST_AFTER_FAILURES",
    "CompanyVisit",
    "DetailFetch",
    "SourceVisit",
    "is_resting",
    "rested_until",
]
