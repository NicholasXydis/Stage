import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from stage.domain import (
    JobFilters,
    Language,
    LocationBucket,
    RoleCategory,
)

DEBOUNCE_SECONDS = 0.04
PAGE_SIZE = 200
MAX_SAVED = 9

FILTER_FIELDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("role", "Role", tuple(value.value for value in RoleCategory if value.value != "hardware")),
    ("location", "Location", tuple(value.value for value in LocationBucket)),
    ("language", "Language", tuple(value.value for value in Language)),
)

_ENUMS: dict[str, Any] = {
    "role": RoleCategory,
    "location": LocationBucket,
    "language": Language,
}


@dataclass
class FilterState:
    query: str = ""
    limit: int = PAGE_SIZE
    values: dict[str, str] = field(default_factory=dict)

    def toggle(self, name: str, value: str) -> None:
        if self.values.get(name) == value:
            self.values.pop(name, None)
            return
        self.values[name] = value

    def clear(self) -> None:
        self.values.clear()
        self.query = ""
        self.limit = PAGE_SIZE

    @property
    def active(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (name, self.values[name]) for name, _, _ in FILTER_FIELDS if name in self.values
        )

    def as_filters(self) -> JobFilters:
        chosen: dict[str, Any] = {}
        for name, value in self.values.items():
            enum = _ENUMS.get(name)
            if enum is None:
                continue
            try:
                chosen[name] = enum(value)
            except ValueError:
                continue
        return JobFilters(limit=self.limit, **chosen)

    def payload(self) -> dict[str, Any]:
        return {"query": self.query, "values": dict(self.values)}

    def restore(self, payload: dict[str, Any]) -> None:
        self.query = str(payload.get("query", ""))
        stored = payload.get("values")
        self.values = (
            {str(k): str(v) for k, v in stored.items()} if isinstance(stored, dict) else {}
        )
        self.limit = PAGE_SIZE


def describe(state: FilterState) -> str:
    parts = [f"{value}" for _, value in state.active]
    if state.query:
        parts.insert(0, f'"{state.query}"')
    return " · ".join(parts) if parts else "all postings"


@dataclass(frozen=True, slots=True)
class SavedSearch:
    name: str
    payload: dict[str, Any]


def saved_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / "tui-searches.json"


def load_saved(path: Path | None = None) -> list[SavedSearch]:
    target = path or saved_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    found: list[SavedSearch] = []
    for entry in raw[:MAX_SAVED]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        payload = entry.get("payload")
        if isinstance(name, str) and isinstance(payload, dict):
            found.append(SavedSearch(name=name, payload=payload))
    return found


def store_saved(searches: list[SavedSearch], path: Path | None = None) -> bool:
    target = path or saved_path()
    body = [{"name": item.name, "payload": item.payload} for item in searches[:MAX_SAVED]]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


def remember(searches: list[SavedSearch], name: str, state: FilterState) -> list[SavedSearch]:
    kept = [item for item in searches if item.name != name]
    return [SavedSearch(name=name, payload=state.payload()), *kept][:MAX_SAVED]


__all__ = [
    "DEBOUNCE_SECONDS",
    "FILTER_FIELDS",
    "MAX_SAVED",
    "PAGE_SIZE",
    "FilterState",
    "SavedSearch",
    "describe",
    "load_saved",
    "remember",
    "replace",
    "saved_path",
    "store_saved",
]
