from collections.abc import Callable, Collection, Mapping, Sequence
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
    JobStatus,
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
from stage.domain.liveness import LivenessCheck


class Link(Protocol):
    @property
    def duplicate_id(self) -> str:
        pass

    @property
    def canonical_id(self) -> str:
        pass

    @property
    def kind(self) -> str:
        pass

    @property
    def evidence(self) -> str:
        pass


Resolver = Callable[[Sequence[Job], Sequence[Job]], Sequence[Link]]


@dataclass(frozen=True, slots=True)
class SourceBatch:
    source: str
    run_started_at: datetime
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    closable_boards: tuple[str, ...] = field(default_factory=tuple)
    unchanged_boards: tuple[str, ...] = field(default_factory=tuple)
    board_urls: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)
    validators: tuple[HttpValidator, ...] = field(default_factory=tuple)
    rate_state: tuple[RateState, ...] = field(default_factory=tuple)
    workday_facets: tuple[WorkdayFacet, ...] = field(default_factory=tuple)
    forgotten_facets: tuple[WorkdayFacet, ...] = field(default_factory=tuple)
    workday_crawls: tuple[WorkdayCrawlStep, ...] = field(default_factory=tuple)
    detail_fetches: tuple[DetailFetch, ...] = field(default_factory=tuple)
    visits: tuple[CompanyVisit, ...] = field(default_factory=tuple)
    quarantined: tuple[QuarantinedJob, ...] = field(default_factory=tuple)
    resolve_duplicates: Resolver | None = None

    closes_whole_source: bool = False
    list_sources: frozenset[str] = frozenset()


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
    successors: tuple[tuple[str, str], ...] = ()


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

    def stored_details(self, ids: Collection[str]) -> dict[str, tuple[str, str]]:
        pass

    def load_workday_facets(self) -> Mapping[tuple[str, str], WorkdayFacet]:
        pass

    def load_workday_crawls(self) -> Mapping[str, WorkdayCrawl]:
        pass

    def list_quarantined(self, filters: QuarantineFilters) -> list[QuarantinedJob]:
        pass

    def count_duplicates(self) -> int:
        pass

    def screened_with(self) -> int:
        pass

    def mark_screened(self, stamp: int, at: datetime | None = None) -> None:
        pass

    def rules_changed_at(self) -> datetime | None:
        pass

    def purge(self, now: datetime) -> PurgeResult:
        pass

    def pending_heirs(self) -> list[tuple[str, str]]:
        pass

    def forget_heirs(self, formers: Sequence[str]) -> None:
        pass

    def close_orphan_boards(self, sources: Sequence[str], boards: Sequence[str]) -> int:
        pass

    def preview_purge(self, now: datetime) -> PurgeResult:
        pass

    def tombstone_count(self) -> int:
        pass

    def count_quarantined(self, filters: QuarantineFilters) -> int:
        pass

    def relabel_quarantine(self, entries: Sequence[QuarantinedJob]) -> int:
        pass

    def refresh_quarantine_locations(
        self, resolve: Callable[[str, str, str], tuple[str, str | None]]
    ) -> int:
        pass

    def quarantine_reason_counts(self, filters: QuarantineFilters | None = None) -> dict[str, int]:
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

    def company_names(self) -> list[str]:
        pass

    def ids_with_prefix(self, prefix: str) -> list[str]:
        pass

    def open_counts_by_company(self, keys: Collection[str]) -> dict[str, int]:
        pass

    def canonical_ids(self, job_ids: Collection[str]) -> dict[str, str]:
        pass

    def posting_states(self, job_ids: Collection[str]) -> dict[str, JobStatus]:
        pass

    def previous_sync_at(self) -> datetime | None:
        pass

    def requests_since(
        self, since: datetime, until: datetime | None = None
    ) -> tuple[dict[str, int], bool]:
        pass

    def pushback_since(self, since: datetime, until: datetime | None = None) -> frozenset[str]:
        pass

    def board_activity(self, month: datetime, week: datetime) -> dict[str, tuple[int, int]]:
        pass

    def earliest_copies(self, ids: Sequence[str]) -> dict[str, datetime]:
        pass

    def first_reads(self) -> dict[str, datetime]:
        pass

    def full_reads(self, source: str, prefix: str) -> dict[str, tuple[datetime, bool, bool]]:
        pass

    def feed_tips(
        self, sources: Collection[str], since: datetime, limit: int
    ) -> dict[str, datetime]:
        pass

    def closed_among(self, job_ids: Sequence[str]) -> int:
        pass

    def board_counts(self) -> dict[str, int]:
        pass

    def company_counts(self) -> dict[str, dict[str, int]]:
        pass

    def quarantine_company_counts(self) -> dict[str, dict[str, int]]:
        pass

    def quarantine_company_reasons(
        self, reasons: Collection[str] = ()
    ) -> dict[str, dict[str, int]]:
        pass

    def company_apply_urls(
        self, companies: Sequence[str], *, kept_only: bool = False
    ) -> dict[str, tuple[str, ...]]:
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

    def volume_history(
        self, limit: int, since: datetime | None = None
    ) -> Mapping[str, list[VolumePoint]]:
        pass

    def run_history(self, limit: int) -> list[SyncRun]:
        pass

    def all_visits(self) -> list[SourceVisit]:
        pass

    def relink_duplicates(self, resolve: Resolver, canonical: Callable[[str], str]) -> int:
        pass

    def repair_integrity(self) -> list[IntegrityRepair]:
        pass

    def integrity_findings(self, *, whole_file: bool = False) -> list[IntegrityFinding]:
        pass

    def composition(self, column: str) -> dict[str, int]:
        pass

    def stored_counts(self) -> dict[str, int]:
        pass

    def schema_version(self) -> int:
        pass

    def close(self) -> None:
        pass

    def boards_with_postings(self, boards: Sequence[str]) -> set[str]:
        pass

    def liveness_queue(
        self,
        since: datetime,
        checked_before: datetime,
        feeds: Collection[str],
        boards: Collection[str],
        url_marks: Sequence[str],
    ) -> list[Job]:
        pass

    def record_checks(
        self,
        checks: Sequence[LivenessCheck],
        pushback: Sequence[RateState] = (),
        *,
        wait_ms: int | None = None,
    ) -> tuple[str, ...]:
        pass


__all__ = ["Repository", "SourceBatch", "SourceBatchResult"]
