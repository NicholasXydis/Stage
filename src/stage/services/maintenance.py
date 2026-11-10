from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from stage.dedup import resolve_duplicates
from stage.domain import (
    UNKNOWN_TERM,
    IntegrityRepair,
    Job,
    PurgeResult,
    QuarantinedJob,
    RateState,
    RoleCategory,
)
from stage.storage import AsyncRepository, SourceBatch

RESCREEN_LIMIT = 100_000
RESCREEN_PASSES = 8


@dataclass(frozen=True, slots=True)
class RateStateView:
    cleared: int
    states: tuple[RateState, ...]
    validators_cleared: int = 0


async def repair_integrity(repository: AsyncRepository) -> tuple[IntegrityRepair, ...]:
    return tuple(await repository.repair_integrity())


async def purge_expired(repository: AsyncRepository, *, now: datetime | None = None) -> PurgeResult:
    from stage.services.tracker import follow_recorded

    result = await repository.purge(now or datetime.now(UTC))
    if await follow_recorded(repository) is None:
        return replace(result, tracker_busy=True)
    return result


async def preview_purge(repository: AsyncRepository, *, now: datetime | None = None) -> PurgeResult:
    return await repository.preview_purge(now or datetime.now(UTC))


async def rate_state(
    repository: AsyncRepository,
    *,
    bucket: str | None = None,
    clear_all: bool = False,
    clear_cache: str | None = None,
    clear_cache_all: bool = False,
) -> RateStateView:
    cleared = 0
    if clear_all:
        cleared = await repository.clear_rate_state()
    elif bucket is not None:
        cleared = await repository.clear_rate_state(bucket)
    validators = 0
    if clear_cache_all:
        validators = await repository.clear_validators()
    elif clear_cache is not None:
        validators = await repository.clear_validators(clear_cache)
    states = await repository.load_rate_state()
    return RateStateView(
        cleared=cleared, states=tuple(states.values()), validators_cleared=validators
    )


@dataclass(frozen=True, slots=True)
class RescreenResult:
    examined: int
    quarantined: int
    total: int = 0
    updated: int = 0
    released: int = 0
    relabelled: int = 0
    relocated: int = 0
    relinked: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.updated
            or self.quarantined
            or self.released
            or self.relabelled
            or self.relocated
            or self.relinked
        )

    @property
    def skipped(self) -> int:
        return max(0, self.total - self.examined)


RECHECK_NOTICE = "Checking your stored postings first. This can take a few minutes."


async def upgrade_pending(repository: AsyncRepository) -> bool:
    from stage.paths import rules_stamp

    return await repository.screened_with() != rules_stamp()


def upgrade_note(result: RescreenResult) -> str:
    from stage.domain.text import counted

    return (
        "This version of Stage screens postings differently, so it checked the ones already "
        f"stored: {counted(result.quarantined, 'posting')} now rejected, "
        f"{counted(result.released, 'posting')} brought back."
    )


async def rescreen(repository: AsyncRepository, *, now: datetime | None = None) -> RescreenResult:
    from stage.domain import JobFilters
    from stage.normalize import canonical_apply_url
    from stage.paths import rules_stamp
    from stage.services.tracker import follow_recorded

    moment = now or datetime.now(UTC)
    examined = 0
    quarantined = 0
    updated = 0
    total = await repository.count_jobs(
        JobFilters(status=None, limit=RESCREEN_LIMIT, with_duplicates=True)
    )
    settled: dict[str, Job] = {}

    for pass_number in range(RESCREEN_PASSES):
        stored = await repository.list_jobs(
            JobFilters(status=None, limit=RESCREEN_LIMIT, with_duplicates=True)
        )
        if not pass_number:
            examined = len(stored)
        pending = [job for job in stored if settled.get(job.id) != job]
        candidates, rejected = _reclassifications(pending) if pending else ((), ())
        rejected_ids = {entry.id for entry in rejected}
        changed: list[Job] = []
        for job, candidate in zip(pending, candidates, strict=True):
            if job != candidate:
                changed.append(candidate)
            elif job.id not in rejected_ids:
                settled[job.id] = job
        if not changed and not rejected:
            break
        updates = tuple(entry for entry in changed if entry.id not in rejected_ids)
        sources = {entry.source for entry in updates} | {entry.source for entry in rejected}
        for source in sorted(sources):
            applied = await repository.apply_source_batch(
                SourceBatch(
                    source=source,
                    run_started_at=moment,
                    jobs=tuple(entry for entry in updates if entry.source == source),
                    quarantined=tuple(entry for entry in rejected if entry.source == source),
                )
            )
            if applied.successors:
                await follow_recorded(repository)
            quarantined += applied.quarantined
        updated += len(updates)
    released, relabelled = await _release_reclassified_quarantine(repository, moment)
    relocated = await repository.refresh_quarantine_locations(_place_resolver())
    relinked = await repository.relink_duplicates(resolve_duplicates, canonical_apply_url)
    await repository.mark_screened(rules_stamp(), at=moment)
    return RescreenResult(
        examined=examined,
        quarantined=quarantined,
        total=total,
        updated=updated,
        released=released,
        relabelled=relabelled,
        relocated=relocated,
        relinked=relinked,
    )


