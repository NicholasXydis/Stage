from pathlib import Path

from stage.domain import JobFilters
from stage.storage.migrations import applied_versions, discover, latest_version
from stage.storage.sqlite_repo import SqliteRepository

BASELINE = 1

TABLES = frozenset(
    {
        "jobs",
        "quarantine",
        "tombstones",
        "http_cache",
        "rate_state",
        "source_visits",
        "workday_facets",
        "detail_fetches",
        "sync_runs",
        "sync_run_sources",
    }
)

TRIGGERS = frozenset({"jobs_fts_after_insert", "jobs_fts_after_update", "jobs_fts_after_delete"})


def test_one_migration_ships_and_it_is_the_baseline() -> None:
    migrations = discover()
    assert [migration.version for migration in migrations] == [BASELINE], (
        "the 14 historical migrations were collapsed into one baseline; the next schema "
        "change starts at 0002"
    )
    assert latest_version() == BASELINE


def test_the_baseline_declares_the_schema_directly_and_never_alters_it() -> None:
    body = discover()[0].read().upper()
    for statement in ("ALTER TABLE", "DROP TABLE", "DROP INDEX", "RENAME TO"):
        assert statement not in body, (
            f"the baseline still carries {statement!r}; a consolidated schema is declared, "
            "not rebuilt through its own history"
        )


def test_a_fresh_database_is_at_the_baseline_with_every_object_present(
    db_path: Path,
) -> None:
    repository = SqliteRepository.connect(db_path)
    try:
        assert applied_versions(repository._conn) == (BASELINE,)
        names = {
            row[0]
            for row in repository._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in repository._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        repository.close()

    assert names >= TABLES, f"missing from the baseline: {sorted(TABLES - names)}"
    assert "jobs_fts" in names, "search must exist from the first run, not from an upgrade"
    assert triggers == TRIGGERS


def test_search_works_on_a_fresh_database_with_no_backfill_step(db_path: Path) -> None:
    from datetime import UTC, datetime

    from stage.domain import Job
    from stage.storage.repository import SourceBatch

    when = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    job = Job(
        id="greenhouse:coveo:1",
        source="greenhouse",
        company="Coveo Solutions",
        title_raw="Stagiaire en développement logiciel",
        title_normalized="stagiaire en developpement logiciel",
        apply_url_raw="https://boards.example.test/1",
        description="Poste basé à Montréal.",
        location_raw="Montréal, QC",
        first_seen=when,
        last_seen=when,
    )
    repository = SqliteRepository.connect(db_path)
    try:
        repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=when, jobs=(job,))
        )
        assert [row.id for row in repository.search_jobs("developpement", JobFilters())] == [job.id]
        assert repository.count_search("montreal", JobFilters()) == 1
        repository._conn.execute("INSERT INTO jobs_fts (jobs_fts) VALUES ('integrity-check')")
    finally:
        repository.close()
