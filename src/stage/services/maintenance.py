from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from stage.dedup import resolve_duplicates
from stage.domain import IntegrityRepair, Job, PurgeResult, QuarantinedJob, RateState, RoleCategory
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
    return await repository.purge(now or datetime.now(UTC))


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

    @property
    def changed(self) -> bool:
        return bool(
            self.updated or self.quarantined or self.released or self.relabelled or self.relocated
        )

    @property
    def skipped(self) -> int:
        return max(0, self.total - self.examined)


async def rescreen(repository: AsyncRepository, *, now: datetime | None = None) -> RescreenResult:
    from stage.domain import JobFilters

    moment = now or datetime.now(UTC)
    examined = 0
    quarantined = 0
    updated = 0
    total = await repository.count_jobs(JobFilters(status=None, limit=RESCREEN_LIMIT))

    for pass_number in range(RESCREEN_PASSES):
        stored = await repository.list_jobs(JobFilters(status=None, limit=RESCREEN_LIMIT))
        if not pass_number:
            examined = len(stored)
        candidates, rejected = _reclassifications(stored) if stored else ((), ())
        changed = tuple(
            candidate for job, candidate in zip(stored, candidates, strict=True) if job != candidate
        )
        if not changed and not rejected:
            break
        rejected_ids = {entry.id for entry in rejected}
        updates = tuple(entry for entry in changed if entry.id not in rejected_ids)
        sources = {entry.source for entry in updates} | {entry.source for entry in rejected}
        for source in sorted(sources):
            await repository.apply_source_batch(
                SourceBatch(
                    source=source,
                    run_started_at=moment,
                    jobs=tuple(entry for entry in updates if entry.source == source),
                    quarantined=tuple(entry for entry in rejected if entry.source == source),
                )
            )
        quarantined += len(rejected)
        updated += len(updates)
    released, relabelled = await _release_reclassified_quarantine(repository, moment)
    relocated = await repository.refresh_quarantine_locations(_resolved_place)
    return RescreenResult(
        examined=examined,
        quarantined=quarantined,
        total=total,
        updated=updated,
        released=released,
        relabelled=relabelled,
        relocated=relocated,
    )


def _resolved_place(location_raw: str) -> tuple[str, str | None]:
    from stage.normalize import resolve_location

    resolved = resolve_location(location_raw)
    scope = resolved.remote_scope
    return resolved.bucket.value, None if scope is None else scope.value


def _reclassifications(
    jobs: Sequence[Job],
) -> tuple[tuple[Job, ...], tuple[QuarantinedJob, ...]]:
    from dataclasses import replace

    from stage.classify import (
        classify_role,
        screen_degree_scope,
        screen_is_cs_role,
        screen_is_internship,
        screen_location,
    )
    from stage.classify.scope import to_quarantined
    from stage.normalize import canonical_apply_url, resolve_location

    candidates: list[Job] = []
    rejected: list[QuarantinedJob] = []
    for job in jobs:
        location = resolve_location(job.location_raw)
        title_role = classify_role(job.title_raw, job.description).role
        candidate = replace(
            job,
            apply_url_canonical=canonical_apply_url(job.apply_url_raw),
            location=location.bucket,
            remote_scope=location.remote_scope,
            role=title_role if title_role is not RoleCategory.UNKNOWN else job.role,
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
    from stage.services.sync import normalize_batch

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
    kept, rejected = normalize_batch(candidates)
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
