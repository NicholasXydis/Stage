from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from stage.domain import (
    CompanyVisit,
    CoverageClassification,
    DetailFetch,
    HttpValidator,
    IntegrityFinding,
    IntegrityRepair,
    Job,
    JobFilters,
    PurgeResult,
    QuarantinedJob,
    QuarantineFilters,
    RateState,
    SourceVisit,
    SyncRun,
    VolumePoint,
    WorkdayCrawl,
    WorkdayCrawlStep,
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
    workday_crawls: tuple[WorkdayCrawlStep, ...] = field(default_factory=tuple)
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
    def apply_source_batch(self, batch: SourceBatch) -> SourceBatchResult:
        pass

    def load_validators(self, source: str) -> Mapping[str, HttpValidator]:
        pass

    def load_rate_state(self) -> Mapping[str, RateState]:
        pass

    def clear_rate_state(self, bucket: str | None = None) -> int:
        pass

    def stale_members(self, source: str, before: datetime) -> list[SourceVisit]:
        pass

    def detail_queue(self, source: str, limit: int) -> list[str]:
        pass

    def detail_queue_size(self, source: str) -> int:
        pass

    def load_workday_facets(self) -> Mapping[tuple[str, str], WorkdayFacet]:
        pass

    def load_workday_crawls(self) -> Mapping[str, WorkdayCrawl]:
        pass

    def list_quarantined(self, filters: QuarantineFilters) -> list[QuarantinedJob]:
        pass

    def count_duplicates(self) -> int:
        pass

    def purge(self, now: datetime) -> PurgeResult:
        pass

    def close_orphan_boards(self, sources: Sequence[str], boards: Sequence[str]) -> int:
        pass

    def preview_purge(self, now: datetime) -> PurgeResult:
        pass

    def tombstone_count(self) -> int:
        pass

    def count_quarantined(self, filters: QuarantineFilters) -> int:
        pass

    def relabel_quarantine(self, entries: Sequence[QuarantinedJob]) -> int: ...

    def refresh_quarantine_locations(
        self, resolve: Callable[[str], tuple[str, str | None]]
    ) -> int: ...

    def quarantine_reason_counts(self) -> dict[str, int]:
        pass

    def list_jobs(self, filters: JobFilters) -> list[Job]:
        pass

    def get_job(self, job_id: str) -> Job | None:
        pass

    def duplicates_of(self, job_id: str) -> list[Job]:
        pass

    def search_jobs(self, query: str, filters: JobFilters) -> list[Job]:
        pass

    def count_search(self, query: str, filters: JobFilters) -> int:
        pass

    def count_jobs(self, filters: JobFilters) -> int:
        pass

    def board_counts(self) -> dict[str, int]:
        pass

    def company_counts(self) -> dict[str, dict[str, int]]:
        pass

    def quarantine_company_counts(self) -> dict[str, dict[str, int]]:
        pass

    def quarantine_company_reasons(self) -> dict[str, dict[str, int]]:
        pass

    def company_apply_urls(self, companies: Sequence[str]) -> dict[str, tuple[str, ...]]:
        pass

    def coverage_classifications(self) -> list[CoverageClassification]:
        pass

    def record_coverage_classification(self, entry: CoverageClassification) -> bool:
        pass

    def clear_coverage_classification(self, company: str) -> bool:
        pass

    def record_sync_run(self, run: SyncRun) -> None:
        pass

    def last_sync_at(self) -> datetime | None:
        pass

    def clear_validators(self, source: str | None = None) -> int:
        pass

    def cached_url_count(self) -> int:
        pass

    def volume_history(self, limit: int) -> Mapping[str, list[VolumePoint]]:
        pass

    def run_history(self, limit: int) -> list[SyncRun]:
        pass

    def all_visits(self) -> list[SourceVisit]:
        pass

    def repair_integrity(self) -> list[IntegrityRepair]: ...

    def integrity_findings(self) -> list[IntegrityFinding]:
        pass

    def composition(self, column: str) -> dict[str, int]:
        pass

    def stored_counts(self) -> dict[str, int]:
        pass

    def schema_version(self) -> int:
        pass

    def close(self) -> None:
        pass


__all__ = ["Repository", "SourceBatch", "SourceBatchResult"]
