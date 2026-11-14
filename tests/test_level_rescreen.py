from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import (
    Job,
    JobFilters,
    JobLevel,
    JobStatus,
    LocationBucket,
    QuarantinedJob,
    QuarantineFilters,
    RejectionReason,
    RoleCategory,
)
from stage.services import maintenance
from stage.services.maintenance import rescreen
from stage.storage import open_repository
from stage.storage.repository import SourceBatch

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
NO_ROLE = "no matching CS role or source category"


def _entry(
    identifier: str,
    title: str,
    reason: RejectionReason = RejectionReason.NOT_AN_INTERNSHIP,
    phrase: str = "no internship marker in title",
) -> QuarantinedJob:
    return QuarantinedJob(
        id=identifier,
        source="greenhouse",
        company="Acme",
        title_raw=title,
        reason=reason,
        first_seen=NOW,
        last_seen=NOW,
        apply_url_raw=f"https://jobs.example.test/{identifier}",
        location_raw="Montreal, QC",
        matched_phrase=phrase,
    )


async def _store(repository: object, entries: list[QuarantinedJob]) -> None:
    await repository.apply_source_batch(  # type: ignore[attr-defined]
        SourceBatch(source="greenhouse", run_started_at=NOW, quarantined=tuple(entries))
    )


async def _levels(repository: object) -> dict[str, JobLevel]:
    filters = JobFilters(status=None, limit=None)
    return {job.id: job.level for job in await repository.list_jobs(filters)}  # type: ignore[attr-defined]


async def _reasons(repository: object) -> dict[str, tuple[str, str]]:
    rows = await repository.list_quarantined(QuarantineFilters(limit=None))  # type: ignore[attr-defined]
    return {row.id: (row.reason.value, row.matched_phrase) for row in rows}


async def test_a_stored_not_an_internship_row_comes_back_with_its_level(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(
            repository,
            [
                _entry("grad", "Software Engineer, New Grad"),
                _entry("entry", "Software Engineer I, Backend (Continuous Delivery)"),
                _entry("senior", "Senior Software Engineer"),
                _entry("plain", "Software Engineer"),
                _entry("generalist", "Startup Generalist (New Grad)"),
            ],
        )

        result = await rescreen(repository, now=NOW)

        assert result.released == 2
        assert await _levels(repository) == {"grad": JobLevel.NEW_GRAD, "entry": JobLevel.ENTRY}
        assert await _reasons(repository) == {
            "senior": ("not-early-career", "senior"),
            "plain": ("not-early-career", "no early-career marker in title"),
            "generalist": ("unknown-cs-role", NO_ROLE),
        }


async def test_a_second_pass_finds_nothing_to_release_or_relabel(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(
            repository,
            [_entry("grad", "Software Engineer, New Grad"), _entry("plain", "Software Engineer")],
        )
        await rescreen(repository, now=NOW)

        again = await rescreen(repository, now=NOW)

        assert (again.released, again.relabelled) == (0, 0)


async def test_an_ended_internship_term_keeps_its_not_an_internship_reason(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(
            repository,
            [
                _entry(
                    "old", "Software Engineer Intern, Summer 2025", phrase="summer 2025 has ended"
                )
            ],
        )

        result = await rescreen(repository, now=NOW)

        assert (result.released, result.relabelled) == (0, 0)
        assert await _reasons(repository) == {"old": ("not-an-internship", "summer 2025 has ended")}


async def test_the_release_budget_serves_the_older_reasons_before_not_an_internship(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "RESCREEN_LIMIT", 2)
    stale = [
        _entry(f"role{number}", title, RejectionReason.UNKNOWN_CS_ROLE, NO_ROLE)
        for number, title in enumerate(("Software Engineer Intern", "Data Engineer Intern"))
    ]
    async with open_repository(db_path) as repository:
        await _store(repository, [*stale, _entry("grad", "Software Engineer, New Grad")])

        result = await rescreen(repository, now=NOW)

        assert result.released == 2
        assert await _levels(repository) == {
            "role0": JobLevel.INTERNSHIP,
            "role1": JobLevel.INTERNSHIP,
        }
        assert list(await _reasons(repository)) == ["grad"], "the new reason takes what is left"


async def test_a_stored_posting_is_rescreened_to_its_own_level(db_path: Path) -> None:
    stored = [
        Job(
            id=f"greenhouse:acme:{name}",
            source="greenhouse",
            company="Acme",
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=f"https://jobs.example.test/{name}",
            description="",
            first_seen=NOW,
            last_seen=NOW,
            location_raw="Montreal, QC",
            location=LocationBucket.MONTREAL,
            role=RoleCategory.SWE,
            status=JobStatus.OPEN,
            level=level,
        )
        for name, title, level in (
            ("intern", "Software Engineer Intern", JobLevel.INTERNSHIP),
            ("grad", "Software Engineer, New Grad", JobLevel.NEW_GRAD),
            ("old", "Senior Software Engineer", JobLevel.INTERNSHIP),
        )
    ]
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(stored))
        )

        result = await rescreen(repository, now=NOW)

        assert result.quarantined == 1
        assert await _levels(repository) == {
            "greenhouse:acme:intern": JobLevel.INTERNSHIP,
            "greenhouse:acme:grad": JobLevel.NEW_GRAD,
        }
        assert await _reasons(repository) == {"greenhouse:acme:old": ("not-early-career", "senior")}
