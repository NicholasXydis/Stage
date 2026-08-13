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


@dataclass(frozen=True, slots=True)
class WorkdayCrawl:
    board: str
    next_offset: int = 0
    total: int | None = None
    facet_parameter: str = ""
    facet_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkdayCrawlStep:
    board: str
    next_offset: int = 0
    total: int | None = None
    facet_parameter: str = ""
    facet_ids: tuple[str, ...] = ()
    seen_ids: tuple[str, ...] = ()
    complete: bool = False
    reset: bool = False
    discard: bool = False


__all__ = ["WorkdayCrawl", "WorkdayCrawlStep", "WorkdayFacet"]
