from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import (
    DegreeRequirement,
    Job,
    JobFilters,
    Language,
    LocationBucket,
    QuarantinedJob,
    RejectionReason,
    RoleCategory,
)
from stage.storage.repository import SourceBatch
from stage.storage.search import match_expression, search_terms
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(
    identifier: str,
    company: str,
    title: str,
    *,
    source: str = "greenhouse",
    location: str = "Montréal, QC",
    bucket: LocationBucket = LocationBucket.MONTREAL,
    description: str = "",
    duplicate_of: str | None = None,
) -> Job:
    return Job(
        id=identifier,
        source=source,
        company=company,
        title_raw=title,
        title_normalized=title.lower(),
        title_canonical=title.lower(),
        apply_url_raw=f"https://boards.example.test/{identifier}",
        description=description,
        first_seen=NOW,
        last_seen=NOW,
        location_raw=location,
        location=bucket,
        language=Language.FR,
        role=RoleCategory.SWE,
        term="summer-2027",
        degree_requirement=DegreeRequirement.UNKNOWN,
        duplicate_of=duplicate_of,
    )


@pytest.fixture
def repository(db_path: Path) -> Iterator[SqliteRepository]:
    repo = SqliteRepository.connect(db_path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job(
                    "greenhouse:coveo:1",
                    "Coveo Solutions",
                    "Stagiaire en développement logiciel",
                    description="Poste basé à Montréal, équipe cœur.",
                ),
                _job(
                    "greenhouse:coveo:2",
                    "Coveo Solutions",
                    "Quantitative Research Intern",
                    location="New York, NY",
                    bucket=LocationBucket.USA,
                ),
            ),
        )
    )
    yield repo
    repo.close()


def _ids(jobs: list[Job]) -> set[str]:
    return {job.id for job in jobs}


def test_a_folded_query_finds_an_accented_title(repository: SqliteRepository) -> None:
    found = repository.search_jobs("developpement", JobFilters())
    assert _ids(found) == {"greenhouse:coveo:1"}


def test_an_accented_query_finds_the_same_row(repository: SqliteRepository) -> None:
    assert _ids(repository.search_jobs("Développement", JobFilters())) == {"greenhouse:coveo:1"}
    assert _ids(repository.search_jobs("MONTRÉAL", JobFilters())) == {"greenhouse:coveo:1"}


def test_a_term_matches_as_a_prefix(repository: SqliteRepository) -> None:
    assert _ids(repository.search_jobs("quant", JobFilters())) == {"greenhouse:coveo:2"}


def test_terms_are_combined_with_and(repository: SqliteRepository) -> None:
    assert repository.search_jobs("quantitative logiciel", JobFilters()) == []
    assert len(repository.search_jobs("intern research", JobFilters())) == 1


def test_a_company_filter_survives_the_fts_join(repository: SqliteRepository) -> None:
    matching = repository.search_jobs("intern", JobFilters(company="Coveo Solutions"))
    assert _ids(matching) == {"greenhouse:coveo:2"}
    assert repository.search_jobs("intern", JobFilters(company="Someone Else")) == []
    assert repository.count_search("intern", JobFilters(company="Coveo Solutions")) == 1


def test_a_location_filter_narrows_the_match(repository: SqliteRepository) -> None:
    assert repository.search_jobs("intern", JobFilters(location=LocationBucket.MONTREAL)) == []
    assert _ids(repository.search_jobs("intern", JobFilters(location=LocationBucket.USA))) == {
        "greenhouse:coveo:2"
    }


def test_fts_operators_in_user_input_are_matched_literally(
    repository: SqliteRepository,
) -> None:
    quant = {"greenhouse:coveo:2"}
    cases: tuple[tuple[str, set[str]], ...] = (
        ('quant" OR "logiciel', set()),
        ("quant NEAR logiciel", set()),
        ("quant*", quant),
        ("-quant", quant),
        ("^quant", quant),
        ("quant AND logiciel", set()),
    )
    for hostile, expected in cases:
        assert _ids(repository.search_jobs(hostile, JobFilters())) == expected, hostile


def test_legacy_location_values_remain_filterable_and_counted(repository: SqliteRepository) -> None:
    repository._conn.execute(
        "UPDATE jobs SET location = CASE id "
        "WHEN ? THEN 'other' WHEN ? THEN 'remote' END "
        "WHERE id IN (?, ?)",
        (
            "greenhouse:coveo:1",
            "greenhouse:coveo:2",
            "greenhouse:coveo:1",
            "greenhouse:coveo:2",
        ),
    )
    assert _ids(repository.list_jobs(JobFilters(location=LocationBucket.INTERNATIONAL))) == {
        "greenhouse:coveo:1"
    }
    assert _ids(repository.list_jobs(JobFilters(location=LocationBucket.UNKNOWN))) == {
        "greenhouse:coveo:2"
    }
    assert repository.composition("location") == {"international": 1, "unknown": 1}


