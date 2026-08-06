from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from stage.domain import DEFAULT_WINDOW_DAYS, Job, JobFilters
from stage.storage import AsyncRepository


@dataclass(frozen=True, slots=True)
class JobListing:
    jobs: tuple[Job, ...]
    total_matching: int
    window_days: int | None
    last_sync_at: datetime | None


async def list_jobs(
    repository: AsyncRepository,
    filters: JobFilters,
    *,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> JobListing:
    effective = filters
    if window_days is not None and filters.first_seen_after is None:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=window_days)
        effective = replace(filters, first_seen_after=cutoff)

    jobs = await repository.list_jobs(effective)
    total = await repository.count_jobs(effective)
    last_sync = await repository.last_sync_at()
    return JobListing(
        jobs=tuple(jobs),
        total_matching=total,
        window_days=window_days,
        last_sync_at=last_sync,
    )
