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
