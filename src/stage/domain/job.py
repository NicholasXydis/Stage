from dataclasses import dataclass
from datetime import datetime

from stage.domain.enums import (
    UNKNOWN_TERM,
    DegreeRequirement,
    JobStatus,
    Language,
    LocationBucket,
    RemoteScope,
    RoleCategory,
)
from stage.domain.ids import board_of
from stage.domain.signals import SourceSignals


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    source: str
    company: str
    title_raw: str
    title_normalized: str
    apply_url_raw: str
    description: str
    first_seen: datetime
    last_seen: datetime
    location_raw: str = ""
    title_canonical: str = ""
    apply_url_canonical: str = ""
    status: JobStatus = JobStatus.OPEN
    language: Language = Language.UNKNOWN
    location: LocationBucket = LocationBucket.UNKNOWN
    remote_scope: RemoteScope | None = None
    term: str = UNKNOWN_TERM
    role: RoleCategory = RoleCategory.UNKNOWN
    work_auth_flag: bool = False
    degree_requirement: DegreeRequirement = DegreeRequirement.UNKNOWN
    compensation: str | None = None
    source_posted_at: datetime | None = None
    duplicate_of: str | None = None

    @property
    def board_key(self) -> str:
        return board_of(self.id, self.source)

    signals: SourceSignals = SourceSignals()
