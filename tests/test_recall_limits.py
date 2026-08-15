from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.domain import (
    DEFAULT_WINDOW_DAYS,
    Job,
    JobFilters,
    Language,
    RoleCategory,
)
from stage.services.query import list_jobs, search_jobs
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(identifier: str, *, age_days: int = 0, **fields: object) -> Job:
    seen = NOW - timedelta(days=age_days)
    return Job(
        id=identifier,
        source="greenhouse",
        company="Acme",
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw="",
        description="",
        location_raw="Montreal, QC, Canada",
        first_seen=seen,
        last_seen=seen,
        **fields,  # type: ignore[arg-type]
    )


async def test_list_hides_anything_older_than_the_window_until_all_is_passed(
    db_path: Path,
) -> None:
    fresh = _job("greenhouse:acme:1", age_days=1)
    stale = _job("greenhouse:acme:2", age_days=DEFAULT_WINDOW_DAYS + 1)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(fresh, stale))
        )
        windowed = await list_jobs(repository, JobFilters(), now=NOW)
        everything = await list_jobs(repository, JobFilters(), window_days=None, now=NOW)

    assert DEFAULT_WINDOW_DAYS == 14
    assert [job.id for job in windowed.jobs] == [fresh.id]
    assert {job.id for job in everything.jobs} == {fresh.id, stale.id}, (
        "stage list --all is required to see every open posting"
    )


async def test_search_ignores_the_window_by_default(db_path: Path) -> None:
    stale = _job("greenhouse:acme:3", age_days=400)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(stale,))
        )
        found = await search_jobs(repository, "software", JobFilters(), now=NOW)

    assert [job.id for job in found.jobs] == [stale.id], (
        "a window is right for what is new, not for a query the user typed"
    )


@pytest.mark.parametrize(
    ("filters", "hidden"),
    [
        (JobFilters(role=RoleCategory.SWE), "unknown-role"),
        (JobFilters(term="summer-2027"), "unknown-term"),
        (JobFilters(language=Language.EN), "unknown-language"),
    ],
)
async def test_a_value_filter_hides_the_rows_whose_value_is_unknown(
    db_path: Path, filters: JobFilters, hidden: str
) -> None:
    unknown = _job("greenhouse:acme:4")
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(unknown,))
        )
        listing = await list_jobs(repository, filters, window_days=None, now=NOW)

    assert not listing.jobs, f"{hidden} rows are hidden by that filter, not surfaced"


@pytest.mark.parametrize("wanted", [Language.EN, Language.FR])
async def test_a_language_filter_keeps_bilingual_and_still_drops_unknown(
    db_path: Path, wanted: Language
) -> None:
    english = _job("greenhouse:acme:5", language=Language.EN)
    french = _job("greenhouse:acme:8", language=Language.FR)
    bilingual = _job("greenhouse:acme:6", language=Language.BILINGUAL)
    unknown = _job("greenhouse:acme:7", language=Language.UNKNOWN)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(english, french, bilingual, unknown),
            )
        )
        listing = await list_jobs(
            repository, JobFilters(language=wanted), window_days=None, now=NOW
        )

    matching = english if wanted is Language.EN else french
    assert {job.id for job in listing.jobs} == {matching.id, bilingual.id}, (
        "a bilingual posting is written in both languages, so a reader of either can use it"
    )
    assert unknown.id not in {job.id for job in listing.jobs}, (
        "unknown means detection failed, which is not evidence the reader can use it"
    )


def test_excluding_workday_removes_every_workday_board_from_the_plan() -> None:
    from stage.companies import load_companies
    from stage.services.sync import _select
    from stage.sources import load_builtins

    load_builtins()
    rows = load_companies()
    grouped, feeds, _ = _select(rows, None, ["workday"])

    assert grouped or feeds, "excluding one source must not empty the run"
    assert "workday" not in grouped, "sync --exclude workday omits Workday coverage entirely"

    with_workday, _, _ = _select(rows, None, None)
    assert "workday" in with_workday, "the exclusion is what removes it, not the registry"


def test_every_parked_row_records_why_so_the_gap_stays_workable() -> None:
    from stage.companies import load_companies

    rows = load_companies()
    disabled = [company for company in rows if not company.enabled]
    assert disabled, "a registry with nothing parked has lost its record of what it cannot reach"
    unexplained = [company.name for company in disabled if not (company.notes or "").strip()]
    assert not unexplained, f"{len(unexplained)} parked rows carry no reason: {unexplained[:5]}"


def test_an_enabled_row_always_carries_the_evidence_that_enabled_it() -> None:
    from stage.companies import load_companies

    unverified = [
        company.name
        for company in load_companies()
        if company.enabled and company.last_verified is None
    ]
    assert not unverified, f"{len(unverified)} enabled rows were never verified: {unverified[:5]}"
