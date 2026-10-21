import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_REMEMBERED = 100_000
_RANGE = re.compile(r"^(\d+)-(\d+)$")


class StaleSelectionError(Exception):
    pass


class BadRangeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Selection:
    ids: tuple[str, ...]
    synced_at: str


def selection_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / "last-listing.json"


def remember(ids: tuple[str, ...], synced_at: datetime | None, path: Path | None = None) -> int:
    target = path or selection_path()
    stored = list(ids[:MAX_REMEMBERED])
    payload = {
        "ids": stored,
        "synced_at": synced_at.isoformat() if synced_at else "",
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return 0
    return len(stored)


def read(path: Path | None = None) -> Selection | None:
    target = path or selection_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    stored = payload.get("ids")
    if not isinstance(stored, list):
        return None
    return Selection(
        ids=tuple(str(value) for value in stored),
        synced_at=str(payload.get("synced_at", "")),
    )


def resolve(row: int, synced_at: datetime | None, path: Path | None = None) -> str:
    selection = read(path)
    if selection is None or not selection.ids:
        raise StaleSelectionError(
            "No listing to number against. Run stage list or stage search first."
        )
    current = synced_at.isoformat() if synced_at else ""
    if selection.synced_at != current:
        raise StaleSelectionError(
            "The database has changed since that listing. Run stage list again."
        )
    if row < 1 or row > len(selection.ids):
        raise StaleSelectionError(
            f"That listing had {len(selection.ids)} row(s), so {row} is out of range."
        )
    return selection.ids[row - 1]


def _looks_like_a_range(text: str) -> bool:
    return "-" in text and all(char == "-" or char.isdecimal() for char in text)


def _row(text: str) -> int:
    if len(text.lstrip("0")) > len(str(MAX_REMEMBERED)):
        raise BadRangeError(f"A listing holds at most {MAX_REMEMBERED:,} rows.")
    return int(text)


def expand(references: Sequence[str]) -> list[str]:
    expanded: dict[str, None] = {}
    for reference in references:
        found = _RANGE.match(reference)
        if found is None:
            if _looks_like_a_range(reference):
                raise BadRangeError(f"{reference!r} is not a range; write one like 1-5.")
            expanded.setdefault(str(_row(reference)) if reference.isdecimal() else reference)
            continue
        start, end = _row(found.group(1)), _row(found.group(2))
        if start > end:
            raise BadRangeError(f"{reference} counts backwards; write {end}-{start}.")
        if end > MAX_REMEMBERED:
            raise BadRangeError(f"A listing holds at most {MAX_REMEMBERED:,} rows.")
        for row in range(start, end + 1):
            expanded.setdefault(str(row))
    return list(expanded)


__all__ = [
    "MAX_REMEMBERED",
    "BadRangeError",
    "Selection",
    "StaleSelectionError",
    "expand",
    "read",
    "remember",
    "resolve",
    "selection_path",
]
