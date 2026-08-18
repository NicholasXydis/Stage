from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from stage.domain.health import VisitState, classify_visit


class CoverageState(StrEnum):
    PRODUCING = "producing"
    EMPTY = "empty"
    FAILING = "failing"
    STALE = "stale"
    NEVER_REACHED = "never-reached"
    UNROUTABLE = "unroutable"


class CoverageDisposition(StrEnum):
    FEED_ONLY = "feed-only"
    UNAVAILABLE = "unavailable"
    CUSTOM_JSON_CANDIDATE = "custom-json-candidate"
    ADAPTER_CANDIDATE = "adapter-candidate"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class CoverageRow:
    company: str
    platform: str
    board: str
    state: CoverageState
    postings: int
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str

    @property
    def is_gap(self) -> bool:
        return self.state is CoverageState.EMPTY


@dataclass(frozen=True, slots=True)
class UnregisteredCompany:
    company: str
    sources: tuple[str, ...]
    postings: int
    quarantined: int = 0
    posts_internships: bool = False


@dataclass(frozen=True, slots=True)
class CoverageClassification:
    company: str
    disposition: CoverageDisposition
    note: str
    checked_on: datetime
    url: str | None = None


def coverage_state(
    postings: int,
    visited: bool,
    last_success_at: datetime | None,
    consecutive_failures: int,
    now: datetime,
    stale_after_days: int,
) -> CoverageState:
    if postings > 0:
        return CoverageState.PRODUCING
    if not visited:
        return CoverageState.NEVER_REACHED
    visit = classify_visit(last_success_at, consecutive_failures, now, stale_after_days)
    if visit is VisitState.FAILING:
        return CoverageState.FAILING
    if visit is VisitState.STALE:
        return CoverageState.STALE
    return CoverageState.EMPTY


__all__ = [
    "CoverageRow",
    "CoverageClassification",
    "CoverageDisposition",
    "CoverageState",
    "UnregisteredCompany",
    "coverage_state",
]
