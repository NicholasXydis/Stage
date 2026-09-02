from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from stage.domain import (
    DEFAULT_WINDOW_DAYS,
    JobFilters,
    Language,
    LocationBucket,
    RoleCategory,
)

DEBOUNCE_SECONDS = 0.04
PAGE_SIZE = 200

LAST_DAYS_CHOICES: tuple[int, ...] = (7, 14, 30, 0)

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

_OPEN_FIELDS: tuple[tuple[str, str], ...] = (("company", "Company"),)


def filter_rows(
    state: "FilterState", employer: str | None = None
) -> tuple[tuple[str, str, str, bool], ...]:
    rows: list[tuple[str, str, str, bool]] = []
    for name, label, options in FILTER_FIELDS:
        for option in options:
            if option == "unknown":
                continue
            rows.append((name, option, f"{label}: {option}", state.values.get(name) == option))
    window = state.last_days
    for days in LAST_DAYS_CHOICES:
        shown = "any date" if days == 0 else f"last {days} days"
        rows.append(("last_days", str(days), f"Window: {shown}", window == days))
    rows.append(("only_new", "on", "Only what the last sync brought in", state.only_new))
    rows.append(("show_all", "on", "Show every match, not just a page", state.show_all))
    chosen = state.values.get("company")
    if chosen is not None:
        rows.append(("company", chosen, f"Employer: {chosen}", True))
    elif employer is not None:
        rows.append(("company", employer, f"Employer: {employer}", False))
    return tuple(rows)


@dataclass
class FilterState:
    query: str = ""
    limit: int = PAGE_SIZE
    values: dict[str, str] = field(default_factory=dict)
    last_days: int = DEFAULT_WINDOW_DAYS
    only_new: bool = False
    show_all: bool = False

    def toggle(self, name: str, value: str) -> None:
        if self.values.get(name) == value:
            self.values.pop(name, None)
            return
        self.values[name] = value

    def clear(self) -> None:
        self.values.clear()
        self.query = ""
        self.limit = PAGE_SIZE
        self.last_days = DEFAULT_WINDOW_DAYS
        self.only_new = False
        self.show_all = False

    def widen(self) -> None:
        self.limit += PAGE_SIZE

    def narrow(self) -> bool:
        if self.limit <= PAGE_SIZE:
            return False
        self.limit = max(PAGE_SIZE, self.limit - PAGE_SIZE)
        return True

    def choose(self, name: str, value: str) -> None:
        if name == "last_days":
            self.last_days = int(value)
        elif name == "only_new":
            self.only_new = not self.only_new
        elif name == "show_all":
            self.show_all = not self.show_all
        else:
            self.toggle(name, value)
        self.limit = PAGE_SIZE

    @property
    def window_days(self) -> int | None:
        return self.last_days or None

    @property
    def active(self) -> tuple[tuple[str, str], ...]:
        named = tuple(name for name, _, _ in FILTER_FIELDS)
        ordered = (*named, *(name for name, _ in _OPEN_FIELDS))
        return tuple((name, self.values[name]) for name in ordered if name in self.values)

    def as_filters(self) -> JobFilters:
        chosen: dict[str, Any] = {}
        for name, value in self.values.items():
            enum = _ENUMS.get(name)
            if enum is None:
                if any(name == field_name for field_name, _ in _OPEN_FIELDS):
                    chosen[name] = value
                continue
            try:
                chosen[name] = enum(value)
            except ValueError:
                continue
        return JobFilters(limit=None if self.show_all else self.limit, **chosen)


def theme_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / "tui-theme"


def load_theme(path: Path | None = None) -> str | None:
    target = path or theme_path()
    try:
        name = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def store_theme(name: str, path: Path | None = None) -> bool:
    target = path or theme_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(name, encoding="utf-8")
    except OSError:
        return False
    return True


__all__ = [
    "DEBOUNCE_SECONDS",
    "FILTER_FIELDS",
    "LAST_DAYS_CHOICES",
    "PAGE_SIZE",
    "FilterState",
    "filter_rows",
    "load_theme",
    "replace",
    "store_theme",
    "theme_path",
]
