from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.http import CEILING_BACKSTOP, profile
from stage.runlock import AnotherRunInProgressError, single_run
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


def test_every_window_stays_below_the_gap_of_a_six_hour_schedule() -> None:
    from stage.cli.schedule import MAX_JITTER_S
    from stage.http import REFRESH_SLACK_H

    shortest_gap_h = 6 - MAX_JITTER_S / 3600
    for name in ("standard", "broad", "moderate", "paginated", "workday"):
        assert profile(name).refresh_interval_h - REFRESH_SLACK_H < shortest_gap_h, (
            f"{name}: a window at or above the gap makes scheduled runs skip boards by accident"
        )


def test_a_quiet_board_is_never_read_less_often_than_every_six_hours() -> None:
    from stage.cli.schedule import JITTER_SHARE, SYNC_EVERY_MINUTES
    from stage.http import REFRESH_SLACK_H

    tick_h = SYNC_EVERY_MINUTES / 60
    jitter_h = tick_h * JITTER_SHARE
    threshold = profile("standard").refresh_interval_h - REFRESH_SLACK_H
    ticks = next(count for count in range(1, 100) if count * tick_h - jitter_h >= threshold)
    assert ticks * tick_h + jitter_h <= 6.0


def test_the_conservative_window_still_allows_two_passes_a_day() -> None:
    from stage.cli.schedule import MAX_JITTER_S

    gap_h = 6 - MAX_JITTER_S / 3600
    assert profile("conservative").refresh_interval_h < 2 * gap_h


def test_a_busy_board_is_read_on_the_hourly_tick_of_a_half_hour_schedule() -> None:
    from stage.cli.schedule import JITTER_SHARE, SYNC_EVERY_MINUTES
    from stage.http import BUSY_REFRESH_H, REFRESH_SLACK_H

    tick_h = SYNC_EVERY_MINUTES / 60
    jitter_h = tick_h * JITTER_SHARE
    assert tick_h + jitter_h < BUSY_REFRESH_H - REFRESH_SLACK_H < 2 * tick_h - jitter_h


def test_searches_rest_four_hours_between_reads() -> None:
    assert profile("searches").refresh_interval_h == 4.0
    assert profile("jobbank").refresh_interval_h == 4.0
    assert profile("searches").refresh_interval_h < profile("standard").refresh_interval_h


def test_a_boards_window_follows_its_own_new_internships() -> None:
    standard = profile("standard")
    windows = [standard.window(month, 0) for month in (0, 1, 2, 3, 5, 11, 40)]
    assert windows == [5.5, 5.5, 4.0, 3.0, 2.0, 1.0, 1.0]
    assert standard.window(1, 1) == 1.0, "a new internship this week means hourly"
    assert standard.window(0, 0, followed=True) == 1.0, "a followed company is hourly"


def test_a_fragile_host_is_never_read_faster_than_every_three_hours() -> None:
    conservative = profile("conservative")
    assert conservative.window(40, 5) == 3.0
    assert conservative.window(0, 0) == 10.0
    assert profile("standard").strictest(conservative).busy_refresh_h == 3.0


def test_a_feed_is_read_every_run_whatever_its_activity() -> None:
    assert profile("feeds").window(0, 0) == 0.0
    assert profile("feeds").window(9, 9, followed=True) == 0.0


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


def test_a_waiting_run_starts_once_the_holder_finishes(tmp_path: Path) -> None:
    from contextlib import ExitStack

    lock = tmp_path / "network.lock"
    held = ExitStack()
    held.enter_context(single_run("sync", lock))
    told: list[str] = []
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        held.close()

    with single_run("rescreen", lock, wait_s=60, waiting=told.append, sleep=nap):
        pass

    assert len(naps) == 1
    assert len(told) == 1 and "stage sync" in told[0]


