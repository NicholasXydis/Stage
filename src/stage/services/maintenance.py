from dataclasses import dataclass
from datetime import UTC, datetime

from stage.domain import PurgeResult, RateState
from stage.storage import AsyncRepository


@dataclass(frozen=True, slots=True)
class RateStateView:
    cleared: int
    states: tuple[RateState, ...]


async def purge_expired(
    repository: AsyncRepository, *, now: datetime | None = None
) -> PurgeResult:
    return await repository.purge(now or datetime.now(UTC))


async def rate_state(
    repository: AsyncRepository,
    *,
    bucket: str | None = None,
    clear_all: bool = False,
) -> RateStateView:
    cleared = 0
    if clear_all:
        cleared = await repository.clear_rate_state()
    elif bucket is not None:
        cleared = await repository.clear_rate_state(bucket)
    states = await repository.load_rate_state()
    return RateStateView(cleared=cleared, states=tuple(states.values()))