def _current_term(job: Job) -> str:
    from stage.normalize import resolve_term
    from stage.normalize.terms import ended_before, fits_title, graduation_only

    since = job.source_posted_at or job.first_seen
    titled = resolve_term(title=job.title_raw, pivot_year=since.year, posted=since)
    if titled.conflict:
        return UNKNOWN_TERM
    if titled.term != UNKNOWN_TERM:
        return titled.term
    if job.term == UNKNOWN_TERM:
        written = resolve_term(
            title=job.title_raw, description=job.description, pivot_year=since.year, posted=since
        ).term
        return written if fits_title(written, job.title_raw) else UNKNOWN_TERM
    if ended_before(job.term, since) or not fits_title(job.term, job.title_raw):
        return UNKNOWN_TERM
    if graduation_only(job.term, job.title_raw, job.description, since.year):
        return UNKNOWN_TERM
    return job.term


def _place_resolver() -> Callable[[str, str, str], tuple[str, str | None]]:
    from stage.domain import LocationBucket
    from stage.normalize import listed_place, resolve_listing, resolve_location

    def resolve(raw: str, url: str, title: str) -> tuple[str, str | None]:
        resolved = resolve_location(raw)
        if resolved.bucket is LocationBucket.UNKNOWN:
            resolved = resolve_listing(listed_place(raw, url, title), title)
        scope = resolved.remote_scope
        return resolved.bucket.value, None if scope is None else scope.value

    return resolve


def _reclassifications(
    jobs: Sequence[Job],
) -> tuple[tuple[Job, ...], tuple[QuarantinedJob, ...]]:
    from stage.classify import (
        classify_role,
        resolve_eligibility,
        screen_degree_scope,
        screen_is_cs_role,
        screen_is_internship,
        screen_location,
    )
    from stage.classify.scope import to_quarantined
    from stage.normalize import (
        canonical_apply_url,
        detect_language,
        listed_place,
        resolve_listing,
        shown_company,
    )

    candidates: list[Job] = []
    rejected: list[QuarantinedJob] = []
    for job in jobs:
        place = listed_place(job.location_raw, job.apply_url_raw, job.title_raw)
        location = resolve_listing(place, job.title_raw)
        title_role = classify_role(job.title_raw, job.description).role
        candidate = replace(
            job,
            company=shown_company(job.company),
            location_raw=place,
            apply_url_canonical=canonical_apply_url(job.apply_url_raw),
            location=location.bucket,
            remote_scope=location.remote_scope,
            role=title_role if title_role is not RoleCategory.UNKNOWN else job.role,
            term=_current_term(job),
        )
        eligibility = resolve_eligibility(candidate)
        candidate = replace(
            candidate,
            language=detect_language(job.title_raw, job.description).language,
            degree_requirement=eligibility.degree_requirement,
            work_auth_flag=eligibility.work_auth_flag,
        )
        candidates.append(candidate)
        rejection = (
            screen_is_internship(candidate)
            or screen_location(candidate)
            or screen_degree_scope(candidate)
            or screen_is_cs_role(candidate)
        )
        if rejection is not None:
            rejected.append(to_quarantined(candidate, rejection))
    return tuple(candidates), tuple(rejected)


async def _release_reclassified_quarantine(
    repository: AsyncRepository,
    moment: datetime,
) -> tuple[int, int]:
    from stage.domain import QuarantineFilters, RejectionReason
    from stage.lexicon import fold
    from stage.services.screening import Screener

    remaining = RESCREEN_LIMIT
    entries: list[QuarantinedJob] = []
    for reason in (
        RejectionReason.UNKNOWN_CS_ROLE,
        RejectionReason.NOT_A_CS_ROLE,
        RejectionReason.OUT_OF_SCOPE_LOCATION,
    ):
        if not remaining:
            break
        found = await repository.list_quarantined(QuarantineFilters(reason=reason, limit=remaining))
        entries.extend(found)
        remaining -= len(found)
    candidates = tuple(
        Job(
            id=entry.id,
            source=entry.source,
            company=entry.company,
            title_raw=entry.title_raw,
            title_normalized=fold(entry.title_raw),
            apply_url_raw=entry.apply_url_raw,
            description="",
            first_seen=entry.first_seen,
            last_seen=entry.last_seen,
            location_raw=entry.location_raw,
            location=entry.location,
            remote_scope=entry.remote_scope,
        )
        for entry in entries
    )
    screener = Screener()
    try:
        kept, rejected = await screener(candidates)
    finally:
        screener.close()
    known = {entry.id: entry for entry in entries}
    sharpened = tuple(
        entry
        for entry in rejected
        if (previous := known.get(entry.id)) is not None
        and (previous.reason, previous.matched_phrase) != (entry.reason, entry.matched_phrase)
    )
    relabelled = await repository.relabel_quarantine(sharpened)
    for source in sorted({entry.source for entry in kept}):
        await repository.apply_source_batch(
            SourceBatch(
                source=source,
                run_started_at=moment,
                jobs=tuple(entry for entry in kept if entry.source == source),
                resolve_duplicates=resolve_duplicates,
            )
        )
    return len(kept), relabelled
