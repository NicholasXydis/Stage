from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CompanyVisit:
    board: str
    succeeded: bool
    error: str = ""
    label: str = ""


@dataclass(frozen=True, slots=True)
class SourceVisit:
    source: str
    board: str
    last_attempt_at: datetime
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: str = ""
    label: str = ""

    @property
    def never_succeeded(self) -> bool:
        return self.last_success_at is None


@dataclass(frozen=True, slots=True)
class DetailFetch:
    id: str
    resolved: bool = False
    failed: bool = False


__all__ = ["CompanyVisit", "DetailFetch", "SourceVisit"]
