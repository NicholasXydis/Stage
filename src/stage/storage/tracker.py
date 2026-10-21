import os
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from stage.domain.tracker import (
    ApplicationStatus,
    Snapshot,
    StatusChange,
    TrackedPosting,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    posting_id  TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    on_day      TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT NOT NULL,
    apply_url   TEXT NOT NULL,
    source      TEXT NOT NULL,
    term        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    posting_id  TEXT NOT NULL
                REFERENCES tracked (posting_id) ON DELETE CASCADE ON UPDATE CASCADE,
    status      TEXT NOT NULL,
    on_day      TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_by_posting ON history (posting_id, recorded_at);
"""


class TrackerUnreadableError(Exception):
    pass


class TrackerStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def connect(cls, path: Path) -> "TrackerStore":
        created = _create_private(path)
        try:
            conn = sqlite3.connect(path, timeout=5)
        except sqlite3.Error as exc:
            raise TrackerUnreadableError(str(exc)) from exc
        conn.row_factory = sqlite3.Row
        store = cls(conn)
        try:
            store._prepare(path, created)
        except BaseException:
            conn.close()
            raise
        return store

    def _prepare(self, path: Path, created: bool) -> None:
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise TrackerUnreadableError("it was written by a newer version of Stage")
            if version < SCHEMA_VERSION:
                with self._conn:
                    self._conn.executescript(_SCHEMA)
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise TrackerUnreadableError(str(exc)) from exc
        if created:
            from stage.paths import restrict_permissions

            restrict_permissions(path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def statuses(self) -> dict[str, tuple[ApplicationStatus, datetime]]:
        rows = self._read("SELECT posting_id, status, changed_at FROM tracked")
        return {
            str(row["posting_id"]): (_status(row["status"]), _moment(row["changed_at"]))
            for row in rows
        }

    def entries(self) -> list[TrackedPosting]:
        rows = self._read("SELECT * FROM tracked ORDER BY changed_at DESC, posting_id")
        history = self._history()
        return [_entry(row, history.get(str(row["posting_id"]), ())) for row in rows]

    def write(
        self,
        entries: Sequence[TrackedPosting],
        changes: Sequence[tuple[str, StatusChange]],
        renamed: Mapping[str, str],
    ) -> None:
        try:
            with self._conn:
                for old, new in renamed.items():
                    self._conn.execute(
                        "UPDATE tracked SET posting_id = ? WHERE posting_id = ?", (new, old)
                    )
                for entry in entries:
                    self._conn.execute(
                        "INSERT INTO tracked (posting_id, status, on_day, changed_at, note, "
                        "company, title, location, apply_url, source, term) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (posting_id) DO UPDATE SET status = excluded.status, "
                        "on_day = excluded.on_day, changed_at = excluded.changed_at, "
                        "note = excluded.note, company = excluded.company, "
                        "title = excluded.title, location = excluded.location, "
                        "apply_url = excluded.apply_url, source = excluded.source, "
                        "term = excluded.term",
                        _row(entry),
                    )
                self._conn.executemany(
                    "INSERT INTO history (posting_id, status, on_day, recorded_at) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            posting_id,
                            change.status.value,
                            change.on.isoformat(),
                            change.recorded_at.isoformat(),
                        )
                        for posting_id, change in changes
                    ],
                )
        except sqlite3.Error as exc:
            raise TrackerUnreadableError(str(exc)) from exc

    def remove(self, ids: Collection[str]) -> None:
        try:
            with self._conn:
                self._conn.executemany(
                    "DELETE FROM tracked WHERE posting_id = ?", [(key,) for key in sorted(set(ids))]
                )
        except sqlite3.Error as exc:
            raise TrackerUnreadableError(str(exc)) from exc

    def _history(self) -> dict[str, tuple[StatusChange, ...]]:
        rows = self._read(
            "SELECT posting_id, status, on_day, recorded_at FROM history "
            "ORDER BY recorded_at, rowid"
        )
        found: dict[str, list[StatusChange]] = {}
        for row in rows:
            found.setdefault(str(row["posting_id"]), []).append(
                StatusChange(
                    status=_status(row["status"]),
                    on=_day(row["on_day"]),
                    recorded_at=_moment(row["recorded_at"]),
                )
            )
        return {key: tuple(value) for key, value in found.items()}

    def _read(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise TrackerUnreadableError(str(exc)) from exc


def _create_private(path: Path) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise TrackerUnreadableError(exc.strerror or type(exc).__name__) from exc
    os.close(descriptor)
    return True


def _status(value: object) -> ApplicationStatus:
    try:
        return ApplicationStatus(str(value))
    except ValueError as exc:
        raise TrackerUnreadableError(
            f"it holds a status this Stage does not know: {value!r}"
        ) from exc


def _moment(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise TrackerUnreadableError(f"it holds a time Stage cannot read: {value!r}") from exc


def _day(value: object) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise TrackerUnreadableError(f"it holds a date Stage cannot read: {value!r}") from exc


def _entry(row: sqlite3.Row, history: tuple[StatusChange, ...]) -> TrackedPosting:
    return TrackedPosting(
        posting_id=str(row["posting_id"]),
        status=_status(row["status"]),
        on=_day(row["on_day"]),
        changed_at=_moment(row["changed_at"]),
        note=str(row["note"]),
        snapshot=Snapshot(
            company=str(row["company"]),
            title=str(row["title"]),
            location=str(row["location"]),
            apply_url=str(row["apply_url"]),
            source=str(row["source"]),
            term=str(row["term"]),
        ),
        history=history,
    )


def _row(entry: TrackedPosting) -> tuple[str, ...]:
    snapshot = entry.snapshot
    return (
        entry.posting_id,
        entry.status.value,
        entry.on.isoformat(),
        entry.changed_at.isoformat(),
        entry.note,
        snapshot.company,
        snapshot.title,
        snapshot.location,
        snapshot.apply_url,
        snapshot.source,
        snapshot.term,
    )


__all__ = ["SCHEMA_VERSION", "TrackerStore", "TrackerUnreadableError"]