def test_a_query_with_no_searchable_word_returns_nothing(repository: SqliteRepository) -> None:
    assert search_terms("!!! -- ***") == ()
    assert match_expression(()) == ""
    assert repository.search_jobs("!!!", JobFilters()) == []
    assert repository.count_search("!!!", JobFilters()) == 0


def test_the_index_follows_an_update(repository: SqliteRepository) -> None:
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(_job("greenhouse:coveo:1", "Coveo Solutions", "Cloud Platform Intern"),),
        )
    )
    assert repository.search_jobs("developpement", JobFilters()) == []
    assert _ids(repository.search_jobs("cloud", JobFilters())) == {"greenhouse:coveo:1"}


def test_a_quarantined_posting_leaves_the_index(repository: SqliteRepository) -> None:
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            quarantined=(
                QuarantinedJob(
                    id="greenhouse:coveo:2",
                    source="greenhouse",
                    company="Coveo Solutions",
                    title_raw="Quantitative Research Intern",
                    reason=RejectionReason.NOT_AN_INTERNSHIP,
                    first_seen=NOW,
                    last_seen=NOW,
                ),
            ),
        )
    )
    assert repository.search_jobs("quantitative", JobFilters()) == []


def test_a_duplicate_never_surfaces_in_search(db_path: Path) -> None:
    repo = SqliteRepository.connect(db_path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job("greenhouse:coveo:9", "Coveo Solutions", "Compiler Intern"),
                _job(
                    "simplify:feed:9",
                    "Coveo Solutions",
                    "Compiler Intern",
                    source="simplify",
                    duplicate_of="greenhouse:coveo:9",
                ),
            ),
        )
    )
    assert _ids(repo.search_jobs("compiler", JobFilters())) == {"greenhouse:coveo:9"}
    repo.close()


def test_the_index_is_consistent_with_the_table_after_a_purge(
    repository: SqliteRepository,
) -> None:
    repository.purge(NOW.replace(year=2027))
    repository._conn.execute("INSERT INTO jobs_fts (jobs_fts) VALUES ('integrity-check')")
    indexed = repository._conn.execute(
        "SELECT COUNT(*) AS total FROM jobs_fts WHERE jobs_fts MATCH '\"intern\"*'"
    ).fetchone()["total"]
    assert repository.search_jobs("intern", JobFilters(status=None)) == []
    assert indexed == 0


def test_search_is_reachable_from_the_command_line(db_path: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    repo = SqliteRepository.connect(db_path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(_job("greenhouse:coveo:1", "Coveo Solutions", "Stagiaire Développement"),),
        )
    )
    repo.close()

    runner = CliRunner()
    found = runner.invoke(app, ["search", "developpement", "--db", str(db_path)])
    assert found.exit_code == 0, found.stdout
    assert "Coveo Solutions" in found.stdout

    missing = runner.invoke(app, ["search", "kubernetes", "--db", str(db_path)])
    assert missing.exit_code == 0, missing.stdout
    assert "No posting matches" in missing.stdout

    unsearchable = runner.invoke(app, ["search", "!!!", "--db", str(db_path)])
    assert unsearchable.exit_code == 0, unsearchable.stdout
    assert "Nothing searchable" in unsearchable.stdout


@pytest.mark.serial
def test_a_pathological_query_is_bounded_rather_than_slow(repository: SqliteRepository) -> None:
    import time

    from stage.storage.search import MAX_TERM_LENGTH, MAX_TERMS

    assert search_terms("z" * 200) == ("z" * MAX_TERM_LENGTH,)
    assert len(search_terms(" ".join(f"w{index}" for index in range(2000)))) == MAX_TERMS

    started = time.perf_counter()
    repository.search_jobs("y" * 65536, JobFilters())
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"a 65,536 char query took {elapsed:.1f}s on a cold fold"


def test_a_capped_query_still_matches_what_the_user_typed(
    repository: SqliteRepository,
) -> None:
    assert search_terms("QUANTITATIVE") == ("quantitative",)
    assert _ids(repository.search_jobs("quantitative", JobFilters())) == {"greenhouse:coveo:2"}
