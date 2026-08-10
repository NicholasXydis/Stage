from collections.abc import Mapping
from datetime import datetime

import httpx

from stage.domain import HttpValidator


class ValidatorCache:
    def __init__(self, seed: Mapping[str, HttpValidator] | None = None) -> None:
        self._entries: dict[str, HttpValidator] = dict(seed or {})
        self._dirty: dict[str, HttpValidator] = {}

    def get(self, url: str) -> HttpValidator | None:
        entry = self._entries.get(url)
        return entry if entry is not None and entry.usable else None

    def conditional_headers(self, url: str) -> dict[str, str]:
        entry = self.get(url)
        if entry is None:
            return {}
        headers: dict[str, str] = {}
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        return headers

    def record(self, url: str, headers: httpx.Headers, fetched_at: datetime) -> None:
        etag = headers.get("etag")
        last_modified = headers.get("last-modified")
        if not etag and not last_modified:
            return
        entry = HttpValidator(
            url=url, etag=etag, last_modified=last_modified, fetched_at=fetched_at
        )
        if self._entries.get(url) == entry:
            return
        self._entries[url] = entry
        self._dirty[url] = entry

    @property
    def pending(self) -> Mapping[str, HttpValidator]:
        return dict(self._dirty)
