from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.domain import (
    STALE_AFTER_DAYS,
    Company,
    CompanyVisit,
    CoverageClassification,
    CoverageDisposition,
    CoverageState,
    Job,
    LocationBucket,
    Platform,
)
from stage.services.coverage import coverage
from stage.storage import open_repository
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(identifier: str, company: str, *, source: str = "greenhouse") -> Job:
    return Job(
        id=identifier,
        source=source,
        company=company,
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw=f"https://boards.example.test/{identifier}",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montréal, QC",
        location=LocationBucket.MONTREAL,
    )


def _states(rows: object) -> dict[str, CoverageState]:
    assert isinstance(rows, tuple)
    return {row.company: row.state for row in rows}


@pytest.fixture
def seeded(db_path: Path) -> Path:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job("greenhouse:producing:1", "Producing"),
                _job("simplify:feed:1", "Tesla", source="simplify"),
                _job("simplify:feed:2", "Coveo Solutions Inc.", source="simplify"),
            ),
            visits=(
                CompanyVisit(board="greenhouse:producing", succeeded=True, label="producing"),
                CompanyVisit(board="greenhouse:empty", succeeded=True, label="empty"),
                CompanyVisit(
                    board="greenhouse:broken", succeeded=False, error="404", label="broken"
                ),
            ),
        )
    )
    repository.close()
    return db_path


COMPANIES = (
    Company(name="Producing", platform=Platform.GREENHOUSE, slug="producing"),
    Company(name="Empty", platform=Platform.GREENHOUSE, slug="empty"),
    Company(name="Broken", platform=Platform.GREENHOUSE, slug="broken"),
    Company(name="Unvisited", platform=Platform.GREENHOUSE, slug="unvisited"),
    Company(name="Jobvite Only", platform=Platform.JOBVITE, slug="jobvite-only"),
    Company(name="Switched Off", platform=Platform.GREENHOUSE, slug="off", enabled=False),
)


