import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.domain import Job, JobFilters
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _job(job_id: str, company: str) -> Job:
    return Job(
        id=job_id,
        source="greenhouse",
        company=company,
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw="",
        description="",
        first_seen=NOW,
        last_seen=NOW,
    )


@pytest.mark.parametrize(
    ("said", "words"),
    [
        ("database is locked", "busy with another Stage process"),
        ("database or disk is full", "is full"),
        ("attempt to write a readonly database", "cannot write to"),
        ("unable to open database file", "cannot write to"),
        ("file is not a database", "damaged or is not a Stage database"),
        ("database disk image is malformed", "damaged or is not a Stage database"),
    ],
)
def test_each_database_problem_is_worded_once(said: str, words: str) -> None:
    from stage.storage.problems import database_problem

    path = Path("/data/stage.db")
    shown = database_problem(sqlite3.OperationalError(said), path)

    assert words in shown and str(path) in shown
    assert ";" not in shown and "Error" not in shown and said not in shown


def test_a_missing_and_a_newer_database_are_worded_with_what_to_do() -> None:
    from stage.storage.problems import newer_database, no_database

    missing = Path("/data/stage.db")
    assert f"stage sync --db {missing}" in no_database(missing)
    assert "uv tool upgrade stage-cli" in newer_database(Path("/data/stage.db"))


@pytest.mark.parametrize(
    ("company", "note", "url", "words"),
    [
        ("!!!", "checked", None, '"!!!" is not an employer name'),
        ("Acme", "  ", None, "--note"),
        ("Acme", "checked", "http://acme.example/jobs", "--url"),
        ("Acme", "checked", "https://user:secret@acme.example", "without a user name"),
    ],
)
def test_a_bad_coverage_classification_names_what_to_fix(
    db_path: Path, company: str, note: str, url: str | None, words: str
) -> None:
    from stage.domain import CoverageClassification, CoverageDisposition

    entry = CoverageClassification(
        company=company,
        disposition=CoverageDisposition.FEED_ONLY,
        note=note,
        checked_on=NOW - timedelta(days=1),
        url=url,
    )
    repository = SqliteRepository.connect(db_path)
    try:
        with pytest.raises(ValueError, match=words) as raised:
            repository.record_coverage_classification(entry)
        with pytest.raises(ValueError, match="not an employer name"):
            repository.clear_coverage_classification("---")
    finally:
        repository.close()

    assert ";" not in str(raised.value)


def test_a_time_without_a_zone_is_refused_plainly() -> None:
    from stage.storage.sql import to_text

    with pytest.raises(ValueError, match="time zone"):
        to_text(NOW.replace(tzinfo=None))


def test_an_unknown_composition_column_lists_the_known_ones(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        with pytest.raises(ValueError, match='"salary" is not a column') as raised:
            repository.composition("salary")
    finally:
        repository.close()

    assert "location" in str(raised.value) and ";" not in str(raised.value)


def test_a_company_with_no_letters_matches_its_exact_name_only(db_path: Path) -> None:
    from stage.storage import SourceBatch

    repository = SqliteRepository.connect(db_path)
    try:
        repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(_job("greenhouse:a:1", "Acme"), _job("greenhouse:b:1", "+++")),
            )
        )
        found = repository.list_jobs(JobFilters(company="+++", limit=None))
    finally:
        repository.close()

    assert [job.id for job in found] == ["greenhouse:b:1"]


def test_a_row_with_no_time_is_named_with_what_to_run(db_path: Path) -> None:
    from stage.storage import SourceBatch

    repository = SqliteRepository.connect(db_path)
    try:
        repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=NOW, jobs=(_job("greenhouse:a:1", "Acme"),)
            )
        )
        repository._conn.execute("UPDATE jobs SET last_seen = ''")
        with pytest.raises(ValueError, match="no last_seen. Run stage doctor"):
            repository.get_job("greenhouse:a:1")
    finally:
        repository.close()
