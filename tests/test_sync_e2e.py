import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from stage.domain import (
    Company,
    CompanyFailed,
    JobFilters,
    JobStatus,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    SyncStarted,
)
from stage.services.query import list_jobs
from stage.services.sync import sync
from stage.storage import AsyncRepository, open_repository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.http import RatePosture
    from stage.services import sync as sync_module

    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=300),
    )


async def _run_sync(
    repository: AsyncRepository, companies: Sequence[Company], moment: datetime
) -> list[SyncEvent]:
    return [
        event
        async for event in sync(
            repository, companies, sources=["greenhouse"], now_fn=lambda: moment
        )
    ]


@respx.mock
async def test_sync_then_list_end_to_end(db_path: Path, acme: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))

    async with open_repository(db_path) as repository:
        events = await _run_sync(repository, [acme], run_time)
        listing = await list_jobs(repository, JobFilters(), now=run_time)

    assert isinstance(events[0], SyncStarted)
    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.outcome is SyncOutcome.SUCCESS
    assert (finished.added, finished.updated, finished.closed) == (3, 0, 0)

    assert listing.total_matching == 3
    assert listing.last_sync_at is not None
    assert [job.company for job in listing.jobs] == ["Acme", "Acme", "Acme"]


@respx.mock
async def test_first_seen_survives_a_source_side_edit(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))

    async with open_repository(db_path) as repository:
        await _run_sync(repository, [acme], run_time)
        original = await repository.get_job("greenhouse:acme:4012345")

        edited = _payload()
        edited["jobs"][0]["title"] = "Software Engineer Intern (Summer) — updated"
        edited["jobs"][0]["updated_at"] = "2026-08-05T09:00:00-04:00"
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=edited))

        later = run_time + timedelta(days=5)
        events = await _run_sync(repository, [acme], later)
        after = await repository.get_job("greenhouse:acme:4012345")
        listing = await list_jobs(repository, JobFilters(), now=later)

    assert original is not None
    assert after is not None
    assert after.first_seen == original.first_seen == run_time
    assert after.last_seen == later
    assert after.title_raw == "Software Engineer Intern (Summer) — updated"

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert (finished.added, finished.updated) == (0, 3)
    assert listing.total_matching == 3


@respx.mock
async def test_sync_is_idempotent(db_path: Path, acme: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))

    async with open_repository(db_path) as repository:
        await _run_sync(repository, [acme], run_time)
        events = await _run_sync(repository, [acme], run_time)
        listing = await list_jobs(repository, JobFilters(), now=run_time)

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert (finished.added, finished.updated, finished.closed) == (0, 3, 0)
    assert listing.total_matching == 3


@respx.mock
async def test_vanished_postings_are_closed_not_deleted(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))

    async with open_repository(db_path) as repository:
        await _run_sync(repository, [acme], run_time)

        shrunk = _payload()
        shrunk["jobs"] = shrunk["jobs"][:1]
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=shrunk))

        later = run_time + timedelta(days=1)
        events = await _run_sync(repository, [acme], later)
        closed_job = await repository.get_job("greenhouse:acme:4012347")
        survivor = await repository.get_job("greenhouse:acme:4012345")
        open_listing = await list_jobs(repository, JobFilters(), now=later)
        every_row = await list_jobs(repository, JobFilters(status=None), now=later)

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.closed == 2

    assert closed_job is not None
    assert closed_job.status is JobStatus.CLOSED
    assert closed_job.first_seen == run_time
    assert closed_job.last_seen == run_time
    assert closed_job.title_raw == "Cybersecurity Analyst Intern"

    assert survivor is not None
    assert survivor.status is JobStatus.OPEN
    assert survivor.last_seen == later

    assert open_listing.total_matching == 1
    assert every_row.total_matching == 3


@respx.mock
async def test_a_closed_posting_reopens_when_it_returns_to_the_feed(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    shrunk = _payload()
    shrunk["jobs"] = shrunk["jobs"][:1]
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=shrunk))

    async with open_repository(db_path) as repository:
        await _run_sync(repository, [acme], run_time)

        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
        day_two = run_time + timedelta(days=1)
        await _run_sync(repository, [acme], day_two)

        shrunk_again = _payload()
        shrunk_again["jobs"] = shrunk_again["jobs"][:1]
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=shrunk_again))
        day_three = run_time + timedelta(days=2)
        await _run_sync(repository, [acme], day_three)

        closed = await repository.get_job("greenhouse:acme:4012347")

        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
        day_four = run_time + timedelta(days=3)
        await _run_sync(repository, [acme], day_four)
        reopened = await repository.get_job("greenhouse:acme:4012347")

    assert closed is not None
    assert closed.status is JobStatus.CLOSED
    assert reopened is not None
    assert reopened.status is JobStatus.OPEN
    assert reopened.first_seen == day_two


@respx.mock
async def test_a_failing_board_does_not_stop_the_run(db_path: Path, run_time: datetime) -> None:
    from stage.domain import Platform

    good = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    bad = Company(name="Broken", platform=Platform.GREENHOUSE, slug="broken")
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
    respx.get("https://boards-api.greenhouse.io/v1/boards/broken/jobs").mock(
        return_value=httpx.Response(404)
    )

    async with open_repository(db_path) as repository:
        events = await _run_sync(repository, [good, bad], run_time)
        listing = await list_jobs(repository, JobFilters(), now=run_time)

    failures = [event for event in events if isinstance(event, CompanyFailed)]
    assert [failure.company for failure in failures] == ["Broken"]

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.outcome is SyncOutcome.PARTIAL
    assert finished.added == 3
    assert listing.total_matching == 3


@respx.mock
async def test_closure_never_touches_a_board_that_failed(db_path: Path, run_time: datetime) -> None:
    from stage.domain import Platform

    acme = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))

    async with open_repository(db_path) as repository:
        await _run_sync(repository, [acme], run_time)

        respx.get(ENDPOINT).mock(return_value=httpx.Response(503))
        later = run_time + timedelta(days=1)
        events = await _run_sync(repository, [acme], later)
        listing = await list_jobs(repository, JobFilters(), now=later)

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.closed == 0
    assert listing.total_matching == 3


@respx.mock
async def test_a_tripped_ceiling_is_reported_not_swallowed(
    db_path: Path, run_time: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import Platform
    from stage.http import RatePosture
    from stage.services import sync as sync_module

    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=3, min_interval_s=0.0, max_requests_per_run=1),
    )
    boards = [
        Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme"),
        Company(name="Second", platform=Platform.GREENHOUSE, slug="second"),
        Company(name="Third", platform=Platform.GREENHOUSE, slug="third"),
    ]
    for slug in ("acme", "second", "third"):
        respx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").mock(
            return_value=httpx.Response(200, json=_payload())
        )

    async with open_repository(db_path) as repository:
        events = await _run_sync(repository, boards, run_time)

    failures = [event for event in events if isinstance(event, CompanyFailed)]
    assert len(failures) == 2
    assert all("ceiling" in failure.error for failure in failures)

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.outcome is SyncOutcome.PARTIAL


@respx.mock
async def test_the_default_window_hides_old_postings_until_all_is_passed(
    db_path: Path, acme: Company
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)

    async with open_repository(db_path) as repository:
        await _run_sync(repository, [acme], long_ago)
        now = datetime(2026, 7, 31, tzinfo=UTC)
        windowed = await list_jobs(repository, JobFilters(), now=now)
        everything = await list_jobs(repository, JobFilters(), window_days=None, now=now)

    assert windowed.total_matching == 0
    assert everything.total_matching == 3
