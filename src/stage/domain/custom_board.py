from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

TITLE_FIELD = "title"
REQUIRED_FIELDS = (TITLE_FIELD,)
KNOWN_FIELDS = (TITLE_FIELD, "id", "location", "url", "description", "department")

_EMPTY: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CustomBoard:
    url: str
    jobs_path: str = ""
    fields: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    url_template: str = ""

    def mapped(self, name: str) -> str:
        return self.fields.get(name, "")


__all__ = ["KNOWN_FIELDS", "REQUIRED_FIELDS", "TITLE_FIELD", "CustomBoard"]
