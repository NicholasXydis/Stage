from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from stage.domain.enums import Platform

CHECK_PREFIX = "check:"
CHECKED_SOURCES = frozenset({"greenhouse", "lever", "smartrecruiters", "workday"})
PAGED_SOURCES = frozenset({"smartrecruiters", "workday"})
BOARD_SOURCES = frozenset(platform.value for platform in Platform)
AWAITING_PAGE = ", waiting for its job page"
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
    "AWAITING_PAGE",
    "BOARD_SOURCES",
    "CHECKED_SOURCES",
    "CHECKS_PER_SYNC",
    "CHECK_PREFIX",
    "RECHECK_AFTER_HOURS",
    "Liveness",
    "LivenessCheck",
    "PAGED_SOURCES",
]
