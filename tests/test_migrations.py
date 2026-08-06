import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.storage.migrations import (
    SchemaVersionError,
    applied_versions,
    latest_version,
    migrate,
    snapshot_path,
)
from stage.storage.sqlite_repo import SqliteRepository


def _snapshots(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.bak-*"))


def test_fresh_database_migrates_without_snapshotting(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        assert applied_versions(repository._conn) == tuple(range(1, latest_version() + 1))
    finally:
        repository.close()

    assert _snapshots(db_path) == []


def test_migrating_twice_is_a_no_op(db_path: Path) -> None:
    SqliteRepository.connect(db_path).close()
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert migrate(conn, db_path) == ()
    finally:
        conn.close()

    assert _snapshots(db_path) == []


def test_populated_database_is_snapshotted_before_a_new_migration(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import migrations

    SqliteRepository.connect(db_path).close()
    assert _snapshots(db_path) == []

    shipped = migrations.discover()
    next_version = shipped[-1].version + 1
    extra = tmp_path / f"{next_version:04d}_add_note.sql"
    extra.write_text("CREATE TABLE note (id INTEGER PRIMARY KEY);", encoding="utf-8")
    pending = (
        *shipped,
        migrations.Migration(version=next_version, name=extra.stem, path=extra),
    )
    monkeypatch.setattr(migrations, "discover", lambda: pending)

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        assert migrate(conn, db_path) == (next_version,)
        assert applied_versions(conn) == tuple(range(1, next_version + 1))
        conn.execute("SELECT id FROM note").fetchall()
    finally:
        conn.close()

    assert len(_snapshots(db_path)) == 1


def test_database_newer_than_this_build_is_refused(db_path: Path) -> None:
    SqliteRepository.connect(db_path).close()
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (9999, "from_the_future", datetime.now(UTC).isoformat()),
        )
        with pytest.raises(SchemaVersionError, match="newer than this build"):
            migrate(conn, db_path)
    finally:
        conn.close()


def test_snapshot_path_is_derived_from_the_database_name(db_path: Path) -> None:
    when = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    assert snapshot_path(db_path, when).name == f"{db_path.name}.bak-20260731T120000"
