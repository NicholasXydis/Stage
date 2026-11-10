import re
from dataclasses import dataclass
from datetime import datetime, timedelta

REST_AFTER_FAILURES = 3
REST_BASE_HOURS = 6
REST_MAX_HOURS = 168
_MAX_BACKOFF_STEPS = 8
_REFUSAL = re.compile(r"\breturned (?:401|403|429)\b")
_GONE = re.compile(r"\breturned (?:404|410)\b")


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

    @property
    def refusing(self) -> bool:
        return self.consecutive_failures >= REST_AFTER_FAILURES and refused(self.last_error)

    @property
    def gone(self) -> bool:
        return self.consecutive_failures >= REST_AFTER_FAILURES and gone(self.last_error)


def refused(error: str) -> bool:
    return bool(_REFUSAL.search(error))


def gone(error: str) -> bool:
    return bool(_GONE.search(error))


def turned_away(error: str) -> bool:
    return refused(error) or gone(error)


def rested_until(visit: SourceVisit) -> datetime | None:
    if visit.consecutive_failures < REST_AFTER_FAILURES:
        return None
    steps = min(visit.consecutive_failures - REST_AFTER_FAILURES, _MAX_BACKOFF_STEPS)
    hours = min(REST_BASE_HOURS * 2**steps, REST_MAX_HOURS)
    return visit.last_attempt_at + timedelta(hours=hours)


def is_resting(visit: SourceVisit, now: datetime) -> bool:
    until = rested_until(visit)
    return until is not None and visit.last_attempt_at <= now < until


@dataclass(frozen=True, slots=True)
class DetailFetch:
    id: str
    resolved: bool = False
    failed: bool = False


FULL_READ_PREFIX = "full:"
SEARCH_PREFIX = "search:"
FIRST_READ_PREFIX = "first:"
HEIR_PREFIX = "heir:"
RULES_PREFIX = "rules:"
HELD_CLOSE_PREFIX = "held:"
MASS_CLOSE_SHARE = 0.3
MASS_CLOSE_MIN = 5

__all__ = [
    "HELD_CLOSE_PREFIX",
    "MASS_CLOSE_MIN",
    "MASS_CLOSE_SHARE",
    "FIRST_READ_PREFIX",
    "FULL_READ_PREFIX",
    "HEIR_PREFIX",
    "RULES_PREFIX",
    "REST_AFTER_FAILURES",
    "SEARCH_PREFIX",
    "CompanyVisit",
    "DetailFetch",
    "SourceVisit",
    "is_resting",
    "gone",
    "refused",
    "turned_away",
    "rested_until",
]
