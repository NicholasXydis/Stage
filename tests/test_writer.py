import asyncio
import threading
from datetime import datetime
from pathlib import Path

import pytest

from stage.domain import Job, JobFilters
from stage.storage import DatabaseWriter, SourceBatch, WriterNotStartedError, open_repository
from stage.storage.repository import Repository


def _job(job_id: str, moment: datetime) -> Job:
    return Job(
        id=job_id,
        source="greenhouse",
        company="Acme",
        title_raw="Software Engineering Intern",
        title_normalized="Software Engineering Intern",
        apply_url_raw=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        description="",
        first_seen=moment,
        last_seen=moment,
    )


async def test_writer_uses_a_single_worker(db_path: Path) -> None:
    writer = DatabaseWriter(db_path)
    await writer.start()
    try:
        assert writer._executor._max_workers == 1
    finally:
        await writer.aclose()


async def test_connection_stays_on_the_writer_thread(db_path: Path) -> None:
    writer = DatabaseWriter(db_path)
    await writer.start()
    try:
        observed = await asyncio.gather(
            *(writer.run(lambda _: threading.get_ident()) for _ in range(8))
        )
        assert set(observed) == {writer.thread_id}
        assert writer.thread_id != threading.get_ident()
    finally:
        await writer.aclose()


async def test_concurrent_calls_never_overlap(db_path: Path) -> None:
    writer = DatabaseWriter(db_path)
    await writer.start()
    active = 0
    peak = 0

    def occupy(_: Repository) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        threading.Event().wait(0.01)
        active -= 1

    try:
        await asyncio.gather(*(writer.run(occupy) for _ in range(10)))
    finally:
        await writer.aclose()

    assert peak == 1


async def test_run_before_start_is_refused(db_path: Path) -> None:
    writer = DatabaseWriter(db_path)
    try:
        with pytest.raises(WriterNotStartedError):
            await writer.run(lambda repository: repository.last_sync_at())
    finally:
        writer._executor.shutdown(wait=True)


async def test_writes_are_visible_through_the_async_repository(
    db_path: Path, run_time: datetime
) -> None:
    async with open_repository(db_path) as repository:
        result = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time,
                jobs=(_job("greenhouse:acme:1", run_time),),
                closable_boards=("greenhouse:acme",),
            )
        )
        assert (result.added, result.updated, result.closed) == (1, 0, 0)

        jobs = await repository.list_jobs(JobFilters())
        assert [job.id for job in jobs] == ["greenhouse:acme:1"]
        assert jobs[0].first_seen == run_time
