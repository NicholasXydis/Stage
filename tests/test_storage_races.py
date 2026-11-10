import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.dedup import DuplicateLink, resolve_duplicates
from stage.domain import Job, JobFilters, LocationBucket, QuarantinedJob, RejectionReason
from stage.storage import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _job(job_id: str, company: str = "Acme", title: str = "Software Engineer Intern") -> Job:
    return Job(
        id=job_id,
        source=job_id.split(":")[0],
        company=company,
        title_raw=title,
        title_normalized=title,
        apply_url_raw="",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location=LocationBucket.USA,
    )


def _rejected(job_id: str, title: str) -> QuarantinedJob:
    return QuarantinedJob(
        id=job_id,
        source=job_id.split(":")[0],
        company="Acme",
        title_raw=title,
        reason=RejectionReason.NOT_AN_INTERNSHIP,
        first_seen=NOW,
        last_seen=NOW,
    )


def _batch(*jobs: Job, quarantined: Sequence[QuarantinedJob] = ()) -> SourceBatch:
    return SourceBatch(
        source=jobs[0].source if jobs else quarantined[0].source,
        run_started_at=NOW,
        jobs=jobs,
        quarantined=tuple(quarantined),
        resolve_duplicates=resolve_duplicates,
    )


def _ids(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn, conn:
        return [str(row[0]) for row in conn.execute("SELECT id FROM jobs ORDER BY id")]


def _holding(path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_a_writer_waits_for_another_and_then_lands(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    holder = _holding(db_path)
    threading.Timer(0.2, holder.rollback).start()
    try:
        result = repository.apply_source_batch(_batch(_job("greenhouse:acme:1")))
    finally:
        repository.close()
        holder.close()

    assert result.added == 1 and _ids(db_path) == ["greenhouse:acme:1"]


def test_a_writer_held_past_the_wait_is_refused_as_busy_and_recovers(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import sqlite_repo
    from stage.storage.problems import database_problem

    monkeypatch.setattr(sqlite_repo, "BUSY_TIMEOUT_MS", 100)
    repository = SqliteRepository.connect(db_path)
    holder = _holding(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            repository.apply_source_batch(_batch(_job("greenhouse:acme:1")))
        holder.rollback()
        assert not repository._conn.in_transaction
        repository.apply_source_batch(_batch(_job("greenhouse:acme:1")))
    finally:
        repository.close()
        holder.close()

    assert "busy with another Stage process" in database_problem(raised.value, db_path)
    assert _ids(db_path) == ["greenhouse:acme:1"]


def test_a_sync_and_a_rescreen_writing_at_once_both_land(db_path: Path) -> None:
    SqliteRepository.connect(db_path).close()
    failures: list[BaseException] = []

    def write(source: str) -> None:
        repository = SqliteRepository.connect(db_path)
        try:
            for number in range(15):
                repository.apply_source_batch(
                    _batch(_job(f"{source}:acme:{number}"), _job(f"{source}:beta:{number}"))
                )
        except Exception as exc:
            failures.append(exc)
        finally:
            repository.close()

    writers = [threading.Thread(target=write, args=(name,)) for name in ("greenhouse", "lever")]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    assert not failures
    assert len(_ids(db_path)) == 60


def test_a_failed_commit_rolls_back_so_the_next_write_lands() -> None:
    from stage.storage.sql import Store

    store = Store()
    store._conn = sqlite3.connect(":memory:", isolation_level=None)
    store._conn.executescript(
        "PRAGMA foreign_keys = ON;"
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE child (id INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED);"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError), store._transaction() as conn:
            conn.execute("INSERT INTO child VALUES (1)")
        assert not store._conn.in_transaction
        with store._transaction() as conn:
            conn.execute("INSERT INTO parent VALUES (1)")
        assert store._conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
    finally:
        store._conn.close()


def test_an_interrupted_batch_leaves_no_transaction_open(db_path: Path) -> None:
    def interrupt(_: Sequence[Job], __: Sequence[Job]) -> Sequence[DuplicateLink]:
        raise KeyboardInterrupt

    repository = SqliteRepository.connect(db_path)
    try:
        with pytest.raises(KeyboardInterrupt):
            repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=NOW,
                    jobs=(_job("greenhouse:acme:1"),),
                    resolve_duplicates=interrupt,
                )
            )
        assert not repository._conn.in_transaction
        repository.apply_source_batch(_batch(_job("greenhouse:acme:2")))
    finally:
        repository.close()

    assert _ids(db_path) == ["greenhouse:acme:2"]


_KILLED_MID_BATCH = """
import os, sys
from datetime import UTC, datetime
from pathlib import Path
from stage.dedup import DuplicateLink, resolve_duplicates
from stage.domain import Job, LocationBucket
from stage.storage import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
def job(ident):
    return Job(id=ident, source="greenhouse", company="Acme", title_raw="Software Intern",
               title_normalized="Software Intern", apply_url_raw="", description="",
               first_seen=now, last_seen=now, location=LocationBucket.USA)
def die(*_):
    os._exit(9)
repository = SqliteRepository.connect(Path(sys.argv[1]))
repository.apply_source_batch(SourceBatch(source="greenhouse", run_started_at=now,
    jobs=(job("greenhouse:acme:1"),), resolve_duplicates=resolve_duplicates))
repository.apply_source_batch(SourceBatch(source="greenhouse", run_started_at=now,
    jobs=tuple(job(f"greenhouse:acme:{n}") for n in range(2, 400)), resolve_duplicates=die))
"""


def test_a_sync_killed_inside_a_batch_keeps_the_last_whole_commit(db_path: Path) -> None:
    killed = subprocess.run(
        [sys.executable, "-c", _KILLED_MID_BATCH, str(db_path)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    assert killed.returncode == 9, killed.stderr
    repository = SqliteRepository.connect(db_path)
    try:
        damage = repository.integrity_findings(whole_file=True)[0]
        assert repository.count_jobs(JobFilters(status=None, limit=None)) == 1
        repository.apply_source_batch(_batch(_job("greenhouse:acme:2")))
    finally:
        repository.close()

    assert damage.count == 0, damage.detail
    assert _ids(db_path) == ["greenhouse:acme:1", "greenhouse:acme:2"]


def test_the_journal_shrinks_back_after_a_large_batch(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import sqlite_repo

    monkeypatch.setattr(sqlite_repo, "WAL_LIMIT_BYTES", 256 * 1024)
    journal = db_path.with_name(f"{db_path.name}-wal")
    repository = SqliteRepository.connect(db_path)
    reader = sqlite3.connect(db_path, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM jobs").fetchone()
        rejected = [_rejected(f"greenhouse:acme:{n}", "Sales " + "x" * 900) for n in range(6000)]
        repository.apply_source_batch(_batch(quarantined=rejected))
        grown = journal.stat().st_size
        reader.execute("COMMIT")
        for number in range(3):
            repository.apply_source_batch(_batch(_job(f"greenhouse:acme:job{number}")))
        settled = journal.stat().st_size
    finally:
        reader.close()
        repository.close()

    assert grown > 4 * 1024 * 1024
    assert settled <= 256 * 1024, f"the journal stayed at {settled} bytes"


def test_a_full_disk_rolls_the_batch_back_and_the_next_one_lands(db_path: Path) -> None:
    from stage.storage.problems import database_problem

    repository = SqliteRepository.connect(db_path)
    try:
        pages = int(repository._conn.execute("PRAGMA page_count").fetchone()[0])
        repository._conn.execute(f"PRAGMA max_page_count = {pages + 1}")
        rejected = [_rejected(f"greenhouse:acme:{n}", "Sales " + "x" * 900) for n in range(500)]
        with pytest.raises(sqlite3.OperationalError) as raised:
            repository.apply_source_batch(_batch(_job("greenhouse:acme:job"), quarantined=rejected))
        assert not repository._conn.in_transaction
        assert repository.count_jobs(JobFilters(status=None, limit=None)) == 0
        repository._conn.execute("PRAGMA max_page_count = 1073741823")
        repository.apply_source_batch(_batch(_job("greenhouse:acme:job"), quarantined=rejected))
    finally:
        repository.close()

    assert "is full" in database_problem(raised.value, db_path)
    assert _ids(db_path) == ["greenhouse:acme:job"]


def test_an_old_sqlite_variable_limit_still_takes_big_batches(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        repository._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        jobs = tuple(_job(f"greenhouse:board{n}:1", company=f"Employer {n}") for n in range(1200))
        assert repository.apply_source_batch(_batch(*jobs)).added == 1200
        boards = [f"greenhouse:board{n}" for n in range(1500)]
        assert repository.close_orphan_boards(["greenhouse"], boards) == 0
        assert repository.close_orphan_boards(["greenhouse"], boards[1:]) == 1
    finally:
        repository.close()


def test_a_corrupt_same_board_link_does_not_keep_a_stale_link_alive(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        repository.apply_source_batch(
            _batch(
                _job("workday:zeta:1", "Zeta", "Data Intern"),
                _job("workday:zeta:2", "Zeta", "Design Intern"),
            )
        )
        repository.apply_source_batch(_batch(_job("lever:zeta:7", "Zeta", "Sales Intern")))
        repository.apply_source_batch(_batch(_job("greenhouse:zeta:2", "Zeta", "Software Intern")))
        repository._conn.execute(
            "UPDATE jobs SET duplicate_of = CASE id WHEN 'workday:zeta:1' THEN 'workday:zeta:2' "
            "ELSE 'lever:zeta:7' END WHERE id IN ('workday:zeta:1', 'greenhouse:zeta:2')"
        )
        repository.apply_source_batch(_batch(_job("greenhouse:zeta:2", "Zeta", "Software Intern")))
        kept = repository.get_job("greenhouse:zeta:2")
    finally:
        repository.close()

    assert kept is not None and kept.duplicate_of is None, "a stale link hid the posting"


def test_a_tracker_save_waits_for_another_writer_then_lands(tmp_path: Path) -> None:
    from stage.domain.tracker import ApplicationStatus, Snapshot, TrackedPosting
    from stage.storage.tracker import TrackerStore

    path = tmp_path / "tracker.db"
    entry = TrackedPosting(
        posting_id="greenhouse:acme:1",
        status=ApplicationStatus.SAVED,
        on=NOW.date(),
        changed_at=NOW,
        note="",
        snapshot=Snapshot("Acme", "Intern", "Toronto", "https://acme.example", "greenhouse", ""),
    )
    with TrackerStore.connect(path) as store:
        holder = _holding(path)
        threading.Timer(0.2, holder.rollback).start()
        try:
            store.write([entry], [], {})
        finally:
            holder.close()
        assert [found.posting_id for found in store.entries()] == ["greenhouse:acme:1"]


def test_a_tracker_held_past_the_wait_is_reported_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import tracker
    from stage.storage.tracker import TrackerStore, TrackerUnreadableError

    monkeypatch.setattr(tracker, "WAIT_S", 0.1)
    path = tmp_path / "tracker.db"
    TrackerStore.connect(path).close()
    holder = _holding(path)
    try:
        with TrackerStore.connect(path) as store, pytest.raises(TrackerUnreadableError) as raised:
            store.hold()
    finally:
        holder.close()

    assert str(raised.value) == "another Stage process is saving to it"
