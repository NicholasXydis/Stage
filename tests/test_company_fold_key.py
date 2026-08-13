import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.dedup import MatchKind, resolve_duplicates, would_merge
from stage.domain import Job, Language, LocationBucket, RoleCategory
from stage.lexicon import fold
from stage.storage.migrations import SchemaVersionError
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

WHEN = datetime(2026, 8, 10, tzinfo=UTC)

VARIANTS = [
    ("Vidéotron", "Videotron"),
    ("Coveo", "coveo"),
    ("Pratt & Whitney", "Pratt and Whitney"),
    ("Développeur SA", "Developpeur SA"),
]


def job(job_id: str, source: str, company: str, title: str) -> Job:
    return Job(
        id=job_id,
        source=source,
        company=company,
        title_raw=title,
        title_normalized=title.lower(),
        description="",
        apply_url_raw="",
        apply_url_canonical="",
        location_raw="Montréal, QC",
        location=LocationBucket.MONTREAL,
        first_seen=WHEN,
        last_seen=WHEN,
        language=Language.EN,
        term="summer-2027",
        role=RoleCategory.SWE,
    )


def _land(repository: SqliteRepository, source: str, entry: Job) -> None:
    repository.apply_source_batch(
        SourceBatch(
            source=source,
            run_started_at=WHEN,
            jobs=(entry,),
            resolve_duplicates=resolve_duplicates,
        )
    )


@pytest.mark.parametrize(("left_name", "right_name"), VARIANTS)
def test_a_company_spelled_differently_still_reaches_the_matcher(
    db_path: Path, left_name: str, right_name: str
) -> None:
    left = job("greenhouse:board:1", "greenhouse", left_name, "Software Engineer Intern")
    right = job("simplify:feed:2", "simplify", right_name, "Software Engineer Intern")
    assert would_merge(left, right).kind is MatchKind.SAME_LANGUAGE, (
        "the fixture must merge before the repository is asked"
    )

    repository = SqliteRepository.connect(db_path)
    try:
        _land(repository, "greenhouse", left)
        _land(repository, "simplify", right)
        assert repository.count_duplicates() == 1, (
            f"{left_name!r} and {right_name!r} fold alike but never became candidates"
        )
    finally:
        repository.close()


def test_the_stored_fold_is_the_lexicon_primitive_and_follows_a_renamed_employer(
    db_path: Path,
) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        _land(repository, "greenhouse", job("greenhouse:b:1", "greenhouse", "Coveo (FR)", "SWE"))
        stored = repository._conn.execute("SELECT company_fold FROM jobs").fetchone()
        assert stored["company_fold"] == fold("Coveo (FR)")

        _land(
            repository,
            "greenhouse",
            job("greenhouse:b:1", "greenhouse", "Coveo Solutions", "SWE"),
        )
        renamed = repository._conn.execute("SELECT company_fold FROM jobs").fetchone()
        assert renamed["company_fold"] == fold("Coveo Solutions"), (
            "the upsert must refresh company_fold with company"
        )
    finally:
        repository.close()


def test_a_database_built_before_the_baseline_is_refused_by_name(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        _land(repository, "greenhouse", job("greenhouse:v:1", "greenhouse", "Vidéotron", "SWE"))
    finally:
        repository.close()

    rewound = sqlite3.connect(db_path)
    try:
        rewound.execute("DROP INDEX idx_jobs_company_fold")
        rewound.execute("ALTER TABLE jobs DROP COLUMN company_fold")
        rewound.commit()
    finally:
        rewound.close()

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(database: Path, isolation_level: None = None) -> sqlite3.Connection:
        conn = real_connect(database, isolation_level=isolation_level)
        opened.append(conn)
        return conn

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", spy)
        with pytest.raises(SchemaVersionError) as raised:
            SqliteRepository.connect(db_path)

    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    message = str(raised.value)
    assert "company_fold" in message
    assert "stage sync" in message, (
        "a missing column must be refused by name, not crash the next write"
    )


def test_a_database_missing_a_baseline_table_is_refused_by_name(db_path: Path) -> None:
    SqliteRepository.connect(db_path).close()
    rewound = sqlite3.connect(db_path)
    try:
        rewound.execute("DROP TABLE coverage_classifications")
        rewound.commit()
    finally:
        rewound.close()

    with pytest.raises(SchemaVersionError, match="coverage_classifications"):
        SqliteRepository.connect(db_path)
