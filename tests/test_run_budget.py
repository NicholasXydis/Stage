from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.cli.runlock import AnotherRunInProgressError, single_run
from stage.http import CEILING_BACKSTOP, profile
from stage.services.sync import DAILY_RUNS, _daily_allowance, _reserve_for, _stalest_first
from stage.sources.workday import WorkdayAdapter

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def test_a_ceiling_rises_with_the_board_count() -> None:
    posture = profile("workday")
    small = posture.sized_for(50)
    large = posture.sized_for(600)
    assert large.max_requests_per_run > small.max_requests_per_run
    assert large.max_requests_per_run >= 600, "one request per board is the irreducible floor"


def test_a_derived_ceiling_never_passes_the_backstop() -> None:
    posture = profile("workday").sized_for(50_000)
    assert posture.max_requests_per_run == CEILING_BACKSTOP, (
        "growth raises the budget, but a registry bug must still be bounded"
    )


def test_sizing_never_lowers_a_shipped_ceiling() -> None:
    posture = profile("broad")
    assert posture.sized_for(1).max_requests_per_run == posture.max_requests_per_run


def test_sizing_a_source_with_no_boards_changes_nothing() -> None:
    posture = profile("standard")
    assert posture.sized_for(0) == posture


def test_the_conservative_tier_refreshes_less_often_than_the_rest() -> None:
    conservative = profile("conservative").refresh_interval_h
    for name in ("standard", "broad", "moderate", "paginated", "workday"):
        assert profile(name).refresh_interval_h < conservative


def test_every_window_stays_below_the_gap_between_scheduled_runs() -> None:
    from stage.cli.schedule import MAX_JITTER_S, SYNC_EVERY_HOURS

    shortest_gap_h = SYNC_EVERY_HOURS - MAX_JITTER_S / 3600
    for name in ("standard", "broad", "moderate", "paginated", "workday"):
        assert profile(name).refresh_interval_h < shortest_gap_h, (
            f"{name}: a window at or above the gap makes scheduled runs skip boards by accident"
        )


def test_the_conservative_window_still_allows_two_passes_a_day() -> None:
    from stage.cli.schedule import MAX_JITTER_S, SYNC_EVERY_HOURS

    gap_h = SYNC_EVERY_HOURS - MAX_JITTER_S / 3600
    assert profile("conservative").refresh_interval_h < 2 * gap_h


def test_feeds_are_never_windowed() -> None:
    assert profile("feeds").refresh_interval_h == 0.0, (
        "a feed is one request and updates continuously; windowing it only makes data stale"
    )


def test_an_empty_run_history_means_no_cap_not_a_cap_of_zero() -> None:
    posture = profile("standard")
    assert _daily_allowance(posture, 0, has_history=False) == posture.max_requests_per_run


def test_the_daily_cap_shrinks_the_run_once_the_day_is_mostly_spent() -> None:
    posture = profile("standard")
    budget = posture.max_requests_per_run
    almost_all = budget * DAILY_RUNS - 10
    assert _daily_allowance(posture, almost_all, has_history=True) == 10


def test_the_daily_cap_never_goes_negative() -> None:
    posture = profile("standard")
    spent = posture.max_requests_per_run * DAILY_RUNS * 5
    assert _daily_allowance(posture, spent, has_history=True) == 0


def test_a_fresh_day_leaves_the_whole_per_run_ceiling_available() -> None:
    posture = profile("standard")
    assert _daily_allowance(posture, 0, has_history=True) == posture.max_requests_per_run


def test_the_reserve_covers_retries_and_details() -> None:
    reserve = _reserve_for(WorkdayAdapter())
    assert reserve >= WorkdayAdapter.retry_reserve + WorkdayAdapter.detail_budget


def test_a_second_run_is_refused_and_names_the_first(tmp_path: Path) -> None:
    lock = tmp_path / "network.lock"

    def second() -> None:
        with single_run("sync", lock):
            pass

    with single_run("sync", lock), pytest.raises(AnotherRunInProgressError, match="already"):
        second()


