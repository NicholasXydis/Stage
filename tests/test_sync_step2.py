import json
import random
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from stage.domain import (
    Company,
    CompanyUnchanged,
    JobFilters,
    JobStatus,
    PlannedRequest,
    Platform,
    RequestLogged,
    SourceFailed,
    SourceFinished,
    SourceRunStats,
    SourceStarted,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    UnroutableCompanies,
)
from stage.http import RatePosture
from stage.services import sync as sync_module
from stage.services.query import list_jobs
from stage.services.sync import sync
from stage.storage import AsyncRepository, open_repository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
VALIDATED = {"ETag": '"board-v1"'}


def _payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=300),
    )


async def _run(
    repository: AsyncRepository,
    companies: Sequence[Company],
    moment: datetime,
    *,
    dry_run: bool = False,
) -> list[SyncEvent]:
    return [
        event
        async for event in sync(
            repository,
            companies,
            sources=["greenhouse"],
            dry_run=dry_run,
            now_fn=lambda: moment,
            rng=random.Random(1),
        )
    ]


@respx.mock
async def test_the_second_sync_sends_a_conditional_request_and_skips_the_body(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_payload(), headers=VALIDATED)
    )

    async with open_repository(db_path) as repository:
        await _run(repository, [acme], run_time)

        route.mock(return_value=httpx.Response(304))
        later = run_time + timedelta(days=1)
        events = await _run(repository, [acme], later)
        listing = await list_jobs(repository, JobFilters(), now=later)

    assert route.calls[1].request.headers["if-none-match"] == '"board-v1"'
    assert any(isinstance(event, CompanyUnchanged) for event in events)

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.not_modified == 1
    assert finished.requests == 1
    assert listing.total_matching == 3


@respx.mock
async def test_an_unchanged_board_never_closes_its_postings(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload(), headers=VALIDATED))

    async with open_repository(db_path) as repository:
        await _run(repository, [acme], run_time)

        respx.get(ENDPOINT).mock(return_value=httpx.Response(304))
        later = run_time + timedelta(days=1)
        events = await _run(repository, [acme], later)

        still_open = await list_jobs(repository, JobFilters(), now=later)
        job = await repository.get_job("greenhouse:acme:4012345")

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.closed == 0
    assert still_open.total_matching == 3
    assert job is not None
    assert job.status is JobStatus.OPEN
    assert job.last_seen == later


@respx.mock
async def test_the_cache_cannot_outlive_the_database(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload(), headers=VALIDATED))

    async with open_repository(db_path) as repository:
        await _run(repository, [acme], run_time)
        assert await repository.cached_url_count() == 1

    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        companion = db_path.with_name(db_path.name + suffix)
        if companion.exists():
            companion.unlink()

    async with open_repository(db_path) as repository:
        assert await repository.cached_url_count() == 0
        later = run_time + timedelta(days=1)
        events = await _run(repository, [acme], later)
        listing = await list_jobs(repository, JobFilters(), now=later)

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.not_modified == 0
    assert finished.added == 3
    assert listing.total_matching == 3


@respx.mock
async def test_dry_run_sends_nothing_and_writes_nothing(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))

    async with open_repository(db_path) as repository:
        events = await _run(repository, [acme], run_time, dry_run=True)
        listing = await list_jobs(repository, JobFilters(), now=run_time)
        last_sync = await repository.last_sync_at()

    assert route.call_count == 0
    assert listing.total_matching == 0
    assert last_sync is None

    planned = [event for event in events if isinstance(event, PlannedRequest)]
    assert len(planned) == 1
    assert planned[0].url == f"{ENDPOINT}?content=true"
    assert planned[0].has_validator is False

    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.dry_run is True


@respx.mock
async def test_dry_run_annotates_from_local_state_only(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_payload(), headers=VALIDATED)
    )

    async with open_repository(db_path) as repository:
        await _run(repository, [acme], run_time)
        calls_after_real_sync = route.call_count

        events = await _run(repository, [acme], run_time, dry_run=True)

    assert route.call_count == calls_after_real_sync

    planned = [event for event in events if isinstance(event, PlannedRequest)]
    assert planned[0].has_validator is True
    assert "likely 304" in planned[0].expectation


@respx.mock
async def test_company_order_is_shuffled_per_run(db_path: Path, run_time: datetime) -> None:
    boards = [
        Company(name=f"Board{index}", platform=Platform.GREENHOUSE, slug=f"board{index}")
        for index in range(8)
    ]
    for company in boards:
        respx.get(f"https://boards-api.greenhouse.io/v1/boards/{company.slug}/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )

    async def order(seed: int) -> list[str]:
        async with open_repository(db_path) as repository:
            events = [
                event
                async for event in sync(
                    repository,
                    boards,
                    sources=["greenhouse"],
                    dry_run=True,
                    now_fn=lambda: run_time,
                    rng=random.Random(seed),
                )
            ]
        return [event.company for event in events if isinstance(event, PlannedRequest)]

    first = await order(1)
    second = await order(7)
    repeat = await order(1)

    assert sorted(first) == sorted(second) == sorted(company.name for company in boards)
    assert first != second
    assert first == repeat


@respx.mock
async def test_every_request_is_auditable(db_path: Path, acme: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload(), headers=VALIDATED))

    async with open_repository(db_path) as repository:
        events = await _run(repository, [acme], run_time)

    logged = [event for event in events if isinstance(event, RequestLogged)]
    assert len(logged) == 1
    assert logged[0].status == 200
    assert logged[0].url.startswith(ENDPOINT)
    assert logged[0].attempt == 1
    assert logged[0].error == ""


