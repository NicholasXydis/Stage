from dataclasses import dataclass, field
from datetime import datetime

from stage.domain.enums import SyncOutcome
from stage.domain.health import UNRECORDED_VOLUME


@dataclass(frozen=True, slots=True)
class SourceRunStats:
    source: str
    fetched: int = 0
    added: int = 0
    updated: int = 0
    closed: int = 0
    quarantined: int = 0
    errors: int = 0
    requests: int = 0
    not_modified: int = 0
    retries: int = 0
    tightenings: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    elapsed_ms: float = 0.0
    deferred: int = 0
    stored: int = UNRECORDED_VOLUME

    blocked: bool = False


@dataclass(frozen=True, slots=True)
class SyncRun:
    started_at: datetime
    finished_at: datetime
    outcome: SyncOutcome
    sources: tuple[SourceRunStats, ...] = field(default_factory=tuple)
