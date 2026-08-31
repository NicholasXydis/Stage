import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_REMEMBERED = 100_000


class StaleSelectionError(Exception):
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


__all__ = [
    "MAX_REMEMBERED",
    "Selection",
    "StaleSelectionError",
    "read",
    "remember",
    "resolve",
    "selection_path",
]
