import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.dedup import SOURCE_PRIORITY, resolve_duplicates
from stage.domain import Job, JobStatus, LocationBucket
from stage.storage import SourceBatch, open_repository
from stage.storage.sqlite_repo import SqliteRepository

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
            SourceBatch(source="greenhouse", run_started_at=later, jobs=(job("row", seen=later),))
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
        assert [(link.duplicate_id, link.canonical_id) for link in links] == [("feed", "direct")]
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


def test_promotion_never_hands_a_cluster_to_a_row_it_is_about_to_purge(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    old = NOW - timedelta(days=5)
    repository.apply_source_batch(
        SourceBatch(
            source="seed",
            run_started_at=old,
            jobs=(
                _job("greenhouse:postman:1", "greenhouse", old, status=JobStatus.CLOSED),
                _job("lever:postman:2", "lever", old, status=JobStatus.CLOSED),
                _job("vanshb03:postman:3", "vanshb03", NOW),
            ),
        )
    )
    repository._conn.executemany(
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
        [
            ("greenhouse:postman:1", "lever:postman:2"),
            ("greenhouse:postman:1", "vanshb03:postman:3"),
        ],
    )
    repository._conn.commit()

    result = repository.purge(NOW)
    assert result.purged == 2
    survivors = repository._conn.execute("SELECT id, duplicate_of FROM jobs ORDER BY id").fetchall()
    assert [row["id"] for row in survivors] == ["vanshb03:postman:3"]
    assert survivors[0]["duplicate_of"] is None, "the only surviving row must become canonical"
    assert _dangling(repository) == 0
    repository.close()


def test_a_purge_heals_a_link_that_was_already_dangling(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="seed",
            run_started_at=NOW,
            jobs=(
                _job("greenhouse:postman:1", "greenhouse", NOW),
                _job("vanshb03:postman:3", "vanshb03", NOW),
            ),
        )
    )
    repository._conn.execute(
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
        ("greenhouse:postman:1", "vanshb03:postman:3"),
    )
    repository._conn.execute("DELETE FROM jobs WHERE id = 'greenhouse:postman:1'")
    repository._conn.commit()
    assert _dangling(repository) == 1

    repository.purge(NOW)
    assert _dangling(repository) == 0
    row = repository._conn.execute(
        "SELECT duplicate_of FROM jobs WHERE id = 'vanshb03:postman:3'"
    ).fetchone()
    assert row["duplicate_of"] is None
    repository.close()


def _dangling(repository: SqliteRepository) -> int:
    return int(
        repository._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs a LEFT JOIN jobs b ON a.duplicate_of = b.id "
            "WHERE a.duplicate_of IS NOT NULL AND b.id IS NULL"
        ).fetchone()["n"]
    )


def _job(
    identifier: str,
    source: str,
    first_seen: datetime,
    *,
    status: JobStatus = JobStatus.OPEN,
) -> Job:
    return Job(
        id=identifier,
        source=source,
        company="Postman",
        title_raw="AI Engineer Intern",
        title_normalized="ai engineer intern",
        apply_url_raw="https://boards.example.test/postman",
        description="",
        first_seen=first_seen,
        last_seen=first_seen,
        location_raw="Remote",
        location=LocationBucket.USA,
        status=status,
    )


def test_every_shipped_source_is_ranked_so_none_falls_below_the_feeds() -> None:
    from stage.domain import SOURCE_PRIORITY as RANKED

    shipped = {
        "greenhouse",
        "lever",
        "smartrecruiters",
        "ashby",
        "workday",
        "workable",
        "bamboohr",
        "recruitee",
        "breezy",
        "collage",
        "oracle_cloud",
        "custom_json",
        "quebec-emploi",
        "simplify",
        "vanshb03",
        "speedyapply",
        "zshah101",
        "hanzili",
        "negar",
        "northwestern-quant",
    }
    missing = sorted(shipped - set(RANKED))

    assert not missing, f"unranked sources tie below every feed: {missing}"


def test_a_direct_employer_board_outranks_every_community_feed() -> None:
    from stage.domain import SOURCE_PRIORITY as RANKED

    feeds = ("simplify", "vanshb03", "speedyapply", "zshah101", "hanzili", "negar")
    worst_direct = max(
        RANKED.index(name)
        for name in ("greenhouse", "workday", "custom_json", "oracle_cloud", "workable", "bamboohr")
    )
    best_feed = min(RANKED.index(name) for name in feeds)

    assert worst_direct < best_feed, (
        "a feed would win the canonical row over the employer's own board"
    )


def test_a_link_the_matcher_no_longer_produces_is_cleared(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    pair_a = _job("greenhouse:acme:1", "greenhouse", NOW)
    feed_a = _job("simplify:acme:2", "simplify", NOW)
    pair_b = dataclasses.replace(
        _job("greenhouse:acme:3", "greenhouse", NOW),
        title_raw="Data Engineer Intern",
        title_normalized="data engineer intern",
    )
    feed_b = dataclasses.replace(
        _job("simplify:acme:4", "simplify", NOW),
        title_raw="Data Engineer Intern",
        title_normalized="data engineer intern",
    )
    repository.apply_source_batch(
        SourceBatch(
            source="seed",
            run_started_at=NOW,
            jobs=(pair_a, feed_a, pair_b, feed_b),
            resolve_duplicates=resolve_duplicates,
        )
    )
    linked = repository._conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL"
    ).fetchone()[0]
    assert linked == 2, "the feed rows were never linked, so the test proves nothing"

    renamed = dataclasses.replace(
        feed_a, title_raw="Principal Analog IC Architect", title_normalized="principal analog ic"
    )
    repository.apply_source_batch(
        SourceBatch(
            source="simplify",
            run_started_at=NOW,
            jobs=(renamed, feed_b),
            resolve_duplicates=resolve_duplicates,
        )
    )

    stale = repository._conn.execute(
        "SELECT duplicate_of FROM jobs WHERE id = ?", ("simplify:acme:2",)
    ).fetchone()[0]
    assert stale is None, "a stale link survived, so it can never repoint when priority changes"
    kept = repository._conn.execute(
        "SELECT duplicate_of FROM jobs WHERE id = ?", ("simplify:acme:4",)
    ).fetchone()[0]
    assert kept == "greenhouse:acme:3", (
        "clearing stale links must not unlink a pair that still matches"
    )
    repository.close()
