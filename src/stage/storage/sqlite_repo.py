import os
import sqlite3
from contextlib import suppress
from pathlib import Path, PurePath
from urllib.parse import quote

from stage.domain import board_of
from stage.domain.text import is_workday_opening
from stage.paths import create_private, discard_new, restrict_permissions, tighten_permissions
from stage.storage.batch import BatchWriter
from stage.storage.details import DetailFetches
from stage.storage.migrations import is_current, migrate, refuse_foreign_database
from stage.storage.postings import PostingReads
from stage.storage.quarantine import QuarantineStore
from stage.storage.upkeep import Upkeep
from stage.storage.visits import VisitRecords

BUSY_TIMEOUT_MS = 5000
ANALYSIS_LIMIT = 400
WAL_LIMIT_BYTES = 32 * 1024 * 1024


def _repost_key(company: str, title: str, place: str | None) -> str:
    from stage.dedup import repost_key

    return repost_key(company, title, place or "")


def _term_ended(term: str, moment: str | None) -> bool:
    from stage.normalize.terms import ended_before
    from stage.storage.sql import from_text

    return ended_before(term, from_text(moment))


def _term_fits(term: str, title: str | None) -> bool:
    from stage.normalize.terms import fits_title

    return fits_title(term, title or "")


def _other_opening(main: str | None, copy: str | None) -> bool:
    from stage.dedup.identity import opening_key

    left, right = opening_key(main or ""), opening_key(copy or "")
    return bool(left and right) and left != right


def _unstarted(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None


def read_only_uri(path: PurePath) -> str:
    text = path.as_posix()
    root = "" if text.startswith("/") else "/"
    return f"file://{root}{quote(text, safe='/:')}?mode=ro"


class SqliteRepository(
    PostingReads, QuarantineStore, BatchWriter, Upkeep, VisitRecords, DetailFetches
):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        connection.create_function("stage_board_of", 2, board_of, deterministic=True)
        connection.create_function(
            "stage_workday_opening", 1, is_workday_opening, deterministic=True
        )
        connection.create_function("stage_repost_key", 3, _repost_key, deterministic=True)
        connection.create_function("stage_term_ended", 2, _term_ended, deterministic=True)
        connection.create_function("stage_term_fits", 2, _term_fits, deterministic=True)
        connection.create_function("stage_other_opening", 2, _other_opening, deterministic=True)

    @classmethod
    def connect(cls, db_path: Path, *, read_only: bool = False) -> "SqliteRepository":
        if read_only and db_path.exists():
            reader = cls._reader(db_path)
            if reader is not None:
                return reader
        db_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = create_private(db_path)
        if is_new:
            try:
                restrict_permissions(db_path)
            except OSError:
                discard_new(db_path)
                raise
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not is_new:
                refuse_foreign_database(conn, db_path)
                if os.name == "nt" and _unstarted(conn):
                    restrict_permissions(db_path)
                tighten_permissions(
                    db_path,
                    db_path.with_name(f"{db_path.name}-wal"),
                    db_path.with_name(f"{db_path.name}-shm"),
                )
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA journal_size_limit = {WAL_LIMIT_BYTES}")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            migrate(conn, db_path)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    @classmethod
    def _reader(cls, db_path: Path) -> "SqliteRepository | None":
        conn = sqlite3.connect(read_only_uri(db_path.resolve()), uri=True, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            current = is_current(conn, db_path)
        except BaseException:
            conn.close()
            raise
        if not current:
            conn.close()
            return None
        return cls(conn)

    def close(self) -> None:
        with suppress(sqlite3.Error):
            if not self._conn.execute("PRAGMA query_only").fetchone()[0]:
                self._conn.execute("PRAGMA busy_timeout = 0")
                self._conn.execute(f"PRAGMA analysis_limit = {ANALYSIS_LIMIT}")
                self._conn.execute("PRAGMA optimize")
        self._conn.close()
