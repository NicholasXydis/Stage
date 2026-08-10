from dataclasses import dataclass
from datetime import datetime

OPEN_RETENTION_DAYS = 14
CLOSED_RETENTION_DAYS = 3


@dataclass(frozen=True, slots=True)
class PurgeResult:
    purged: int = 0
    tombstoned: int = 0
    promoted: int = 0


@dataclass(frozen=True, slots=True)
class Tombstone:
    id: str
    source: str
    first_seen: datetime
    purged_at: datetime
