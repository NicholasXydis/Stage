from dataclasses import dataclass
from datetime import datetime

from stage.domain.enums import (
    DegreeRequirement,
    JobStatus,
    Language,
    LocationBucket,
    RoleCategory,
)

DEFAULT_WINDOW_DAYS = 14
DEFAULT_LIMIT = 50


@dataclass(frozen=True, slots=True)
class JobFilters:
    location: LocationBucket | None = None
    term: str | None = None
    role: RoleCategory | None = None
    degree: DegreeRequirement | None = None
    language: Language | None = None
    source: str | None = None
    company: str | None = None
    status: JobStatus | None = JobStatus.OPEN
    first_seen_after: datetime | None = None
    limit: int | None = DEFAULT_LIMIT
