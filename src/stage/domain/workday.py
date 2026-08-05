from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class WorkdayFacet:

    tenant: str
    site: str
    parameter: str
    facet_ids: tuple[str, ...]
    descriptor: str = ""
    resolved_at: datetime | None = None
    pinned: bool = False


__all__ = ["WorkdayFacet"]
