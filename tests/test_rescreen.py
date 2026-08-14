from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import Job, JobStatus, LocationBucket, RoleCategory
from stage.services.maintenance import rescreen
from stage.storage import open_repository
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _job(identifier: str, title: str, *, role: RoleCategory = RoleCategory.SWE) -> Job:
    return Job(
        id=identifier,
        source="workday",
        company="TD Bank",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=f"https://boards.example.test/{identifier}",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw="Montreal, QC",
        location=LocationBucket.MONTREAL,
        role=role,
        status=JobStatus.OPEN,
    )


@pytest.fixture
def seeded(db_path: Path) -> Path:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(
                _job("workday:td:1", "Personal Banking Associate Trainee"),
                _job("workday:td:2", "Software Engineer Intern"),
            ),
        )
    )
    repository.close()
    return db_path


@pytest.mark.asyncio
async def test_a_row_the_lexicon_now_rejects_is_moved_to_quarantine(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        result = await rescreen(repository, now=NOW)

    assert result.examined == 2
    assert result.quarantined == 1

    store = SqliteRepository.connect(seeded)
    kept = [row["id"] for row in store._conn.execute("SELECT id FROM jobs")]
    banished = [row["id"] for row in store._conn.execute("SELECT id FROM quarantine")]
    assert kept == ["workday:td:2"]
    assert banished == ["workday:td:1"]
    store.close()


@pytest.mark.asyncio
async def test_the_move_preserves_first_seen(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        await rescreen(repository, now=NOW.replace(year=2027))

    store = SqliteRepository.connect(seeded)
    row = store._conn.execute(
        "SELECT first_seen FROM quarantine WHERE id = 'workday:td:1'"
    ).fetchone()
    assert row["first_seen"].startswith("2026-08-08")
    store.close()


@pytest.mark.asyncio
async def test_a_second_pass_finds_nothing_left_to_do(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        await rescreen(repository, now=NOW)
        again = await rescreen(repository, now=NOW)
    assert again.quarantined == 0
    assert again.changed is False


@pytest.mark.asyncio
async def test_an_empty_database_is_not_an_error(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)
    assert (result.examined, result.quarantined) == (0, 0)


@pytest.mark.asyncio
async def test_rescreen_never_rewrites_a_stored_classification(db_path: Path) -> None:
    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="simplify",
            run_started_at=NOW,
            jobs=(_job("simplify:feed:1", "Summer Intern", role=RoleCategory.QUANT),),
        )
    )
    store._conn.execute("UPDATE jobs SET term = 'summer-2027' WHERE id = 'simplify:feed:1'")
    store._conn.commit()
    store.close()

    async with open_repository(db_path) as repo:
        result = await rescreen(repo, now=NOW)

    assert result.quarantined == 0
    store = SqliteRepository.connect(db_path)
    row = store._conn.execute("SELECT term, role FROM jobs WHERE id = 'simplify:feed:1'").fetchone()
    assert (row["term"], row["role"]) == ("summer-2027", RoleCategory.QUANT.value), (
        "SourceSignals are not stored, so re-deriving term or role would lose the feed fields"
    )
    store.close()


def test_rescreen_is_reachable_from_the_command_line(seeded: Path) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    result = CliRunner().invoke(app, ["rescreen", "--db", str(seeded)])
    assert result.exit_code == 0, result.stdout
    assert "moved to quarantine" in result.stdout

    again = CliRunner().invoke(app, ["rescreen", "--db", str(seeded)])
    assert again.exit_code == 0, again.stdout
    assert "agrees with every one" in again.stdout


@pytest.mark.asyncio
async def test_clearing_a_cached_validator_forces_the_next_fetch(db_path: Path) -> None:
    from stage.domain import HttpValidator
    from stage.services.maintenance import rate_state

    store = SqliteRepository.connect(db_path)
    store.apply_source_batch(
        SourceBatch(
            source="simplify",
            run_started_at=NOW,
            validators=(
                HttpValidator(
                    url="https://raw.example.test/listings.json", etag="abc", fetched_at=NOW
                ),
            ),
        )
    )
    store.close()

    async with open_repository(db_path) as repository:
        assert await repository.cached_url_count() == 1
        view = await rate_state(repository, clear_cache="simplify")
        assert view.validators_cleared == 1
        assert await repository.cached_url_count() == 0


async def test_rescreen_removes_a_stored_phd_internship_and_keeps_its_term_and_role(
    db_path: Path,
) -> None:
    from dataclasses import replace

    from stage.domain import JobFilters, QuarantineFilters, RejectionReason

    doctorate = replace(
        _job("workday:acme:1", "PhD Research Intern", role=RoleCategory.ML_AI),
        term="summer-2027",
    )
    ordinary = replace(_job("workday:acme:2", "Software Engineer Intern"), term="summer-2027")
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(source="workday", run_started_at=NOW, jobs=(doctorate, ordinary))
    )
    repository.close()

    async with open_repository(db_path) as target:
        result = await rescreen(target, now=NOW)
        stored = {job.id: job for job in await target.list_jobs(JobFilters())}
        held = await target.list_quarantined(
            QuarantineFilters(reason=RejectionReason.OUT_OF_SCOPE_DEGREE)
        )

    assert result.quarantined == 1
    assert doctorate.id not in stored
    assert [entry.id for entry in held] == [doctorate.id]
    assert stored[ordinary.id].term == "summer-2027", "screening must not rewrite a term"
    assert stored[ordinary.id].role is RoleCategory.SWE, "screening must not rewrite a role"
    assert held[0].first_seen == NOW, "a move preserves first_seen"


