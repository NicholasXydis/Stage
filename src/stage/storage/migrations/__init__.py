import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_MIGRATIONS_DIR = Path(__file__).resolve().parent


class SchemaVersionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover() -> tuple[Migration, ...]:
    found: list[Migration] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise SchemaVersionError(f"migration {path.name!r} does not follow NNNN_name.sql")
        found.append(Migration(version=int(match.group(1)), name=path.stem, path=path))
    versions = [migration.version for migration in found]
    if len(set(versions)) != len(versions):
        raise SchemaVersionError("duplicate migration version numbers")
    return tuple(found)


def latest_version() -> int:
    migrations = discover()
    return migrations[-1].version if migrations else 0


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def applied_versions(conn: sqlite3.Connection) -> tuple[int, ...]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return tuple(int(row[0]) for row in rows)


BASELINE_COLUMNS = frozenset({"company_fold", "employment_type", "source_category"})
BASELINE_TABLES = frozenset({"coverage_classifications", "workday_crawls", "workday_crawl_seen"})


def _refuse_a_pre_baseline_database(conn: sqlite3.Connection, db_path: Path) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing = sorted(BASELINE_COLUMNS - columns) + sorted(BASELINE_TABLES - tables)
    if not missing:
        return
    raise SchemaVersionError(
        f"The database at {db_path} records the current schema version but is missing "
        f"{', '.join(missing)}, so it predates this baseline. Back it up or delete it, then "
        "run stage sync to collect postings again"
    )


def _newer(db_path: Path, version: int) -> SchemaVersionError:
    return SchemaVersionError(
        f"The database at {db_path} has schema version {version}, newer than this build "
        f"understands ({latest_version()}); upgrade stage-cli"
    )


def is_current(conn: sqlite3.Connection, db_path: Path) -> bool:
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise
    applied = {int(row[0]) for row in rows}
    known = {migration.version for migration in discover()}
    if applied - known:
        raise _newer(db_path, max(applied - known))
    if known - applied:
        return False
    _refuse_a_pre_baseline_database(conn, db_path)
    return True


def snapshot_path(db_path: Path, when: datetime) -> Path:
    return db_path.with_name(f"{db_path.name}.bak-{when.strftime('%Y%m%dT%H%M%S')}")


def _snapshot(conn: sqlite3.Connection, db_path: Path) -> Path:
    target = snapshot_path(db_path, datetime.now(UTC))
    backup = sqlite3.connect(target)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    return target


def refuse_foreign_database(conn: sqlite3.Connection, db_path: Path) -> None:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if tables and not tables & {"schema_migrations", "jobs"}:
        raise SchemaVersionError(
            f"{db_path} is not a Stage database, so Stage left it alone. "
            "Point --db or STAGE_DB at another file."
        )


def migrate(conn: sqlite3.Connection, db_path: Path) -> tuple[int, ...]:
    migrations = discover()
    known = {migration.version for migration in migrations}
    applied = applied_versions(conn)

    unknown = [version for version in applied if version not in known]
    if unknown:
        raise _newer(db_path, max(unknown))

    pending = [migration for migration in migrations if migration.version not in applied]
    if not pending:
        _refuse_a_pre_baseline_database(conn, db_path)
        return ()

    if applied:
        _snapshot(conn, db_path)

    for migration in pending:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{migration.read()}")
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
        except Exception:
            conn.rollback()
            if migration.version in applied_versions(conn):
                continue
            raise
        conn.commit()

    return tuple(migration.version for migration in pending)


__all__ = [
    "BASELINE_COLUMNS",
    "BASELINE_TABLES",
    "Migration",
    "SchemaVersionError",
    "refuse_foreign_database",
    "applied_versions",
    "discover",
    "is_current",
    "latest_version",
    "migrate",
    "snapshot_path",
]
