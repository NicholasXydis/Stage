from dataclasses import dataclass, replace
from datetime import datetime

from stage.domain.enums import (
    DegreeRequirement,
    JobLevel,
    JobStatus,
    Language,
    LocationBucket,
    RoleCategory,
)

DEFAULT_WINDOW_DAYS = 14
DEFAULT_LIMIT = 50
ALL_LEVELS = "all"
DEFAULT_LEVELS = frozenset({JobLevel.INTERNSHIP})
EVERY_LEVEL = frozenset(JobLevel)


@dataclass(frozen=True, slots=True)
class JobFilters:
    location: LocationBucket | None = None
    term: str | None = None
    role: RoleCategory | None = None
    level: JobLevel | None = None
    degree: DegreeRequirement | None = None
    language: Language | None = None
    source: str | None = None
    company: str | None = None
    status: JobStatus | None = JobStatus.OPEN
    first_seen_after: datetime | None = None
    limit: int | None = DEFAULT_LIMIT
    locations: frozenset[LocationBucket] = frozenset()
    roles: frozenset[RoleCategory] = frozenset()
    levels: frozenset[JobLevel] = frozenset()
    terms: frozenset[str] = frozenset()
    avoid_companies: frozenset[str] = frozenset()
    follow_companies: frozenset[str] = frozenset()
    excluded_ids: frozenset[str] = frozenset()
    with_duplicates: bool = False

    @property
    def level_chosen(self) -> bool:
        return self.level is not None or bool(self.levels)


def with_default_levels(filters: JobFilters) -> JobFilters:
    return filters if filters.level_chosen else replace(filters, levels=DEFAULT_LEVELS)


def chosen_level(value: str) -> tuple[JobLevel | None, frozenset[JobLevel]]:
    if value == ALL_LEVELS:
        return None, EVERY_LEVEL
    return JobLevel(value), frozenset()


def level_flag(filters: JobFilters) -> str | None:
    if filters.level is not None:
        return filters.level.value
    return ALL_LEVELS if filters.levels == EVERY_LEVEL else None