def test_a_waiting_run_gives_up_at_its_deadline(tmp_path: Path) -> None:
    lock = tmp_path / "network.lock"
    clock = iter([0.0, 0.0, 5.0, 11.0, 11.0])

    waiting = single_run("purge", lock, wait_s=10, sleep=lambda _: None, clock=lambda: next(clock))
    with (
        single_run("sync", lock),
        pytest.raises(AnotherRunInProgressError, match="already"),
        waiting,
    ):
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
    assert "last_verified" in result.stderr, (
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


@respx.mock
async def test_a_board_the_open_breaker_refused_is_deferred_not_failed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import Company, CompanyDeferred, CompanyFailed, Platform
    from stage.http import RatePosture
    from stage.services import sync as sync_module
    from stage.storage import open_repository

    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        side_effect=httpx.ConnectError("down")
    )
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_, **__: RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=300),
    )
    boards = [
        Company(name=f"Board {index}", platform=Platform.GREENHOUSE, slug=f"board{index}")
        for index in range(8)
    ]
    async with open_repository(db_path) as repository:
        events = [
            event
            async for event in sync_module.sync(
                repository, boards, sources=["greenhouse"], now_fn=lambda: NOW
            )
        ]
        visits = await repository.all_visits()

    failed = [event for event in events if isinstance(event, CompanyFailed)]
    deferred = [event for event in events if isinstance(event, CompanyDeferred)]
    assert failed and all("could not connect" in event.error for event in failed)
    assert deferred and all("circuit breaker" in event.reason for event in deferred)
    assert not [visit for visit in visits if visit.consecutive_failures]


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
    from stage.services.sync import _pacing
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
        pacing = await _pacing(repository, now)

    assert pacing.has_history
    assert pacing.spent["greenhouse"] == 250


