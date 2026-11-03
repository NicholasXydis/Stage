from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

CHECK_PREFIX = "check:"
CHECKED_SOURCES = frozenset({"greenhouse", "lever", "smartrecruiters", "workday"})
RECHECK_AFTER_HOURS = 6
CHECKS_PER_SYNC = 30


class Liveness(StrEnum):
    OPEN = "open"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LivenessCheck:
    id: str
    verdict: Liveness
    checked_at: datetime


__all__ = [
    "CHECKED_SOURCES",
    "CHECKS_PER_SYNC",
    "CHECK_PREFIX",
    "RECHECK_AFTER_HOURS",
    "Liveness",
    "LivenessCheck",
]
