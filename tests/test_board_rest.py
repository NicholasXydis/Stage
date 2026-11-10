from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import (
    REST_AFTER_FAILURES,
    Company,
    CompanyFinished,
    Platform,
    SourceResting,
    SourceVisit,
    is_resting,
    rested_until,
)
from stage.services import sync as sync_module
from stage.storage import open_repository
from stage.storage.repository import SourceBatch

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _visit(failures: int, attempted: datetime = NOW) -> SourceVisit:
    return SourceVisit(
        source="greenhouse",
        board="greenhouse:acme",
        last_attempt_at=attempted,
        consecutive_failures=failures,
    )


def test_a_board_below_the_threshold_never_rests() -> None:
    for failures in range(REST_AFTER_FAILURES):
        assert rested_until(_visit(failures)) is None, (
            "one bad answer is transport noise, not a reason to stop asking"
        )


def test_the_wait_doubles_with_each_further_failure() -> None:
    waits = [rested_until(_visit(REST_AFTER_FAILURES + step)) for step in range(4)]
    assert all(wait is not None for wait in waits)
    gaps = [wait - NOW for wait in waits if wait is not None]
    assert gaps == sorted(gaps), "backoff must not shrink as failures accumulate"
    assert gaps[1] == gaps[0] * 2, "the wait doubles on each further failure"


def test_the_wait_is_capped_so_a_board_is_never_abandoned() -> None:
    forever = rested_until(_visit(200))
    assert forever is not None
    assert forever - NOW <= timedelta(days=7), (
        "an uncapped backoff would retire a board that may come back"
    )


def test_rest_expires_on_its_own() -> None:
    visit = _visit(REST_AFTER_FAILURES)
    assert is_resting(visit, NOW), "a board that just failed again should wait"
    assert not is_resting(visit, NOW + timedelta(days=8)), (
        "the board must be retried once the wait elapses, with no human action"
    )


def test_one_success_clears_the_backoff() -> None:
    healed = SourceVisit(
        source="greenhouse",
        board="greenhouse:acme",
        last_attempt_at=NOW,
        last_success_at=NOW,
        consecutive_failures=0,
    )
    assert not is_resting(healed, NOW), "a success must put the board straight back in rotation"


