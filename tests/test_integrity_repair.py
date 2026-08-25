import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from stage.domain import Job, JobStatus
from stage.services.maintenance import repair_integrity
from stage.storage import open_repository
from stage.storage.repository import SourceBatch

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _job(identifier: str, source: str = "greenhouse", **kwargs: object) -> Job:
    fields: dict[str, object] = {
        "id": identifier,
        "source": source,
        "company": "Acme",
        "title_raw": f"Intern {identifier}",
        "title_normalized": f"intern {identifier}",
        "apply_url_raw": f"https://jobs.example.test/{identifier}",
        "description": "",
        "first_seen": NOW,
        "last_seen": NOW,
    }
    fields.update(kwargs)
    return Job(**fields)  # type: ignore[arg-type]


async def _store(repository: object, jobs: list[Job]) -> None:
    await repository.apply_source_batch(  # type: ignore[attr-defined]
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(jobs))
    )


async def _counts(repository: object) -> dict[str, int]:
    findings = await repository.integrity_findings()  # type: ignore[attr-defined]
    return {finding.check: finding.count for finding in findings}


async def test_a_dangling_duplicate_link_is_cleared_so_the_posting_reappears(
    db_path: Path,
) -> None:
    async with open_repository(db_path) as repository:
        await _store(repository, [_job("a"), _job("b", source="lever", duplicate_of="a")])

    with closing(sqlite3.connect(db_path)) as corrupt:
        corrupt.execute("DELETE FROM jobs WHERE id = 'a'")
        corrupt.commit()

    async with open_repository(db_path) as repository:
        assert (await _counts(repository))["dangling duplicate links"] == 1

        repairs = await repair_integrity(repository)
        assert any(entry.check == "dangling duplicate links" for entry in repairs)
        assert (await _counts(repository))["dangling duplicate links"] == 0
        freed = await repository.get_job("b")
        assert freed is not None and freed.duplicate_of is None, (
            "the posting must become visible again once its survivor is gone"
        )


async def test_a_duplicate_chain_is_flattened_onto_the_survivor(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(
            repository,
            [
                _job("a"),
                _job("b", source="lever", duplicate_of="a"),
                _job("c", source="ashby", duplicate_of="b"),
            ],
        )
        assert (await _counts(repository))["duplicate chains"] == 1

        await repair_integrity(repository)
        assert (await _counts(repository))["duplicate chains"] == 0
        rebuilt = await repository.get_job("c")
        assert rebuilt is not None and rebuilt.duplicate_of == "a", (
            "a follower must end up pointing at the survivor, not at another follower"
        )


async def test_a_same_board_merge_is_undone(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(repository, [_job("a"), _job("b", duplicate_of="a")])
        assert (await _counts(repository))["same-board merges"] == 1

        await repair_integrity(repository)
        assert (await _counts(repository))["same-board merges"] == 0
        freed = await repository.get_job("b")
        assert freed is not None and freed.duplicate_of is None, (
            "two rows from one board are two requisitions and both must stay visible"
        )


async def test_repair_reports_nothing_when_there_is_nothing_to_repair(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(repository, [_job("a"), _job("b", source="lever", duplicate_of="a")])
        assert await repair_integrity(repository) == (), (
            "a healthy database must produce no repair rows"
        )


async def test_a_finding_that_cannot_be_repaired_safely_is_left_for_a_human(
    db_path: Path,
) -> None:
    async with open_repository(db_path) as repository:
        await _store(repository, [_job("a", status=JobStatus.OPEN)])
        repairs = await repair_integrity(repository)
        assert not [entry for entry in repairs if entry.check == "postings in both tables"], (
            "quarantine collisions need a human decision and must not be repaired blindly"
        )


async def test_a_renamed_board_stops_holding_its_old_postings_open(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(
            repository,
            [
                _job("greenhouse:acme:1", status=JobStatus.OPEN),
                _job("greenhouse:acme-old:2", status=JobStatus.OPEN),
            ],
        )
        closed = await repository.close_orphan_boards(["greenhouse"], ["greenhouse:acme"])
        assert closed == 1, "only the board the registry can no longer poll is closed"
        live = await repository.get_job("greenhouse:acme:1")
        orphan = await repository.get_job("greenhouse:acme-old:2")
        assert live is not None and live.status is JobStatus.OPEN
        assert orphan is not None and orphan.status is JobStatus.CLOSED


async def test_a_feed_posting_is_never_closed_as_an_orphan_board(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(
            repository, [_job("simplify:acme:1", source="simplify", status=JobStatus.OPEN)]
        )
        closed = await repository.close_orphan_boards(["greenhouse"], ["greenhouse:acme"])
        assert closed == 0, "a feed board is not a registry row and cannot be orphaned"


async def test_a_posting_with_no_board_in_its_id_is_never_closed_as_an_orphan(
    db_path: Path,
) -> None:
    async with open_repository(db_path) as repository:
        await _store(repository, [_job("greenhouse-deferred", status=JobStatus.OPEN)])
        closed = await repository.close_orphan_boards(["greenhouse"], ["greenhouse:acme"])
        assert closed == 0, "an id carrying no board identity is unknown, not orphaned"


async def test_no_known_boards_closes_nothing(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await _store(repository, [_job("greenhouse:acme:1", status=JobStatus.OPEN)])
        assert await repository.close_orphan_boards(["greenhouse"], []) == 0, (
            "an empty registry is a loading failure, not proof every board is gone"
        )
