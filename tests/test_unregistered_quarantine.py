from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import (
    Company,
    Job,
    LocationBucket,
    Platform,
    QuarantinedJob,
    RejectionReason,
)
from stage.services.coverage import coverage
from stage.services.discover import select_unregistered
from stage.storage import open_repository
from stage.storage.repository import SourceBatch

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
REGISTERED = (Company(name="Coveo", platform=Platform.GREENHOUSE, slug="coveo"),)


def _rejected(identifier: str, company: str, reason: RejectionReason) -> QuarantinedJob:
    return QuarantinedJob(
        id=identifier,
        source="simplify",
        company=company,
        title_raw="Mechanical Engineering Intern",
        reason=reason,
        first_seen=NOW,
        last_seen=NOW,
        apply_url_raw=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{identifier}",
    )


def _kept(identifier: str, company: str) -> Job:
    return Job(
        id=identifier,
        source="simplify",
        company=company,
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{identifier}",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montréal, QC",
        location=LocationBucket.MONTREAL,
    )


async def _seed(path: Path) -> None:
    async with open_repository(path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="simplify",
                run_started_at=NOW,
                jobs=(_kept("simplify:feed:1", "Keeper"),),
                quarantined=(
                    _rejected("simplify:feed:2", "Roleblocked", RejectionReason.UNKNOWN_CS_ROLE),
                    _rejected("simplify:feed:3", "Roleblocked", RejectionReason.UNKNOWN_CS_ROLE),
                    _rejected("simplify:feed:4", "Notintern", RejectionReason.NOT_AN_INTERNSHIP),
                    _rejected("simplify:feed:5", "Notintern", RejectionReason.NOT_AN_INTERNSHIP),
                    _rejected("simplify:feed:6", "Notintern", RejectionReason.NOT_AN_INTERNSHIP),
                ),
            )
        )


@pytest.mark.asyncio
async def test_an_employer_seen_only_in_quarantine_reaches_the_growth_queue(tmp_path: Path) -> None:
    db = tmp_path / "stage.db"
    await _seed(db)
    async with open_repository(db) as repository:
        report = await coverage(repository, REGISTERED, now=NOW, unregistered=True)

    found = {row.company for row in report.unregistered}
    assert "Roleblocked" in found, "an all-rejected board is invisible to registry growth"
    assert "Notintern" in found, "an all-rejected board is invisible to registry growth"


@pytest.mark.asyncio
async def test_a_kept_posting_still_outranks_any_quarantine_evidence(tmp_path: Path) -> None:
    db = tmp_path / "stage.db"
    await _seed(db)
    async with open_repository(db) as repository:
        report = await coverage(repository, REGISTERED, now=NOW, unregistered=True)

    order = [row.company for row in report.unregistered]
    assert order[0] == "Keeper", "an employer with a kept posting must lead the queue"


@pytest.mark.asyncio
async def test_rejection_on_role_outranks_rejection_on_the_internship_screen(
    tmp_path: Path,
) -> None:
    db = tmp_path / "stage.db"
    await _seed(db)
    async with open_repository(db) as repository:
        report = await coverage(repository, REGISTERED, now=NOW, unregistered=True)

    rows = {row.company: row for row in report.unregistered}
    assert rows["Roleblocked"].posts_internships is True
    assert rows["Notintern"].posts_internships is False
    order = [row.company for row in report.unregistered]
    assert order.index("Roleblocked") < order.index("Notintern"), (
        "a board that posts internships must outrank one rejected on the internship screen"
    )


@pytest.mark.asyncio
async def test_apply_urls_are_read_from_quarantine_as_well_as_kept_rows(tmp_path: Path) -> None:
    db = tmp_path / "stage.db"
    await _seed(db)
    async with open_repository(db) as repository:
        urls = await repository.company_apply_urls(("Roleblocked", "Keeper"))

    assert "Roleblocked" in urls, "a board token on a rejected posting is still the employer's own"
    assert urls["Roleblocked"], "a board token on a rejected posting is still the employer's own"


def test_the_limit_counts_probeable_employers_not_queue_position() -> None:
    ranked = ["NoBoard1", "NoBoard2", "HasBoard"]
    apply_urls = {"HasBoard": ("https://job-boards.greenhouse.io/hasboard/jobs/1",)}
    direct, names = select_unregistered(ranked, apply_urls, limit=2, direct_only=True)

    assert [company.name for company in direct] == ["HasBoard"], (
        "a direct-only run must not spend its limit on employers with no board link"
    )
    assert names == ()
