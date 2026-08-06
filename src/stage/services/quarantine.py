from dataclasses import dataclass

from stage.domain import QuarantinedJob, QuarantineFilters
from stage.storage import AsyncRepository


@dataclass(frozen=True, slots=True)
class QuarantineListing:
    entries: tuple[QuarantinedJob, ...]
    total_matching: int
    reason_counts: dict[str, int]


async def list_quarantined(
    repository: AsyncRepository, filters: QuarantineFilters
) -> QuarantineListing:
    entries = await repository.list_quarantined(filters)
    total = await repository.count_quarantined(filters)
    counts = await repository.quarantine_reason_counts()
    return QuarantineListing(
        entries=tuple(entries), total_matching=total, reason_counts=counts
    )
