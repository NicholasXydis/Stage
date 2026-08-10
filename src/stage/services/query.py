from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from stage.domain import DEFAULT_WINDOW_DAYS, Job, JobFilters
from stage.storage import AsyncRepository
from stage.storage.search import search_terms


@dataclass(frozen=True, slots=True)
class JobListing:
    jobs: tuple[Job, ...]
    total_matching: int
    window_days: int | None
    last_sync_at: datetime | None
    query: str = ""
    terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PostingDetail:
    job: Job
    duplicates: tuple[Job, ...]
    canonical: Job | None


def _windowed(filters: JobFilters, window_days: int | None, now: datetime | None) -> JobFilters:
    if window_days is None or filters.first_seen_after is not None:
        return filters
    cutoff = (now or datetime.now(UTC)) - timedelta(days=window_days)
    return replace(filters, first_seen_after=cutoff)


async def list_jobs(
    repository: AsyncRepository,
    filters: JobFilters,
    *,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> JobListing:
    effective = _windowed(filters, window_days, now)
    jobs = await repository.list_jobs(effective)
    total = await repository.count_jobs(effective)
    last_sync = await repository.last_sync_at()
    return JobListing(
        jobs=tuple(jobs),
        total_matching=total,
        window_days=window_days,
        last_sync_at=last_sync,
    )


async def search_jobs(
    repository: AsyncRepository,
    query: str,
    filters: JobFilters,
    *,
    window_days: int | None = None,
    now: datetime | None = None,
) -> JobListing:
    effective = _windowed(filters, window_days, now)
    terms = search_terms(query)
    jobs = await repository.search_jobs(query, effective) if terms else []
    total = await repository.count_search(query, effective) if terms else 0
    return JobListing(
        jobs=tuple(jobs),
        total_matching=total,
        window_days=window_days,
        last_sync_at=await repository.last_sync_at(),
        query=query,
        terms=terms,
    )


async def get_posting(repository: AsyncRepository, job_id: str) -> PostingDetail | None:
    job = await repository.get_job(job_id)
    if job is None:
        return None
    canonical = await repository.get_job(job.duplicate_of) if job.duplicate_of is not None else None
    return PostingDetail(
        job=job,
        duplicates=tuple(await repository.duplicates_of(job.id)),
        canonical=canonical,
    )


__all__ = ["JobListing", "PostingDetail", "get_posting", "list_jobs", "search_jobs"]
