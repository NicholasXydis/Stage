from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from stage.domain import Job, PurgeResult, QuarantinedJob, RateState
from stage.storage import AsyncRepository, SourceBatch

RESCREEN_LIMIT = 100_000
RESCREEN_PASSES = 8


@dataclass(frozen=True, slots=True)
class RateStateView:
    cleared: int
    states: tuple[RateState, ...]
    validators_cleared: int = 0


async def purge_expired(repository: AsyncRepository, *, now: datetime | None = None) -> PurgeResult:
    return await repository.purge(now or datetime.now(UTC))


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

    @property
    def changed(self) -> bool:
        return bool(self.quarantined)


async def rescreen(repository: AsyncRepository, *, now: datetime | None = None) -> RescreenResult:
    from stage.domain import JobFilters

    moment = now or datetime.now(UTC)
    examined = 0
    quarantined = 0

    for pass_number in range(RESCREEN_PASSES):
        stored = await repository.list_jobs(JobFilters(status=None, limit=RESCREEN_LIMIT))
        if not pass_number:
            examined = len(stored)
        rejected = _rejections(stored) if stored else ()
        if not rejected:
            break
        for source in sorted({entry.source for entry in rejected}):
            await repository.apply_source_batch(
                SourceBatch(
                    source=source,
                    run_started_at=moment,
                    quarantined=tuple(entry for entry in rejected if entry.source == source),
                )
            )
        quarantined += len(rejected)
    return RescreenResult(examined=examined, quarantined=quarantined)


def _rejections(jobs: Sequence[Job]) -> tuple[QuarantinedJob, ...]:
    from dataclasses import replace

    from stage.classify import (
        screen_degree_scope,
        screen_is_cs_role,
        screen_is_internship,
        screen_location,
    )
    from stage.classify.scope import to_quarantined
    from stage.normalize import resolve_location

    rejected: list[QuarantinedJob] = []
    for job in jobs:
        location = resolve_location(job.location_raw)
        candidate = replace(job, location=location.bucket, remote_scope=location.remote_scope)
        rejection = (
            screen_location(candidate, location.evidence)
            or screen_is_internship(candidate)
            or screen_degree_scope(candidate)
            or screen_is_cs_role(candidate)
        )
        if rejection is not None:
            rejected.append(to_quarantined(candidate, rejection))
    return tuple(rejected)
