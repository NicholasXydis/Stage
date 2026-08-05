from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class HttpValidator:
    url: str
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: datetime | None = None

    @property
    def usable(self) -> bool:
        return bool(self.etag or self.last_modified)