async def test_yesterdays_requests_do_not_count_against_today(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import SourceRunStats, SyncOutcome, SyncRun
    from stage.services.sync import _pacing
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
        pacing = await _pacing(repository, now)

    assert pacing.spent["greenhouse"] == 7


async def test_an_unchanged_answer_costs_nothing_and_pushback_is_remembered(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import SourceRunStats, SyncOutcome, SyncRun
    from stage.services.sync import _pacing
    from stage.storage import open_repository

    now = datetime.now(UTC)
    async with open_repository(tmp_path / "pace.db") as repository:
        await repository.record_sync_run(
            SyncRun(
                started_at=now - timedelta(minutes=5),
                finished_at=now,
                outcome=SyncOutcome.SUCCESS,
                sources=(
                    SourceRunStats(source="greenhouse", requests=10, not_modified=8),
                    SourceRunStats(source="workday", requests=10, tightenings=1),
                    SourceRunStats(source="lever", requests=10, retries=1),
                ),
            )
        )
        pacing = await _pacing(repository, now)

    assert pacing.spent["greenhouse"] == 2
    assert pacing.pushback == {"workday", "lever"}
    assert pacing.pace("greenhouse") == 2
    assert pacing.pace("workday") == 1


def test_a_clean_day_doubles_the_daily_cap_but_never_the_run() -> None:
    posture = profile("standard")
    per_run = posture.max_requests_per_run
    spent = per_run * DAILY_RUNS
    assert _daily_allowance(posture, spent, True) == 0
    assert _daily_allowance(posture, spent, True, pace=2) == per_run
    assert _daily_allowance(posture, 0, True, pace=2) == per_run


def test_the_daily_cap_holds_every_quiet_read_of_six_hundred_workday_boards() -> None:
    posture = profile("workday").sized_for(604, 100)
    day = round(604 * posture.requests_per_board * 24 / posture.refresh_interval_h * 2.5)
    assert day == 13178
    assert _daily_allowance(posture, CEILING_BACKSTOP * 2, True, boards=604) > 0, (
        "the old 4,000 left runs 5 to 8 of a soak with no Workday requests"
    )
    assert _daily_allowance(posture, day - 10, True, boards=604) == 10
    assert _daily_allowance(posture, day, True, boards=604) == 0


def test_pushback_halves_the_board_sized_cap_and_a_clean_day_doubles_it() -> None:
    posture = profile("workday").sized_for(604, 100)
    assert _daily_allowance(posture, 14_000, True, boards=604, pace=1) == 0
    assert _daily_allowance(posture, 14_000, True, boards=604, pace=2) == (
        posture.max_requests_per_run
    )


def test_a_small_source_keeps_its_run_based_cap() -> None:
    posture = profile("standard")
    spent = posture.max_requests_per_run * DAILY_RUNS
    assert _daily_allowance(posture, spent, True, boards=20) == 0
    feeds = profile("feeds")
    assert _daily_allowance(feeds, feeds.max_requests_per_run * DAILY_RUNS, True, boards=5) == 0


async def test_a_board_with_a_new_internship_this_week_is_busy(tmp_path: Path) -> None:
    from dataclasses import replace

    from stage.domain import Company, Job, Platform
    from stage.services.sync import _pacing
    from stage.sources import adapter_for_platform
    from stage.storage import SourceBatch, open_repository

    now = datetime.now(UTC)
    backlog_at, news_at = now - timedelta(days=9), now - timedelta(days=2)
    posting = Job(
        id="greenhouse:acme:1",
        source="greenhouse",
        company="Acme",
        title_raw="Software Engineering Intern",
        title_normalized="software engineering intern",
        apply_url_raw="",
        description="",
        first_seen=backlog_at,
        last_seen=backlog_at,
    )
    fresh = replace(posting, id="greenhouse:acme:2", first_seen=news_at, last_seen=news_at)
    async with open_repository(tmp_path / "busy.db") as repository:
        for moment, job in ((backlog_at, posting), (news_at, fresh)):
            await repository.apply_source_batch(
                SourceBatch(source="greenhouse", run_started_at=moment, jobs=(job,))
            )
        pacing = await _pacing(repository, now)

    assert pacing.activity == {"greenhouse:acme": (1, 1)}, "the first batch is backlog"
    greenhouse = adapter_for_platform(Platform.GREENHOUSE)
    lever = adapter_for_platform(Platform.LEVER)
    assert greenhouse is not None and lever is not None
    acme = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    assert pacing.window(greenhouse, acme, {}) == 1.0
    assert pacing.window(lever, replace(acme, platform=Platform.LEVER), {}) == 5.5


def test_a_lock_held_by_another_program_is_not_blamed_on_stage(tmp_path: Path) -> None:
    from stage.runlock import _try_lock

    lock = tmp_path / "network.lock"
    with lock.open("a+b") as handle:
        assert _try_lock(handle)
        with pytest.raises(AnotherRunInProgressError) as caught, single_run("sync", lock):
            pass

    assert str(caught.value) == (
        f"Another program holding {lock} is already running. Run stage sync again when it finishes."
    )
    assert caught.value.holder == f"another program holding {lock}"


def test_a_held_lock_names_its_holder_and_when_it_started(tmp_path: Path) -> None:
    import re

    lock = tmp_path / "network.lock"
    with (
        single_run("sync", lock),
        pytest.raises(AnotherRunInProgressError) as caught,
        single_run("purge", lock, busy="press s again when it finishes"),
    ):
        pass

    said = str(caught.value)
    assert re.fullmatch(
        r"stage sync \(pid \d+, started \d{4}-\d{2}-\d{2} \d{2}:\d{2}\) is already running\. "
        r"Press s again when it finishes\.",
        said,
    ), said
    assert ";" not in said


def test_a_lock_that_cannot_be_opened_says_where_and_what_to_check(tmp_path: Path) -> None:
    folder = tmp_path / "network.lock"
    folder.mkdir()
    with pytest.raises(AnotherRunInProgressError) as caught, single_run("sync", folder):
        pass
    said = str(caught.value)
    assert said.startswith(f"Stage could not open its run lock at {folder} (")
    assert said.endswith(f"Check that {tmp_path} is writable.")
