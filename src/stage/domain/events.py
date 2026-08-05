from dataclasses import dataclass
from datetime import datetime

from stage.domain.enums import SyncOutcome


@dataclass(frozen=True, slots=True)
class SyncStarted:
    sources: tuple[str, ...]
    companies: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class SourceStarted:
    source: str
    companies: int


@dataclass(frozen=True, slots=True)
class CompanyStarted:
    source: str
    company: str


@dataclass(frozen=True, slots=True)
class CompanyFinished:
    source: str
    company: str
    fetched: int
    elapsed_ms: float
    degraded: str = ""


@dataclass(frozen=True, slots=True)
class CompanyFailed:
    source: str
    company: str
    error: str
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class CompanyUnchanged:
    source: str
    company: str
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class UnroutableCompanies:

    companies: tuple[str, ...]
    platforms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceBlocked:

    source: str
    bucket: str
    blocked_until: datetime
    remaining_s: float
    reason: str
    consecutive_failures: int


@dataclass(frozen=True, slots=True)
class SourceRotated:

    source: str
    bucket: str
    selected: int
    deferred: int
    cursor: str
    wrapped: bool


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    source: str
    company: str
    url: str
    has_validator: bool
    expectation: str


@dataclass(frozen=True, slots=True)
class BucketPlan:

    bucket: str
    sources: tuple[str, ...]
    planned: int
    worst_case: int
    ceiling: int

    @property
    def exceeds_ceiling(self) -> bool:
        return self.worst_case > self.ceiling


@dataclass(frozen=True, slots=True)
class RequestLogged:
    source: str
    method: str
    url: str
    status: int | None
    elapsed_ms: float
    attempt: int
    error: str = ""


@dataclass(frozen=True, slots=True)
class SourceFinished:
    source: str
    fetched: int
    added: int
    updated: int
    closed: int
    failed_companies: int
    elapsed_ms: float
    quarantined: int = 0
    requests: int = 0
    not_modified: int = 0
    retries: int = 0
    tightenings: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class SyncFinished:
    outcome: SyncOutcome
    added: int
    updated: int
    closed: int
    failed_sources: tuple[str, ...]
    elapsed_ms: float
    quarantined: int = 0
    purged: int = 0
    requests: int = 0
    not_modified: int = 0
    dry_run: bool = False


SyncEvent = (
    SyncStarted
    | UnroutableCompanies
    | SourceBlocked
    | SourceRotated
    | SourceStarted
    | CompanyStarted
    | CompanyFinished
    | CompanyUnchanged
    | CompanyFailed
    | PlannedRequest
    | BucketPlan
    | RequestLogged
    | SourceFinished
    | SyncFinished
)
