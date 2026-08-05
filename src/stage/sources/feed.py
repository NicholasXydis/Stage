
from datetime import datetime
from typing import ClassVar, Protocol, runtime_checkable

from stage.http import HttpClient
from stage.sources.base import FetchResult

_FEEDS: dict[str, "FeedAdapter"] = {}


@runtime_checkable
class FeedAdapter(Protocol):
    name: ClassVar[str]
    rate_profile: ClassVar[str]
    hosts: ClassVar[frozenset[str]]
    bucket_key: ClassVar[str]

    def season_year(self, now: datetime) -> int: ...

    def plan(self, now: datetime) -> tuple[str, ...]: ...

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult: ...


def register_feed[F: FeedAdapter](cls: type[F]) -> type[F]:
    adapter = cls()
    existing = _FEEDS.get(adapter.name)
    if existing is not None and type(existing) is not cls:
        raise ValueError(f"two feeds claim the name {adapter.name!r}")
    _FEEDS[adapter.name] = adapter
    return cls


def get_feeds() -> dict[str, FeedAdapter]:
    from stage.sources import load_builtins

    load_builtins()
    return dict(_FEEDS)


def upcoming_season_year(now: datetime, rolls_in_month: int = 8) -> int:
    return now.year + 1 if now.month >= rolls_in_month else now.year
