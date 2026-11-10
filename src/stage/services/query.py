from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from stage.domain import DEFAULT_WINDOW_DAYS, Job, JobFilters, LocationBucket
from stage.storage import AsyncRepository
from stage.storage.search import search_terms
from stage.storage.sql import fold_company


@dataclass(frozen=True, slots=True)
class JobListing:
    jobs: tuple[Job, ...]
    total_matching: int
    window_days: int | None
    last_sync_at: datetime | None
    query: str = ""
    terms: tuple[str, ...] = ()
    no_location: int | None = None


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


async def count_without_location(
    repository: AsyncRepository,
    filters: JobFilters,
    *,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> int | None:
    return await _left_out(repository, _windowed(filters, window_days, now))


async def count_hidden(
    repository: AsyncRepository,
    filters: JobFilters,
    hidden: frozenset[str],
    *,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    query: str = "",
) -> int:
    hidden = hidden & filters.excluded_ids
    if not hidden or (query and not search_terms(query)):
        return 0
    effective = _windowed(filters, window_days, now)
    widened = replace(effective, excluded_ids=effective.excluded_ids - hidden)
    if query:
        return await repository.count_search(query, widened) - await repository.count_search(
            query, effective
        )
    return await repository.count_jobs(widened) - await repository.count_jobs(effective)


async def _left_out(
    repository: AsyncRepository, filters: JobFilters, query: str = ""
) -> int | None:
    if not filters.locations:
        return None
    unplaced = replace(
        filters, locations=frozenset({LocationBucket.UNKNOWN}), follow_companies=frozenset()
    )
    if query:
        return await repository.count_search(query, unplaced)
    return await repository.count_jobs(unplaced)


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
        no_location=await _left_out(repository, effective),
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
        no_location=await _left_out(repository, effective, query) if terms else None,
    )


async def company_match(repository: AsyncRepository, company: str) -> tuple[bool, str | None]:
    from difflib import get_close_matches

    names = await repository.company_names()
    wanted = fold_company(company)
    if company in names or (wanted and any(fold_company(name) == wanted for name in names)):
        return True, None
    contained = sorted((name for name in names if wanted and wanted in fold_company(name)), key=len)
    if contained:
        return False, contained[0]
    folded: dict[str, str] = {}
    for name in names:
        folded.setdefault(name.casefold(), name)
    near = get_close_matches(company.casefold(), list(folded), n=1, cutoff=0.8)
    return False, folded[near[0]] if near else None


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


def searched_as(query: str, terms: tuple[str, ...], width: int = 60) -> str:
    import unicodedata

    from stage.domain.text import truncate
    from stage.lexicon import fold

    lost = [
        part
        for part in unicodedata.normalize("NFC", query)
        if not part.isspace() and not fold(part).strip()
    ]
    shown = "".join(part for part in query if unicodedata.category(part) not in ("Cc", "Cf"))
    if not lost or (terms and " ".join(shown.lower().split()) == " ".join(terms)):
        return ""
    reason = (
        "Stage searches only Latin letters and digits"
        if any(part.isalpha() for part in lost)
        else "Stage drops punctuation and symbols"
    )
    asked = truncate(query.strip(), width)
    if not terms:
        return f'{reason}, so nothing in "{asked}" could be searched.'
    return f'{reason}, so "{asked}" was searched as "{truncate(" ".join(terms), width)}".'


__all__ = [
    "JobListing",
    "PostingDetail",
    "company_match",
    "count_hidden",
    "count_without_location",
    "get_posting",
    "list_jobs",
    "search_jobs",
    "searched_as",
]