@respx.mock
async def test_a_resting_board_is_skipped_and_reported(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import CompanyVisit

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    boards = [
        Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme"),
        Company(name="Beta", platform=Platform.GREENHOUSE, slug="beta"),
    ]

    async with open_repository(db_path) as repository:
        for _ in range(REST_AFTER_FAILURES):
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=NOW,
                    visits=(
                        CompanyVisit(board="greenhouse:acme", succeeded=False, error="HTTP 500"),
                    ),
                )
            )
        events = [
            event
            async for event in sync_module.sync(
                repository, boards, sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]

    rested = [event for event in events if isinstance(event, SourceResting)]
    fetched = {event.company for event in events if isinstance(event, CompanyFinished)}
    assert rested and rested[0].skipped == 1, "the failing board was fetched again anyway"
    assert fetched == {"Beta"}, "resting one board must not stop the healthy ones"


@respx.mock
async def test_failures_stage_s_own_circuit_breaker_caused_never_rest_a_board(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import CompanyVisit

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    board = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    skipped = "BreakerOpenError: workday circuit breaker is open after 6 consecutive failures"

    async with open_repository(db_path) as repository:
        for _ in range(REST_AFTER_FAILURES + 8):
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=NOW,
                    visits=(CompanyVisit(board="greenhouse:acme", succeeded=False, error=skipped),),
                )
            )
        events = [
            event
            async for event in sync_module.sync(
                repository, [board], sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]

    assert {event.company for event in events if isinstance(event, CompanyFinished)} == {"Acme"}


@respx.mock
@pytest.mark.parametrize(("refused_before", "quiet"), [(REST_AFTER_FAILURES - 1, True), (0, False)])
async def test_a_board_that_keeps_refusing_this_computer_stops_failing_the_run(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, refused_before: int, quiet: bool
) -> None:
    from stage.domain import CompanyFailed, CompanyVisit, SyncFinished, SyncOutcome

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/acme/.*").mock(
        return_value=httpx.Response(403)
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    boards = [Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")]
    refusal = "boards-api.greenhouse.io returned 403 (forbidden)"

    async with open_repository(db_path) as repository:
        for _ in range(refused_before):
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=NOW - timedelta(days=30),
                    visits=(CompanyVisit(board="greenhouse:acme", succeeded=False, error=refusal),),
                )
            )
        events = [
            event
            async for event in sync_module.sync(
                repository, boards, sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]

    failed = [event for event in events if isinstance(event, CompanyFailed)]
    finished = next(event for event in events if isinstance(event, SyncFinished))
    assert [event.refused for event in failed] == [quiet]
    assert (finished.outcome is SyncOutcome.SUCCESS) is quiet
    assert (finished.partial_reason == "") is quiet


@respx.mock
@pytest.mark.parametrize(("missing_before", "quiet"), [(REST_AFTER_FAILURES - 1, True), (0, False)])
async def test_a_board_that_keeps_answering_not_found_stops_failing_the_run(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, missing_before: int, quiet: bool
) -> None:
    from stage.domain import CompanyFailed, CompanyVisit, SyncFinished, SyncOutcome

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/acme/.*").mock(
        return_value=httpx.Response(404)
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    boards = [Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")]
    missing = "boards-api.greenhouse.io returned 404 (not found)"

    async with open_repository(db_path) as repository:
        for _ in range(missing_before):
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=NOW - timedelta(days=30),
                    visits=(CompanyVisit(board="greenhouse:acme", succeeded=False, error=missing),),
                )
            )
        events = [
            event
            async for event in sync_module.sync(
                repository, boards, sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]

    failed = [event for event in events if isinstance(event, CompanyFailed)]
    finished = next(event for event in events if isinstance(event, SyncFinished))
    assert [(event.gone, event.refused) for event in failed] == [(quiet, False)]
    assert (finished.outcome is SyncOutcome.SUCCESS) is quiet


@pytest.mark.parametrize(
    ("failures", "error", "gone"),
    [
        (REST_AFTER_FAILURES, "boards-api.greenhouse.io returned 404 (not found)", True),
        (REST_AFTER_FAILURES, "api.lever.co returned 410 (gone)", True),
        (REST_AFTER_FAILURES - 1, "boards-api.greenhouse.io returned 404 (not found)", False),
        (REST_AFTER_FAILURES, "jobs.aon.com returned 403 (forbidden)", False),
    ],
)
def test_a_board_that_answers_not_found_three_times_is_gone(
    failures: int, error: str, gone: bool
) -> None:
    from dataclasses import replace

    from stage.domain import VisitState, classify_visit

    visit = replace(_visit(failures), last_error=error)

    assert visit.gone is gone
    state = classify_visit(None, failures, NOW, refusing=visit.refusing, gone=visit.gone)
    assert (state is VisitState.GONE) is gone


@pytest.mark.parametrize(
    ("failures", "error", "refusing"),
    [
        (REST_AFTER_FAILURES, "jobs.aon.com returned 403 (forbidden)", True),
        (REST_AFTER_FAILURES, "apply.careers.microsoft.com returned 429 (too many requests)", True),
        (REST_AFTER_FAILURES - 1, "jobs.aon.com returned 403 (forbidden)", False),
        (REST_AFTER_FAILURES, "boards-api.greenhouse.io returned 404 (not found)", False),
    ],
)
def test_a_board_refuses_this_computer_after_repeated_403_or_429(
    failures: int, error: str, refusing: bool
) -> None:
    from dataclasses import replace

    from stage.domain import VisitState, classify_visit

    visit = replace(_visit(failures), last_error=error)

    assert visit.refusing is refusing
    state = classify_visit(None, failures, NOW, refusing=visit.refusing)
    assert (state is VisitState.REFUSING) is refusing
