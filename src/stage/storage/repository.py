from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from stage.domain import (
    CompanyVisit,
    DetailFetch,
    HttpValidator,
    IntegrityFinding,
    Job,
    JobFilters,
    PurgeResult,
    QuarantinedJob,
    QuarantineFilters,
    RateState,
    SourceVisit,
    SyncRun,
    VolumePoint,
    WorkdayFacet,
)


@dataclass(frozen=True, slots=True)
class SourceBatch:
    source: str
    run_started_at: datetime
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    closable_boards: tuple[str, ...] = field(default_factory=tuple)
    unchanged_boards: tuple[str, ...] = field(default_factory=tuple)
    validators: tuple[HttpValidator, ...] = field(default_factory=tuple)
    rate_state: tuple[RateState, ...] = field(default_factory=tuple)
    workday_facets: tuple[WorkdayFacet, ...] = field(default_factory=tuple)
    forgotten_facets: tuple[WorkdayFacet, ...] = field(default_factory=tuple)
    detail_fetches: tuple[DetailFetch, ...] = field(default_factory=tuple)
    visits: tuple[CompanyVisit, ...] = field(default_factory=tuple)
    quarantined: tuple[QuarantinedJob, ...] = field(default_factory=tuple)
    resolve_duplicates: "Callable[[Sequence[Job], Sequence[Job]], Sequence[object]] | None" = None

    closes_whole_source: bool = False


@dataclass(frozen=True, slots=True)
class SourceBatchResult:
    source: str
    fetched: int
    added: int
    updated: int
    closed: int
    touched: int = 0
    quarantined: int = 0
    duplicates: int = 0
    stored: int = 0


class Repository(Protocol):
    def apply_source_batch(self, batch: SourceBatch) -> SourceBatchResult: ...

    def load_validators(self, source: str) -> Mapping[str, HttpValidator]: ...

    def load_rate_state(self) -> Mapping[str, RateState]: ...

    def clear_rate_state(self, bucket: str | None = None) -> int: ...

    def stale_members(self, source: str, before: datetime) -> list[SourceVisit]: ...

    def detail_queue(self, source: str, limit: int) -> list[str]: ...

    def detail_queue_size(self, source: str) -> int: ...

    def load_workday_facets(self) -> Mapping[tuple[str, str], WorkdayFacet]: ...


    def list_quarantined(self, filters: QuarantineFilters) -> list[QuarantinedJob]: ...

    def count_duplicates(self) -> int: ...

    def purge(self, now: datetime) -> PurgeResult: ...

    def tombstone_count(self) -> int: ...

    def count_quarantined(self, filters: QuarantineFilters) -> int: ...

    def quarantine_reason_counts(self) -> dict[str, int]: ...

    def list_jobs(self, filters: JobFilters) -> list[Job]: ...

    def get_job(self, job_id: str) -> Job | None: ...

    def count_jobs(self, filters: JobFilters) -> int: ...

    def record_sync_run(self, run: SyncRun) -> None: ...

    def last_sync_at(self) -> datetime | None: ...

    def cached_url_count(self) -> int: ...

    def volume_history(self, limit: int) -> Mapping[str, list[VolumePoint]]: ...

    def run_history(self, limit: int) -> list[SyncRun]: ...

    def all_visits(self) -> list[SourceVisit]: ...

    def integrity_findings(self) -> list[IntegrityFinding]: ...

    def composition(self, column: str) -> dict[str, int]: ...

    def stored_counts(self) -> dict[str, int]: ...

    def schema_version(self) -> int: ...

    def close(self) -> None: ...


__all__ = ["Repository", "SourceBatch", "SourceBatchResult"]