async def test_rescreen_converges_when_quarantining_a_canonical_promotes_a_follower(
    db_path: Path,
) -> None:
    from dataclasses import replace

    from stage.domain import JobFilters, QuarantineFilters, RejectionReason

    canonical = _job("greenhouse:acme:1", "PhD Research Intern", role=RoleCategory.ML_AI)
    follower = replace(
        _job("simplify:acme:2", "Research Scientist Intern, PhD", role=RoleCategory.ML_AI),
        source="simplify",
        duplicate_of=canonical.id,
    )
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(canonical,))
    )
    repository.apply_source_batch(
        SourceBatch(source="simplify", run_started_at=NOW, jobs=(follower,))
    )
    repository._conn.execute(
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?", (canonical.id, follower.id)
    )
    repository._conn.commit()
    repository.close()

    async with open_repository(db_path) as target:
        result = await rescreen(target, now=NOW)
        remaining = await target.list_jobs(JobFilters(status=None))
        held = await target.list_quarantined(
            QuarantineFilters(reason=RejectionReason.OUT_OF_SCOPE_DEGREE)
        )
        findings = await target.integrity_findings()

    assert result.quarantined == 2, (
        "quarantining a canonical promotes its follower, so one pass cannot converge"
    )
    assert not remaining, "no PhD-restricted posting may survive as a promoted canonical"
    assert {entry.id for entry in held} == {canonical.id, follower.id}
    assert not [finding for finding in findings if finding.count], (
        "the move must leave no dangling duplicate link behind"
    )


@pytest.mark.asyncio
async def test_a_pass_that_cannot_read_every_row_says_so(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import JobFilters

    monkeypatch.setattr("stage.services.maintenance.RESCREEN_LIMIT", 1)
    async with open_repository(seeded) as repository:
        stored = len(await repository.list_jobs(JobFilters(status=None)))
        result = await rescreen(repository, now=NOW)

    assert stored > 1, "the fixture has to hold more rows than the cap, or nothing is skipped"
    assert result.total == stored
    assert result.examined == 1
    assert result.skipped == stored - 1, (
        "a capped pass reporting the full count is a silent truncation"
    )


@pytest.mark.asyncio
async def test_an_uncapped_pass_reports_nothing_skipped(seeded: Path) -> None:
    async with open_repository(seeded) as repository:
        result = await rescreen(repository, now=NOW)
    assert result.skipped == 0
    assert result.total == result.examined


@pytest.mark.asyncio
async def test_rescreen_persists_active_location_corrections(db_path: Path) -> None:
    from dataclasses import replace

    job = replace(
        _job("workday:td:1", "Software Engineer Intern"),
        location_raw="IN - Bangalore, India",
        location=LocationBucket.USA,
    )
    sync_repository = SqliteRepository.connect(db_path)
    sync_repository.apply_source_batch(
        SourceBatch(source="workday", run_started_at=NOW, jobs=(job,))
    )
    sync_repository.close()

    async with open_repository(db_path) as repository:
        result = await rescreen(repository, now=NOW)
        stored = await repository.get_job(job.id)

    assert result.updated == 1
    assert result.quarantined == 0
    assert stored is not None
    assert stored.location is LocationBucket.INTERNATIONAL