@pytest.mark.asyncio
async def test_each_reason_for_zero_postings_is_a_separate_state(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        report = await coverage(repository, COMPANIES, now=NOW)

    states = _states(report.rows)
    assert states == {
        "Producing": CoverageState.PRODUCING,
        "Empty": CoverageState.EMPTY,
        "Broken": CoverageState.FAILING,
        "Unvisited": CoverageState.NEVER_REACHED,
        "Jobvite Only": CoverageState.UNROUTABLE,
    }
    assert (report.enabled, report.disabled) == (5, 1)
    assert [row.company for row in report.gaps] == ["Empty"]


@pytest.mark.asyncio
async def test_a_board_that_has_not_answered_lately_is_stale_not_empty(seeded: Path) -> None:
    later = NOW + timedelta(days=STALE_AFTER_DAYS + 1)
    async with open_repository(seeded) as repository:
        report = await coverage(repository, COMPANIES, now=later)

    assert _states(report.rows)["Empty"] is CoverageState.STALE
    assert report.gaps == ()


@pytest.mark.asyncio
async def test_a_row_whose_board_key_cannot_be_built_reports_unroutable(seeded: Path) -> None:
    unaddressable = Company(name="No Board Key", platform=Platform.WORKDAY, slug="---")
    async with open_repository(seeded) as repository:
        report = await coverage(repository, (*COMPANIES, unaddressable), now=NOW)

    assert _states(report.rows)["No Board Key"] is CoverageState.UNROUTABLE, (
        "one unaddressable row must cost itself, not the command"
    )
    assert _states(report.rows)["Producing"] is CoverageState.PRODUCING


@pytest.mark.asyncio
async def test_a_disabled_row_is_never_reported_as_a_gap(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        report = await coverage(repository, COMPANIES, now=NOW)
    assert "Switched Off" not in _states(report.rows)


@pytest.mark.asyncio
async def test_unregistered_names_exclude_the_registry_even_under_a_different_caption(
    seeded: Path,
) -> None:
    async with open_repository(seeded) as repository:
        quiet = await coverage(repository, COMPANIES, now=NOW)
        listed = await coverage(repository, COMPANIES, now=NOW, unregistered=True)

    assert quiet.unregistered == ()
    names = {row.company for row in listed.unregistered}
    assert "Tesla" in names
    assert "Producing" not in names
    assert "Coveo Solutions Inc." in names

    registered = (*COMPANIES, Company(name="Coveo", platform=Platform.GREENHOUSE, slug="coveo"))
    async with open_repository(seeded) as repository:
        matched = await coverage(repository, registered, now=NOW, unregistered=True)
    assert "Coveo Solutions Inc." not in {row.company for row in matched.unregistered}


@pytest.mark.asyncio
async def test_exact_generic_token_name_is_not_unregistered(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="simplify",
                run_started_at=NOW,
                jobs=(_job("simplify:feed:3", "Western Digital", source="simplify"),),
            )
        )
        report = await coverage(
            repository,
            (
                *COMPANIES,
                Company(
                    name="Western Digital",
                    platform=Platform.GREENHOUSE,
                    slug="western-digital",
                ),
            ),
            now=NOW,
            unregistered=True,
        )

    assert "Western Digital" not in {row.company for row in report.unregistered}


@pytest.mark.asyncio
async def test_unregistered_rows_carry_their_sources_and_volume(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        report = await coverage(repository, COMPANIES, now=NOW, unregistered=True)

    tesla = next(row for row in report.unregistered if row.company == "Tesla")
    assert (tesla.sources, tesla.postings) == (("simplify",), 1)


@pytest.mark.asyncio
async def test_unregistered_application_urls_keep_canonical_feed_links(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        urls = await repository.company_apply_urls(("Tesla", "Coveo Solutions Inc.", "Missing"))

    assert urls == {
        "Tesla": ("https://boards.example.test/simplify:feed:1",),
        "Coveo Solutions Inc.": ("https://boards.example.test/simplify:feed:2",),
    }


@pytest.mark.asyncio
async def test_a_classified_feed_employer_leaves_the_unregistered_queue(seeded: Path) -> None:
    classification = CoverageClassification(
        company="Tesla",
        disposition=CoverageDisposition.FEED_ONLY,
        note="public careers page has no stable unauthenticated jobs endpoint",
        checked_on=NOW,
    )
    async with open_repository(seeded) as repository:
        await repository.record_coverage_classification(classification)
        report = await coverage(repository, COMPANIES, now=NOW, unregistered=True)

    assert "Tesla" not in {row.company for row in report.unregistered}
    assert report.classifications == (classification,)


@pytest.mark.asyncio
async def test_a_classification_is_normalized_replaced_and_validated(seeded: Path) -> None:
    first = CoverageClassification(
        company="  Tesla ",
        disposition=CoverageDisposition.CUSTOM_JSON_CANDIDATE,
        note="a stable public endpoint needs field mapping",
        checked_on=NOW,
        url="https://jobs.example.test/openings",
    )
    replacement = CoverageClassification(
        company="tesla",
        disposition=CoverageDisposition.ADAPTER_CANDIDATE,
        note="several employers share this public API",
        checked_on=NOW,
    )
    invalid = CoverageClassification(
        company="Tesla",
        disposition=CoverageDisposition.FEED_ONLY,
        note="checked",
        checked_on=NOW,
        url="http://jobs.example.test",
    )
    async with open_repository(seeded) as repository:
        assert not await repository.record_coverage_classification(first)
        assert await repository.record_coverage_classification(replacement)
        with pytest.raises(ValueError, match="public https"):
            await repository.record_coverage_classification(invalid)
        entries = await repository.coverage_classifications()
        assert await repository.clear_coverage_classification("TESLA")
        assert not await repository.clear_coverage_classification("Tesla")

    assert entries == [
        CoverageClassification(
            company="tesla",
            disposition=CoverageDisposition.ADAPTER_CANDIDATE,
            note="several employers share this public API",
            checked_on=NOW,
        )
    ]


def test_coverage_is_reachable_from_the_command_line(seeded: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app
    from stage.companies import write_registry

    registry = write_registry(COMPANIES, tmp_path / "companies.yaml")
    runner = CliRunner()
    result = runner.invoke(
        app, ["coverage", "--db", str(seeded), "--registry", str(registry), "--json"]
    )
    assert result.exit_code == 0, result.stdout
    assert '"state": "never-reached"' in result.stdout
    assert '"classifications": []' in result.stdout

    rendered = runner.invoke(
        app,
        ["coverage", "--unregistered", "--db", str(seeded), "--registry", str(registry)],
    )
    assert rendered.exit_code == 0, rendered.stdout
    assert "Tesla" in rendered.stdout
    assert "5 enabled row(s)" in rendered.stdout


def test_classify_records_and_clears_a_feed_company(seeded: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "classify",
            "Tesla",
            "--status",
            "feed-only",
            "--note",
            "no public jobs API",
            "--db",
            str(seeded),
        ],
    )
    assert result.exit_code == 0, result.stdout

    from stage.companies import write_registry

    registry = write_registry(COMPANIES, tmp_path / "companies.yaml")
    report = runner.invoke(
        app,
        [
            "coverage",
            "--unregistered",
            "--db",
            str(seeded),
            "--registry",
            str(registry),
        ],
    )
    assert report.exit_code == 0, report.stdout
    assert "Tesla" not in report.stdout

    reviewed = runner.invoke(app, ["coverage", "--classified", "--db", str(seeded)])
    assert reviewed.exit_code == 0, reviewed.stdout
    assert "Tesla" in reviewed.stdout
    assert "feed-only" in reviewed.stdout

    cleared = runner.invoke(app, ["classify", "Tesla", "--clear", "--db", str(seeded)])
    assert cleared.exit_code == 0, cleared.stdout

    restored = runner.invoke(
        app,
        [
            "coverage",
            "--unregistered",
            "--db",
            str(seeded),
            "--registry",
            str(registry),
        ],
    )
    assert restored.exit_code == 0, restored.stdout
    assert "Tesla" in restored.stdout


def test_classify_requires_status_and_evidence_except_when_clearing(seeded: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    runner = CliRunner()
    missing_note = runner.invoke(
        app, ["classify", "Tesla", "--status", "feed-only", "--db", str(seeded)]
    )
    missing_status = runner.invoke(
        app, ["classify", "Tesla", "--note", "researched", "--db", str(seeded)]
    )

    assert missing_note.exit_code == 2
    assert missing_status.exit_code == 2
    assert "Pass both --status and --note" in missing_note.stdout


def test_unregistered_discovery_without_adoptions_is_a_success(
    seeded: Path, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app
    from stage.companies import write_registry

    runner = CliRunner()
    registry = write_registry(COMPANIES, tmp_path / "companies.yaml")
    result = runner.invoke(
        app,
        ["discover", "--unregistered", "--db", str(seeded), "--registry", str(registry)],
    )

    assert result.exit_code == 0, result.stdout
    assert "0 adoptable" in result.stdout


@pytest.mark.asyncio
async def test_a_board_whose_postings_all_deduplicate_still_counts_as_producing(
    db_path: Path,
) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=(
                _job("greenhouse:producing:1", "Producing"),
                _job("greenhouse:empty:1", "Empty"),
            ),
            visits=(
                CompanyVisit(board="greenhouse:producing", succeeded=True, label="producing"),
                CompanyVisit(board="greenhouse:empty", succeeded=True, label="empty"),
            ),
        )
    )
    repository._conn.execute(
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
        ("greenhouse:producing:1", "greenhouse:empty:1"),
    )
    repository._conn.commit()
    repository.close()

    async with open_repository(db_path) as repo:
        report = await coverage(repo, COMPANIES, now=NOW)

    states = _states(report.rows)
    assert states["Empty"] is CoverageState.PRODUCING, "a deduped board still produced"
    assert [row.company for row in report.gaps] == []
