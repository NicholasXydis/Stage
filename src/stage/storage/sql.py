import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

EACH = "(SELECT value FROM json_each(?))"


def link_of(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"coalesce(nullif({prefix}apply_url_canonical, ''), {prefix}apply_url_raw)"


def listed[T: (str, int)](values: Iterable[T]) -> str:
    return json.dumps(sorted(set(values)))


def marks(count: int) -> str:
    return ", ".join("?" * count)


def starting(prefix: str) -> str:
    return f"{prefix}*"


def board_rows(board: str) -> str:
    return starting(f"{board}:")


@lru_cache(maxsize=4096)
def to_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Stage stores only times with a time zone.")
    return value.astimezone(UTC).isoformat()


def from_text(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def required_time(value: str | None, column: str) -> datetime:
    parsed = from_text(value)
    if parsed is None:
        raise ValueError(f"The database has a row with no {column}. Run stage doctor to check it.")
    return parsed


def limit_clause(limit: int | None, offset: int = 0) -> tuple[str, tuple[int, ...]]:
    if not offset:
        return ("", ()) if limit is None else (" LIMIT ?", (limit,))
    return " LIMIT ? OFFSET ?", (-1 if limit is None else limit, offset)


def fold_company(value: str) -> str:
    from stage.domain.text import plain_company
    from stage.lexicon import fold

    return fold(plain_company(value))


def count(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> int:
    return int(conn.execute(sql, tuple(params)).fetchone()[0])


class Store:
    _conn: sqlite3.Connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
