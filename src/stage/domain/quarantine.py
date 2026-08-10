from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from stage.domain.enums import LocationBucket, RemoteScope

DEFAULT_QUARANTINE_LIMIT = 50


class RejectionReason(StrEnum):
    OUT_OF_SCOPE_LOCATION = "out-of-scope-location"
    NOT_AN_INTERNSHIP = "not-an-internship"
    OUT_OF_SCOPE_DEGREE = "out-of-scope-degree"
    NOT_A_CS_ROLE = "not-a-cs-role"


@dataclass(frozen=True, slots=True)
class QuarantinedJob:
    id: str
    source: str
    company: str
    title_raw: str
    reason: RejectionReason
    first_seen: datetime
    last_seen: datetime
    apply_url_raw: str = ""
    location_raw: str = ""
    location: LocationBucket = LocationBucket.UNKNOWN
    remote_scope: RemoteScope | None = None
    matched_phrase: str = ""


@dataclass(frozen=True, slots=True)
class QuarantineFilters:
    reason: RejectionReason | None = None
    source: str | None = None
    company: str | None = None
    limit: int = DEFAULT_QUARANTINE_LIMIT
