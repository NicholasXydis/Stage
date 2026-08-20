from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

TITLE_FIELD = "title"
REQUIRED_FIELDS = (TITLE_FIELD,)
KNOWN_FIELDS = (
    TITLE_FIELD,
    "id",
    "location",
    "url",
    "description",
    "department",
    "employment_type",
    "category",
)

_EMPTY: Mapping[str, str] = MappingProxyType({})
_EMPTY_BODY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CustomBoard:
    url: str
    jobs_path: str = ""
    fields: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    url_template: str = ""
    page_param: str = ""
    page_size: int = 0
    page_start: int = 0
    page_step: int = 0
    max_pages: int = 0
    method: str = "GET"
    fmt: str = "json"
    extract: str = ""
    row_selector: str = ""
    handshake_url: str = ""
    token_pattern: str = ""
    token_header: str = ""
    token_prefix: str = ""
    body: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_BODY)
    headers: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    authoritative: bool = True

    def mapped(self, name: str) -> str:
        return self.fields.get(name, "")

    @property
    def embedded(self) -> bool:
        return bool(self.extract)

    @property
    def rss(self) -> bool:
        return self.fmt == "rss"

    @property
    def html(self) -> bool:
        return self.fmt == "html"

    @property
    def sitemap(self) -> bool:
        return self.fmt == "sitemap"

    @property
    def jsonld(self) -> bool:
        return self.fmt == "jsonld"

    @property
    def handshakes(self) -> bool:
        return bool(self.handshake_url and self.token_pattern and self.token_header)

    @property
    def posts(self) -> bool:
        return self.method.upper() == "POST"

    @property
    def paginated(self) -> bool:
        return bool(self.page_param) and self.page_size > 0

    def page_budget(self, default: int, ceiling: int) -> int:
        if not self.paginated:
            return 1
        return min(ceiling, self.max_pages or default)

    def page_value(self, index: int) -> int:
        return self.page_start + index * (self.page_step or self.page_size)


__all__ = ["KNOWN_FIELDS", "REQUIRED_FIELDS", "TITLE_FIELD", "CustomBoard"]
