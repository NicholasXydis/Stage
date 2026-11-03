import asyncio
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self, TypeVar

from stage.domain import (
    CoverageClassification,
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
    WorkdayFacet,
)
from stage.domain.liveness import LivenessCheck
from stage.storage.repository import Repository, Resolver, SourceBatch, SourceBatchResult
from stage.storage.sqlite_repo import SqliteRepository

T = TypeVar("T")


class WriterNotStartedError(RuntimeError):
    pass


class DatabaseWriter:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self._db_path = db_path
        self._read_only = read_only
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage-writer")
        self._repository: Repository | None = None
        self._thread_id: int | None = None

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    async def _submit(self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    def _open(self) -> None:
        self._thread_id = threading.get_ident()
        self._repository = SqliteRepository.connect(self._db_path, read_only=self._read_only)

    def _shutdown(self) -> None:
        if self._repository is not None:
            self._repository.close()
            self._repository = None

    async def start(self) -> Self:
        await self._submit(self._open)
        return self

    async def aclose(self) -> None:
        closed = self._executor.submit(self._shutdown)
        await asyncio.shield(asyncio.wrap_future(closed))
        self._executor.shutdown(wait=True)

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def run(self, fn: Callable[[Repository], T]) -> T:
        def call() -> T:
            repository = self._repository
            if repository is None:
                raise WriterNotStartedError("Stage used its database before opening it.")
            return fn(repository)

        return await self._submit(call)


class AsyncRepository:
    def __init__(self, writer: DatabaseWriter) -> None:
        self._writer = writer

    async def apply_source_batch(self, batch: SourceBatch) -> SourceBatchResult:
        return await self._writer.run(lambda repository: repository.apply_source_batch(batch))

    async def load_validators(self, source: str) -> Mapping[str, HttpValidator]:
        return await self._writer.run(lambda repository: repository.load_validators(source))

    async def load_rate_state(self) -> Mapping[str, RateState]:
        return await self._writer.run(lambda repository: repository.load_rate_state())

    async def clear_rate_state(self, bucket: str | None = None) -> int:
        return await self._writer.run(lambda repository: repository.clear_rate_state(bucket))

    async def stale_members(self, source: str, before: datetime) -> list[SourceVisit]:
        return await self._writer.run(lambda repository: repository.stale_members(source, before))

    async def detail_queue(self, source: str, limit: int) -> list[str]:
        return await self._writer.run(lambda repository: repository.detail_queue(source, limit))

    async def detail_queue_size(self, source: str) -> int:
        return await self._writer.run(lambda repository: repository.detail_queue_size(source))

    async def load_workday_facets(self) -> Mapping[tuple[str, str], WorkdayFacet]:
        return await self._writer.run(lambda repository: repository.load_workday_facets())

    async def load_workday_crawls(self) -> Mapping[str, WorkdayCrawl]:
        return await self._writer.run(lambda repository: repository.load_workday_crawls())

    async def clear_validators(self, source: str | None = None) -> int:
        return await self._writer.run(lambda repository: repository.clear_validators(source))

    async def cached_url_count(self) -> int:
        return await self._writer.run(lambda repository: repository.cached_url_count())

    async def list_quarantined(self, filters: QuarantineFilters) -> list[QuarantinedJob]:
        return await self._writer.run(lambda repository: repository.list_quarantined(filters))

    async def count_quarantined(self, filters: QuarantineFilters) -> int:
        return await self._writer.run(lambda repository: repository.count_quarantined(filters))

    async def quarantine_reason_counts(
        self, filters: QuarantineFilters | None = None
    ) -> dict[str, int]:
        return await self._writer.run(
            lambda repository: repository.quarantine_reason_counts(filters)
        )

    async def count_duplicates(self) -> int:
        return await self._writer.run(lambda repository: repository.count_duplicates())

    async def screened_with(self) -> int:
        return await self._writer.run(lambda repository: repository.screened_with())

    async def mark_screened(self, stamp: int) -> None:
        await self._writer.run(lambda repository: repository.mark_screened(stamp))

    async def relabel_quarantine(self, entries: Sequence[QuarantinedJob]) -> int:
        return await self._writer.run(lambda repository: repository.relabel_quarantine(entries))

    async def refresh_quarantine_locations(
        self, resolve: Callable[[str, str, str], tuple[str, str | None]]
    ) -> int:
        return await self._writer.run(
            lambda repository: repository.refresh_quarantine_locations(resolve)
        )

    async def relink_duplicates(self, resolve: Resolver, canonical: Callable[[str], str]) -> int:
        return await self._writer.run(
            lambda repository: repository.relink_duplicates(resolve, canonical)
        )

    async def repair_integrity(self) -> list[IntegrityRepair]:
        return await self._writer.run(lambda repository: repository.repair_integrity())

    async def purge(self, now: datetime) -> PurgeResult:
        return await self._writer.run(lambda repository: repository.purge(now))

    async def pending_heirs(self) -> list[tuple[str, str]]:
        return await self._writer.run(lambda repository: repository.pending_heirs())

    async def forget_heirs(self, formers: Sequence[str]) -> None:
        await self._writer.run(lambda repository: repository.forget_heirs(formers))

    async def close_orphan_boards(self, sources: Sequence[str], boards: Sequence[str]) -> int:
        return await self._writer.run(
            lambda repository: repository.close_orphan_boards(sources, boards)
        )

    async def preview_purge(self, now: datetime) -> PurgeResult:
        return await self._writer.run(lambda repository: repository.preview_purge(now))

    async def tombstone_count(self) -> int:
        return await self._writer.run(lambda repository: repository.tombstone_count())

    async def list_jobs(self, filters: JobFilters) -> list[Job]:
        return await self._writer.run(lambda repository: repository.list_jobs(filters))

    async def count_jobs(self, filters: JobFilters) -> int:
        return await self._writer.run(lambda repository: repository.count_jobs(filters))

    async def closed_among(self, job_ids: Sequence[str]) -> int:
        return await self._writer.run(lambda repository: repository.closed_among(job_ids))

    async def requests_since(
        self, since: datetime, until: datetime | None = None
    ) -> tuple[dict[str, int], bool]:
        return await self._writer.run(lambda repository: repository.requests_since(since, until))

    async def pushback_since(
        self, since: datetime, until: datetime | None = None
    ) -> frozenset[str]:
        return await self._writer.run(lambda repository: repository.pushback_since(since, until))

    async def board_activity(self, month: datetime, week: datetime) -> dict[str, tuple[int, int]]:
        return await self._writer.run(lambda repository: repository.board_activity(month, week))

    async def earliest_copies(self, ids: Sequence[str]) -> dict[str, datetime]:
        return await self._writer.run(lambda repository: repository.earliest_copies(ids))

    async def first_reads(self) -> dict[str, datetime]:
        return await self._writer.run(lambda repository: repository.first_reads())

    async def full_reads(self, source: str, prefix: str) -> dict[str, tuple[datetime, bool, bool]]:
        return await self._writer.run(lambda repository: repository.full_reads(source, prefix))

    async def feed_tips(
        self, sources: Collection[str], since: datetime, limit: int
    ) -> dict[str, datetime]:
        return await self._writer.run(
            lambda repository: repository.feed_tips(sources, since, limit)
        )

    async def previous_sync_at(self) -> datetime | None:
        return await self._writer.run(lambda repository: repository.previous_sync_at())

    async def company_names(self) -> list[str]:
        return await self._writer.run(lambda repository: repository.company_names())

    async def ids_with_prefix(self, prefix: str) -> list[str]:
        return await self._writer.run(lambda repository: repository.ids_with_prefix(prefix))

    async def open_counts_by_company(self, keys: Collection[str]) -> dict[str, int]:
        return await self._writer.run(lambda repository: repository.open_counts_by_company(keys))

    async def canonical_ids(self, job_ids: Collection[str]) -> dict[str, str]:
        return await self._writer.run(lambda repository: repository.canonical_ids(job_ids))

    async def posting_states(self, job_ids: Collection[str]) -> dict[str, JobStatus]:
        return await self._writer.run(lambda repository: repository.posting_states(job_ids))

    async def get_job(self, job_id: str) -> Job | None:
        return await self._writer.run(lambda repository: repository.get_job(job_id))

    async def duplicates_of(self, job_id: str) -> list[Job]:
        return await self._writer.run(lambda repository: repository.duplicates_of(job_id))

    async def search_jobs(self, query: str, filters: JobFilters) -> list[Job]:
        return await self._writer.run(lambda repository: repository.search_jobs(query, filters))

    async def count_search(self, query: str, filters: JobFilters) -> int:
        return await self._writer.run(lambda repository: repository.count_search(query, filters))

    async def board_counts(self) -> dict[str, int]:
        return await self._writer.run(lambda repository: repository.board_counts())

    async def company_counts(self) -> dict[str, dict[str, int]]:
        return await self._writer.run(lambda repository: repository.company_counts())

    async def quarantine_company_counts(self) -> dict[str, dict[str, int]]:
        return await self._writer.run(lambda repository: repository.quarantine_company_counts())

    async def quarantine_company_reasons(
        self, reasons: Collection[str] = ()
    ) -> dict[str, dict[str, int]]:
        return await self._writer.run(
            lambda repository: repository.quarantine_company_reasons(reasons)
        )

    async def company_apply_urls(
        self, companies: Sequence[str], *, kept_only: bool = False
    ) -> dict[str, tuple[str, ...]]:
        return await self._writer.run(
            lambda repository: repository.company_apply_urls(companies, kept_only=kept_only)
        )

    async def coverage_classifications(self) -> list[CoverageClassification]:
        return await self._writer.run(lambda repository: repository.coverage_classifications())

    async def record_coverage_classification(self, entry: CoverageClassification) -> bool:
        return await self._writer.run(
            lambda repository: repository.record_coverage_classification(entry)
        )

    async def clear_coverage_classification(self, company: str) -> bool:
        return await self._writer.run(
            lambda repository: repository.clear_coverage_classification(company)
        )

    async def record_sync_run(self, run: SyncRun) -> None:
        await self._writer.run(lambda repository: repository.record_sync_run(run))

    async def last_sync_at(self) -> datetime | None:
        return await self._writer.run(lambda repository: repository.last_sync_at())

    async def volume_history(self, limit: int) -> Mapping[str, list[VolumePoint]]:
        return await self._writer.run(lambda repository: repository.volume_history(limit))

    async def run_history(self, limit: int) -> list[SyncRun]:
        return await self._writer.run(lambda repository: repository.run_history(limit))

    async def all_visits(self) -> list[SourceVisit]:
        return await self._writer.run(lambda repository: repository.all_visits())

    async def integrity_findings(self, *, whole_file: bool = False) -> list[IntegrityFinding]:
        return await self._writer.run(
            lambda repository: repository.integrity_findings(whole_file=whole_file)
        )

    async def composition(self, column: str) -> dict[str, int]:
        return await self._writer.run(lambda repository: repository.composition(column))

    async def stored_counts(self) -> dict[str, int]:
        return await self._writer.run(lambda repository: repository.stored_counts())

    async def schema_version(self) -> int:
        return await self._writer.run(lambda repository: repository.schema_version())

    async def boards_with_postings(self, boards: Sequence[str]) -> set[str]:
        return await self._writer.run(lambda repository: repository.boards_with_postings(boards))

    async def liveness_queue(
        self,
        since: datetime,
        checked_before: datetime,
        feeds: Collection[str],
        boards: Collection[str],
        url_marks: Sequence[str],
    ) -> list[Job]:
        return await self._writer.run(
            lambda repository: repository.liveness_queue(
                since, checked_before, feeds, boards, url_marks
            )
        )

    async def record_checks(
        self,
        checks: Sequence[LivenessCheck],
        pushback: Sequence[RateState] = (),
        *,
        wait_ms: int | None = None,
    ) -> tuple[str, ...]:
        return await self._writer.run(
            lambda repository: repository.record_checks(checks, pushback, wait_ms=wait_ms)
        )