def test_the_lock_is_released_when_the_run_finishes(tmp_path: Path) -> None:
    lock = tmp_path / "network.lock"
    owner = lock.with_suffix(".owner")
    with single_run("sync", lock):
        assert owner.exists(), "a held lock must name the run that holds it"
    assert not owner.exists(), "the holder record outlived the run that wrote it"
    with single_run("discover", lock):
        assert owner.exists(), "the lock was not reusable once the first run finished"


def test_the_lock_is_released_even_when_the_run_raises(tmp_path: Path) -> None:
    lock = tmp_path / "network.lock"
    with pytest.raises(ValueError), single_run("sync", lock):
        raise ValueError("boom")
    with single_run("sync", lock):
        pass


def test_tighten_can_establish_an_interval_from_zero() -> None:
    from stage.http import HostBudget, RatePosture

    budget = HostBudget(posture=RatePosture(concurrency=1, min_interval_s=0.0))
    budget.tighten(2.0, rejected=True)
    assert budget.min_interval_s > 0.0, (
        "a multiply with no floor makes zero a fixed point and the control silently inert"
    )


def test_a_clamped_run_fetches_the_stalest_boards_first() -> None:
    from stage.domain import Company, Platform

    class _Adapter:
        name = "greenhouse"

        def board_key(self, company: Company) -> str:
            return f"greenhouse:{company.slug}"

    rows = [
        Company(name="fresh", platform=Platform.GREENHOUSE, slug="fresh"),
        Company(name="stale", platform=Platform.GREENHOUSE, slug="stale"),
        Company(name="new", platform=Platform.GREENHOUSE, slug="new"),
    ]
    last_success = {
        "greenhouse:fresh": NOW,
        "greenhouse:stale": NOW - timedelta(days=3),
    }
    ordered = _stalest_first(_Adapter(), rows, last_success)  # type: ignore[arg-type]

    assert [company.slug for company in ordered] == ["new", "stale", "fresh"], (
        "a never-visited board leads, then the most out-of-date, so a clamped run spends well"
    )


