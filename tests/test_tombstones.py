
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.dedup import SOURCE_PRIORITY, resolve_duplicates
from stage.domain import Job, JobStatus, LocationBucket
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def job(
    job_id: str,
    *,
    source: str = "greenhouse",
    company: str = "Acme",
    title: str = "Software Engineer Intern",
    seen: datetime = NOW,
    status: JobStatus = JobStatus.OPEN,
) -> Job:
    return Job(
        id=job_id,
        source=source,
        company=company,
        title_raw=title,
        title_normalized=title,
        apply_url_raw="",
        description="",
        first_seen=seen,
        last_seen=seen,
        status=status,
        location=LocationBucket.USA,
    )


def test_the_promotion_order_comes_from_domain() -> None:
    from stage.domain import SOURCE_PRIORITY as DOMAIN_PRIORITY
    from stage.domain import source_rank

    assert SOURCE_PRIORITY is DOMAIN_PRIORITY
    assert source_rank("greenhouse", "a") < source_rank("simplify", "a")
    assert source_rank("simplify", "a") < source_rank("vanshb03", "a")
    assert source_rank("unlisted-future-adapter", "a") > source_rank("vanshb03", "z")


async def test_an_expired_posting_leaves_a_tombstone(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        old = job("old", seen=NOW - timedelta(days=20))
        fresh = job("fresh", seen=NOW - timedelta(days=2))
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(old, fresh))
        )
        result = await repository.purge(NOW)
        assert result.purged == 1
        assert result.tombstoned == 1
        assert await repository.get_job("old") is None
        assert await repository.get_job("fresh") is not None
        assert await repository.tombstone_count() == 1


async def test_a_purged_posting_does_not_resurrect_with_a_fresh_date(db_path: Path) -> None:
    original = NOW - timedelta(days=20)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(job("row", seen=original),))
        )
        await repository.purge(NOW)
        assert await repository.get_job("row") is None

        later = NOW + timedelta(days=1)
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=later, jobs=(job("row", seen=later),)
            )
        )
        restored = await repository.get_job("row")
        assert restored is not None
        assert restored.first_seen == original, "purged posting resurfaced as new"


async def test_closed_postings_purge_faster_than_open_ones(db_path: Path) -> None:
    seen = NOW - timedelta(days=5)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(
                    job("open", seen=seen),
                    job("shut", seen=seen, status=JobStatus.CLOSED),
                ),
            )
        )
        result = await repository.purge(NOW)
        assert result.purged == 1
        assert await repository.get_job("open") is not None
        assert await repository.get_job("shut") is None


async def test_a_duplicate_is_promoted_when_its_survivor_is_purged(db_path: Path) -> None:
    old = NOW - timedelta(days=20)
    async with open_repository(db_path) as repository:
        survivor = job("direct", source="greenhouse", seen=old)
        duplicate = job("feed", source="simplify", seen=NOW - timedelta(days=1))
        links = resolve_duplicates([survivor, duplicate], [])
        assert [(link.duplicate_id, link.canonical_id) for link in links] == [
            ("feed", "direct")
        ]
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(survivor, duplicate),
                resolve_duplicates=lambda _incoming, _existing: links,
            )
        )
        assert await repository.count_duplicates() == 1

        result = await repository.purge(NOW)
        assert result.purged == 1
        assert result.promoted == 1
        assert await repository.get_job("direct") is None
        promoted = await repository.get_job("feed")
        assert promoted is not None, "the surviving copy must remain reachable"
        assert await repository.count_duplicates() == 0


async def test_promotion_picks_the_highest_priority_remaining_row(db_path: Path) -> None:
    old = NOW - timedelta(days=20)
    async with open_repository(db_path) as repository:
        survivor = job("gh", source="greenhouse", seen=old)
        mid = job("simp", source="simplify", seen=NOW)
        low = job("vansh", source="vanshb03", seen=NOW)
        links = resolve_duplicates([survivor, mid, low], [])
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(survivor, mid, low),
                resolve_duplicates=lambda _incoming, _existing: links,
            )
        )
        await repository.purge(NOW)
        assert await repository.get_job("simp") is not None
        assert await repository.count_duplicates() == 1


async def test_purging_nothing_is_not_an_error(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(job("fresh"),))
        )
        result = await repository.purge(NOW)
        assert result.purged == 0
        assert await repository.tombstone_count() == 0


async def test_purge_is_idempotent(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(job("old", seen=NOW - timedelta(days=30)),),
            )
        )
        first = await repository.purge(NOW)
        second = await repository.purge(NOW)
        assert first.purged == 1
        assert second.purged == 0
        assert await repository.tombstone_count() == 1


@pytest.mark.parametrize("days", [13, 15])
async def test_the_window_is_measured_from_first_seen(db_path: Path, days: int) -> None:
    async with open_repository(db_path) as repository:
        stored = job("row", seen=NOW - timedelta(days=days))
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(stored,))
        )
        result = await repository.purge(NOW)
        assert result.purged == (1 if days > 14 else 0)