@respx.mock
async def test_metrics_reach_the_source_summary(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_payload(), headers=VALIDATED))

    async with open_repository(db_path) as repository:
        events = await _run(repository, [acme], run_time)

    summary = next(event for event in events if isinstance(event, SourceFinished))
    assert summary.requests == 1
    assert summary.not_modified == 0
    assert summary.latency_p50_ms >= 0.0
    assert summary.latency_p95_ms >= summary.latency_p50_ms


async def test_dry_run_fails_on_a_fault_that_needs_no_network_to_see(
    db_path: Path, run_time: datetime
) -> None:
    companies = (
        Company(name="Faire", platform=Platform.GREENHOUSE, slug="faire"),
        Company(name="Stranded", platform=Platform.TEAMTAILOR, slug="stranded"),
    )
    events = []
    async with open_repository(db_path) as repository:
        async for event in sync(
            repository, companies, sources=["greenhouse"], dry_run=True, now_fn=lambda: run_time
        ):
            events.append(event)

    unroutable = [event for event in events if isinstance(event, UnroutableCompanies)]
    assert [event.platforms for event in unroutable] == [("teamtailor",)]
    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.dry_run is True

    assert finished.outcome is SyncOutcome.PARTIAL

async def test_a_source_stream_failure_does_not_stop_other_sources() -> None:
    async def broken() -> AsyncIterator[SyncEvent]:
        yield SourceStarted(source="broken", companies=0)
        raise RuntimeError("source setup failed")

    async def healthy() -> AsyncIterator[SyncEvent]:
        yield SourceStarted(source="healthy", companies=0)
        yield SourceFinished(
            source="healthy",
            fetched=0,
            added=0,
            updated=0,
            closed=0,
            failed_companies=0,
            elapsed_ms=0.0,
        )

    failed: list[str] = []
    stats: list[SourceRunStats] = []
    events = [
        event
        async for event in sync_module._merge(
            [("broken", broken()), ("healthy", healthy())], failed, stats
        )
    ]

    assert any(isinstance(event, SourceFailed) for event in events)
    assert any(isinstance(event, SourceFinished) for event in events)
    assert failed == ["broken"]
    assert stats == [SourceRunStats(source="broken", errors=1)]


async def test_dry_run_stays_green_when_every_enabled_row_is_routable(
    db_path: Path, run_time: datetime
) -> None:
    companies = (
        Company(name="Faire", platform=Platform.GREENHOUSE, slug="faire"),
        Company(name="Stranded", platform=Platform.TEAMTAILOR, slug="stranded", enabled=False),
    )
    events = []
    async with open_repository(db_path) as repository:
        async for event in sync(
            repository, companies, sources=["greenhouse"], dry_run=True, now_fn=lambda: run_time
        ):
            events.append(event)

    assert not [event for event in events if isinstance(event, UnroutableCompanies)]
    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    assert finished.outcome is SyncOutcome.SUCCESS


@respx.mock
async def test_a_validation_failure_does_not_persist_its_validator(
    db_path: Path, acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [{"id": "not-an-int", "title": "x", "absolute_url": "u"}]},
            headers={"ETag": '"v1"'},
        )
    )
    async with open_repository(db_path) as repository:
        async for _ in sync(repository, [acme], sources=["greenhouse"], now_fn=lambda: run_time):
            pass
        assert dict(await repository.load_validators("greenhouse")) == {}


def test_excluding_a_source_drops_it_and_keeps_the_rest() -> None:
    from stage.services.sync import _select

    rows = (
        Company(name="A", platform=Platform.GREENHOUSE, slug="a"),
        Company(name="B", platform=Platform.LEVER, slug="b"),
    )
    grouped, feeds, _ = _select(rows, None, ["greenhouse", "simplify"])
    assert set(grouped) == {"lever"}
    assert "simplify" not in feeds
    assert "vanshb03" in feeds


def test_excluding_an_unknown_source_is_an_error_not_a_silent_no_op() -> None:
    from stage.services.sync import NoSourcesSelectedError, _select

    rows = (Company(name="A", platform=Platform.GREENHOUSE, slug="a"),)
    with pytest.raises(NoSourcesSelectedError, match="unknown source"):
        _select(rows, None, ["greehnouse"])


def test_excluding_a_source_with_no_enabled_rows_still_works() -> None:
    from stage.services.sync import _select

    rows = (Company(name="A", platform=Platform.GREENHOUSE, slug="a"),)
    grouped, _, _ = _select(rows, None, ["workday"])
    assert set(grouped) == {"greenhouse"}