@respx.mock
async def test_the_daily_cap_actually_reaches_the_budget_that_spends(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import Company, CompanyFinished, Platform, SourceCapped
    from stage.services import sync as sync_module
    from stage.storage import open_repository

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    monkeypatch.setattr(sync_module, "_daily_allowance", lambda *_, **__: 1)

    boards = [
        Company(name=f"Board {index}", platform=Platform.GREENHOUSE, slug=f"board{index}")
        for index in range(4)
    ]
    async with open_repository(db_path) as repository:
        events = [
            event
            async for event in sync_module.sync(
                repository, boards, sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]

    capped = [event for event in events if isinstance(event, SourceCapped)]
    fetched = [event for event in events if isinstance(event, CompanyFinished)]
    assert capped, "an allowance below the ceiling must be reported"
    assert len(fetched) <= 1, (
        "the allowance has to reach the budget that counts requests, or the cap is decorative"
    )


def test_a_broken_registry_is_reported_even_while_another_run_holds_the_lock(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    registry = tmp_path / "companies.yaml"
    registry.write_text(
        "- {name: A, platform: greenhouse, slug: a, last_verified: nope}\n", encoding="utf-8"
    )
    lock = tmp_path / "network.lock"
    with single_run("sync", lock):
        result = CliRunner().invoke(
            app,
            ["sync", "--registry", str(registry), "--db", str(tmp_path / "s.db")],
        )

    assert result.exit_code == 2
    assert "last_verified" in result.stdout, (
        "a config error needs no network, so the lock must never mask it"
    )


def test_two_databases_do_not_contend_for_one_lock(tmp_path: Path) -> None:
    from stage.cli.options import _lock_path

    first = _lock_path(tmp_path / "one.db")
    second = _lock_path(tmp_path / "two.db")
    assert first != second, "independent databases are independent runs"
    with single_run("sync", first), single_run("sync", second):
        pass


@respx.mock
async def test_our_own_exhausted_budget_is_never_recorded_as_a_board_failing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import Company, CompanyDeferred, CompanyFailed, Platform
    from stage.services import sync as sync_module
    from stage.storage import open_repository

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    monkeypatch.setattr(sync_module, "_daily_allowance", lambda *_, **__: 1)

    boards = [
        Company(name=f"Board {index}", platform=Platform.GREENHOUSE, slug=f"board{index}")
        for index in range(4)
    ]
    async with open_repository(db_path) as repository:
        events = [
            event
            async for event in sync_module.sync(
                repository, boards, sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]
        visits = {visit.board: visit for visit in await repository.all_visits()}

    deferred = [event for event in events if isinstance(event, CompanyDeferred)]
    failed = [event for event in events if isinstance(event, CompanyFailed)]
    assert deferred, "a board the run declined to request must be reported as deferred"
    assert not failed, "running out of our own budget is not evidence that a board failed"
    starved = {event.company.lower().replace(" ", "") for event in deferred}
    recorded = {board.split(":")[-1] for board in visits}
    assert not (starved & recorded), (
        "a board we never sent a request to must leave no visit row, or it reads as failing"
    )
    assert all(visit.consecutive_failures == 0 for visit in visits.values()), (
        "our own budget running out must never count against a board's failure streak"
    )


def test_the_daily_cap_spans_every_bucket_a_source_fetches() -> None:
    from stage.http.profiles import profile
    from stage.services.sync import DAILY_RUNS, _daily_allowance

    posture = profile("conservative")
    per_bucket = posture.max_requests_per_run
    spent = per_bucket * DAILY_RUNS * 3

    single = _daily_allowance(posture, spent, True, buckets=1)
    assert single == 0, "one bucket's own daily cap must still bind"

    spread = _daily_allowance(posture, spent, True, buckets=40)
    assert spread == per_bucket, (
        "a source spanning many hosts must not charge every bucket's spend to one budget"
    )


def test_a_single_bucket_source_is_unchanged_by_the_span() -> None:
    from stage.http.profiles import profile
    from stage.services.sync import _daily_allowance

    posture = profile("broad")
    spent = posture.max_requests_per_run
    assert _daily_allowance(posture, spent, True) == _daily_allowance(
        posture, spent, True, buckets=1
    ), "the default must behave exactly as a one-bucket source"


async def test_more_than_sixteen_runs_in_a_day_still_count(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import SourceRunStats, SyncOutcome, SyncRun
    from stage.services.sync import _spent_today
    from stage.storage import open_repository

    now = datetime.now(UTC)
    async with open_repository(tmp_path / "budget.db") as repository:
        for index in range(25):
            started = now - timedelta(minutes=25 - index)
            await repository.record_sync_run(
                SyncRun(
                    started_at=started,
                    finished_at=started + timedelta(seconds=30),
                    outcome=SyncOutcome.SUCCESS,
                    sources=(SourceRunStats(source="greenhouse", requests=10),),
                )
            )
        spent, seen = await _spent_today(repository, now - timedelta(hours=24))

    assert seen
    assert spent["greenhouse"] == 250


async def test_yesterdays_requests_do_not_count_against_today(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import SourceRunStats, SyncOutcome, SyncRun
    from stage.services.sync import _spent_today
    from stage.storage import open_repository

    now = datetime.now(UTC)
    async with open_repository(tmp_path / "old.db") as repository:
        for offset in (timedelta(days=2), timedelta(minutes=5)):
            started = now - offset
            await repository.record_sync_run(
                SyncRun(
                    started_at=started,
                    finished_at=started + timedelta(seconds=30),
                    outcome=SyncOutcome.SUCCESS,
                    sources=(SourceRunStats(source="greenhouse", requests=7),),
                )
            )
        spent, _ = await _spent_today(repository, now - timedelta(hours=24))

    assert spent["greenhouse"] == 7
