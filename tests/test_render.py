import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console

from stage.cli.render.health import (
    render_board_health,
    render_canary,
    render_doctor,
    render_rate_state,
    render_repairs,
    render_stats,
    render_workday_crawl_progress,
)
from stage.domain import (
    IntegrityFinding,
    IntegrityRepair,
    RateState,
    VisitState,
    VolumeVerdict,
)
from stage.domain.health import VolumeSignal
from stage.domain.workday import WorkdayCrawl
from stage.services.health import BoardHealth, DoctorReport, SourceHealth, StatsReport

if TYPE_CHECKING:
    from stage.domain import SyncEvent
    from stage.services.canary import BoardProbe

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _paint(render: Callable[..., None], *args: Any, width: int = 100, **kwargs: Any) -> str:
    buffer = io.StringIO()
    render(Console(file=buffer, width=width, emoji=False), *args, **kwargs)
    return buffer.getvalue()


def _volume(verdict: VolumeVerdict = VolumeVerdict.UNPROVEN) -> VolumeSignal:
    return VolumeSignal(
        source="greenhouse",
        verdict=verdict,
        latest=10,
        baseline=10.0,
        samples=3,
        detail="volume dropped sharply",
    )


def _board(state: VisitState, error: str = "") -> BoardHealth:
    return BoardHealth(
        source="greenhouse",
        board="greenhouse:acme",
        label="Acme",
        state=state,
        last_success_at=None if state is VisitState.FAILING else NOW,
        consecutive_failures=4 if state is VisitState.FAILING else 0,
        last_error=error,
    )


def _source(**overrides: object) -> SourceHealth:
    defaults: dict[str, object] = {
        "source": "greenhouse",
        "stored": 12,
        "volume": _volume(),
        "requests": 30,
        "not_modified": 4,
        "latency_p50_ms": 120.0,
        "latency_p95_ms": 900.0,
        "errors": 0,
        "tightenings": 0,
        "deferred": 0,
        "blocked": False,
    }
    defaults.update(overrides)
    return SourceHealth(**defaults)  # type: ignore[arg-type]


def _report(**overrides: object) -> DoctorReport:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "last_sync_at": NOW - timedelta(hours=2),
        "integrity": (),
        "sources": (_source(),),
        "blocks": (),
        "never_synced": False,
    }
    defaults.update(overrides)
    return DoctorReport(**defaults)  # type: ignore[arg-type]


def test_a_healthy_report_says_so() -> None:
    output = _paint(render_doctor, _report(), NOW)

    assert "Healthy" in output


def test_integrity_problems_are_named() -> None:
    finding = IntegrityFinding(check="orphan rows", count=3, detail="3 rows without a parent")

    output = _paint(render_doctor, _report(integrity=(finding,)), NOW)

    assert "orphan rows" in output


def test_a_never_synced_database_explains_itself() -> None:
    output = _paint(render_doctor, _report(never_synced=True, last_sync_at=None), NOW)

    assert "sync" in output


def test_a_blocked_bucket_is_reported_with_its_reason() -> None:
    blocked = RateState(
        bucket="boards.greenhouse.io",
        consecutive_failures=5,
        reason="429 for every request",
        updated_at=NOW,
        blocked_until=NOW + timedelta(hours=1),
    )

    output = _paint(render_doctor, _report(blocks=(blocked,)), NOW)

    assert "boards.greenhouse.io" in output


def test_failing_boards_are_listed_with_a_next_step() -> None:
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, "404"),)),))

    output = _paint(render_doctor, report, NOW)

    assert "Acme" in output


def test_a_volume_alert_names_the_source() -> None:
    report = _report(sources=(_source(volume=_volume(VolumeVerdict.COLLAPSED)),))

    output = _paint(render_doctor, report, NOW)

    assert "greenhouse" in output


def test_rows_due_for_recheck_are_listed() -> None:
    output = _paint(render_doctor, _report(due_for_recheck=("Acme: stale note",)), NOW)

    assert "Acme" in output


def test_workday_crawl_progress_shows_the_cursor() -> None:
    crawl = WorkdayCrawl(
        board="workday:acme-careers",
        next_offset=120,
        total=800,
        facet_parameter="",
        facet_ids=(),
    )

    output = _paint(render_workday_crawl_progress, (crawl,))

    assert "120" in output


def test_workday_crawl_progress_handles_nothing_in_flight() -> None:
    assert _paint(render_workday_crawl_progress, ()) != ""


def test_board_health_lists_unhealthy_boards_only() -> None:
    sources = (_source(boards=(_board(VisitState.FAILING, "boom"), _board(VisitState.HEALTHY))),)

    output = _paint(render_board_health, sources, NOW)

    assert "Acme" in output


def test_rate_state_with_nothing_recorded_says_so() -> None:
    output = _paint(render_rate_state, (), NOW)

    assert "No rate state" in output


def test_a_tightened_bucket_is_shown() -> None:
    state = RateState(
        bucket="api.lever.co",
        consecutive_failures=2,
        reason="slow responses",
        updated_at=NOW,
        min_interval_override=2.5,
    )

    output = _paint(render_rate_state, (state,), NOW)

    assert "api.lever.co" in output


def test_repairs_are_reported_when_any_ran() -> None:
    repair = IntegrityRepair(check="orphans", repaired=2, detail="relinked")

    output = _paint(render_repairs, (repair,))

    assert "orphans" in output


def test_no_repairs_prints_nothing() -> None:
    assert _paint(render_repairs, ()) == ""


def _stats(**overrides: object) -> StatsReport:
    defaults: dict[str, object] = {
        "runs": (),
        "composition": {"source": {"greenhouse": 10, "lever": 5}},
        "total_jobs": 15,
        "duplicates": 2,
        "quarantined": {"not-an-internship": 40},
        "tombstones": 0,
        "cached_urls": 7,
        "schema_version": 1,
    }
    defaults.update(overrides)
    return StatsReport(**defaults)  # type: ignore[arg-type]


def test_stats_reports_the_totals() -> None:
    output = _paint(render_stats, _stats(), NOW)

    assert "15" in output


def test_stats_breakdown_is_capped_and_says_so() -> None:
    wide = {"source": {f"src{index}": index for index in range(30)}}

    output = _paint(render_stats, _stats(composition=wide), NOW, limit=5)

    assert "--all" in output


def test_stats_without_a_limit_lists_every_row() -> None:
    wide = {"source": {f"src{index}": index for index in range(30)}}

    output = _paint(render_stats, _stats(composition=wide), NOW, limit=None)

    assert "src29" in output


@pytest.mark.parametrize("width", [50, 80, 120, 200])
def test_doctor_never_overflows_the_terminal(width: int) -> None:
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, "e" * 200),)),))

    output = _paint(render_doctor, report, NOW, width=width)

    assert max(len(line.rstrip()) for line in output.split("\n")) <= width


def test_verbose_keeps_the_whole_board_error() -> None:
    error = "a very long failure message that would normally be shortened for the table " * 3
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, error),)),))

    brief = _paint(render_doctor, report, NOW, width=200)
    full = _paint(render_doctor, report, NOW, width=200, verbose=True)

    assert len(full) > len(brief)


def _probe(**overrides: object) -> "BoardProbe":
    from stage.services.canary import BoardProbe

    defaults: dict[str, object] = {
        "source": "greenhouse",
        "company": "Acme",
        "fetched": 12,
        "error": "",
        "degraded": "",
        "unchanged": False,
        "unreachable": False,
    }
    defaults.update(overrides)
    return BoardProbe(**defaults)  # type: ignore[arg-type]


def test_a_passing_canary_reports_success() -> None:
    from stage.services.canary import CanaryReport

    output = _paint(render_canary, CanaryReport(probes=(_probe(),), skipped_platforms=()))

    assert "greenhouse" in output


def test_a_canary_failure_names_the_board() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(
        probes=(_probe(fetched=0, error="selector matched no rows"),),
        skipped_platforms=(),
    )

    output = _paint(render_canary, report)

    assert "Acme" in output


def test_an_unreachable_board_is_blamed_on_the_publisher() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(
        probes=(_probe(fetched=0, error="timeout", unreachable=True),),
        skipped_platforms=(),
    )

    output = _paint(render_canary, report)

    assert "their server" in output


def test_skipped_platforms_are_listed() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(probes=(_probe(),), skipped_platforms=("workday",))

    output = _paint(render_canary, report)

    assert "workday" in output


def test_a_canary_note_is_kept_whole_when_verbose() -> None:
    from stage.services.canary import CanaryReport

    note = "a long degradation note that the table would otherwise shorten " * 3
    report = CanaryReport(probes=(_probe(degraded=note),), skipped_platforms=())

    brief = _paint(render_canary, report, width=200)
    full = _paint(render_canary, report, width=200, verbose=True)

    assert len(full) > len(brief)
    assert note.strip() not in " ".join(brief.split())
    assert note.strip() in " ".join(full.split())


async def test_sync_lines_carry_a_running_count() -> None:
    import io
    from collections.abc import AsyncIterator
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render.progress import render_sync
    from stage.domain import (
        CompanyFinished,
        SourceStarted,
        SyncFinished,
        SyncOutcome,
        SyncStarted,
    )

    now = datetime.now(UTC)

    async def events() -> "AsyncIterator[SyncEvent]":
        yield SyncStarted(sources=("greenhouse", "simplify"), companies=3, started_at=now)
        yield SourceStarted(source="greenhouse", companies=3)
        yield SourceStarted(source="simplify", companies=0, feed=True)
        for source, name in (
            ("greenhouse", "Acme"),
            ("greenhouse", "Globex"),
            ("simplify", "SimplifyJobs"),
            ("greenhouse", "Initech"),
        ):
            yield CompanyFinished(
                source=source, company=name, fetched=1, elapsed_ms=10.0, degraded=""
            )
        yield SyncFinished(
            outcome=SyncOutcome.SUCCESS,
            dry_run=False,
            added=3,
            updated=0,
            closed=0,
            failed_sources=(),
            elapsed_ms=30.0,
        )

    buffer = io.StringIO()
    await render_sync(Console(file=buffer, width=200, emoji=False), events())
    rendered = buffer.getvalue()

    assert "1/4" in rendered
    assert "4/4" in rendered and "5/4" not in rendered


def test_the_banner_follows_the_terminal_foreground() -> None:
    import io
    import re

    from rich.console import Console

    from stage.cli.render import splash

    buffer = io.StringIO()
    console = Console(
        file=buffer, force_terminal=True, color_system="truecolor", width=80, emoji=False
    )
    splash(console)
    painted = buffer.getvalue()

    assert not re.search(r"38;2;\d+;\d+;\d+", painted)
    assert "\x1b[1;39m" in painted


def test_the_banner_accent_is_the_terminal_default() -> None:
    from stage.banner import ACCENT

    assert ACCENT == "default"


def test_a_feed_that_declares_itself_empty_reads_as_none_listed() -> None:
    from stage.services.canary import CanaryReport

    report = CanaryReport(probes=(_probe(fetched=0, declared_empty=True),), skipped_platforms=())

    output = _paint(render_canary, report)

    assert "none listed" in output
    assert "no postings" not in output


def test_the_empty_state_offers_a_wider_window_only_below_the_default() -> None:
    from stage.cli.render.postings import _empty_state

    now = datetime(2026, 10, 20, tzinfo=UTC)
    assert "--last" in _empty_state(3, now, now)
    assert "--last" not in _empty_state(14, now, now)
    assert "--last" not in _empty_state(None, now, now)


def test_one_integrity_finding_reads_as_a_count_not_a_plural() -> None:
    finding = IntegrityFinding(
        check="open postings with no first_seen", count=1, detail="first_seen is the sort key"
    )

    output = _paint(render_doctor, _report(integrity=(finding,)), NOW)

    assert "open postings with no first_seen: 1. first_seen is the sort key" in output
    assert "1 open postings" not in output


def test_stats_keeps_every_number_on_one_line_at_40_columns() -> None:
    from stage.domain import SourceRunStats, SyncOutcome, SyncRun

    run = SyncRun(
        started_at=NOW - timedelta(minutes=16),
        finished_at=NOW - timedelta(hours=5),
        outcome=SyncOutcome.PARTIAL,
        sources=(SourceRunStats("greenhouse", added=3368, closed=48, quarantined=51837),),
    )
    composition = {"source": {"vanshb03": 941_000, "simplify": 714}}

    output = _paint(render_stats, _stats(runs=(run,), composition=composition), NOW, width=40)
    rows = output.splitlines()

    assert max(len(row) for row in rows) <= 40
    assert any("3,368" in row and "48" in row for row in rows)
    assert any("vanshb03" in row and "941,000" in row and "%" in row for row in rows)


def test_coverage_counts_carry_thousands_separators() -> None:
    from stage.cli.render.health import render_coverage
    from stage.domain import CoverageState
    from stage.domain.coverage import CoverageRow
    from stage.services.coverage import CoverageReport

    rows = tuple(
        CoverageRow(
            f"Co {index}", "greenhouse", f"greenhouse:co{index}", CoverageState.EMPTY, 0, NOW, 0, ""
        )
        for index in range(1197)
    )
    report = CoverageReport(rows, (), (), (), enabled=1197, disabled=0, stale_after_days=7)

    output = _paint(render_coverage, report, NOW)

    assert "1,197 empty" in output and "(1,197)" in output and "1,167 more" in output


def test_the_list_footer_only_offers_rows_that_exist() -> None:
    from stage.cli.render.postings import render_jobs
    from stage.domain import Job, LocationBucket

    jobs = [
        Job(
            id=f"greenhouse:acme:{index}",
            source="greenhouse",
            company="Acme",
            title_raw="Intern",
            title_normalized="intern",
            apply_url_raw="https://example.com",
            description="",
            first_seen=NOW,
            last_seen=NOW,
            location_raw="Montreal",
            location=LocationBucket.MONTREAL,
        )
        for index in range(3)
    ]

    output = _paint(render_jobs, jobs, total_matching=3, window_days=14, last_sync_at=NOW, now=NOW)

    assert "stage open 1-3 acts" in output and "1-5" not in output


def test_a_long_query_that_matches_nothing_is_shown_short() -> None:
    from stage.cli.render.postings import render_search
    from stage.services.query import JobListing

    query = " ".join(["intern"] * 20_000)
    listing = JobListing(
        jobs=(),
        total_matching=0,
        window_days=None,
        last_sync_at=NOW,
        query=query,
        terms=tuple(query.split()),
    )

    output = _paint(render_search, listing)

    assert 'No posting matches "intern intern' in output and len(output) < 400


STORED_ERRORS = (
    "PayloadValidationError: custom_json/croesus: selector matched no rows; captured at /x.json",
    "BreakerOpenError: workday circuit breaker is open after 6 consecutive failures",
    "BucketBlockedError: api.example asked to be left alone; nothing further is sent this run",
)


@pytest.mark.parametrize("error", STORED_ERRORS)
def test_a_stored_board_error_reads_as_a_plain_sentence(error: str) -> None:
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING, error),)),))

    for verbose in (False, True):
        board = _paint(render_doctor, report, NOW, width=200, verbose=verbose)
        line = next(line for line in board.splitlines() if line.startswith("  Acme"))
        table = _paint(render_board_health, (report.sources[0],), NOW, width=200, verbose=verbose)
        assert "Error" not in line + table and ";" not in line + table
        assert line.startswith("  Acme (greenhouse) failed 4 fetches in a row: ")


def test_a_board_without_an_error_says_so_in_words() -> None:
    report = _report(sources=(_source(boards=(_board(VisitState.FAILING),)),))

    assert "failed 4 fetches in a row, with no error recorded" in _paint(render_doctor, report, NOW)


@pytest.mark.parametrize(
    "error",
    [STORED_ERRORS[1], "workday circuit breaker is open after 6 consecutive failures"],
)
def test_boards_the_breaker_skipped_are_told_apart_with_or_without_a_class_name(
    error: str,
) -> None:
    boards = (_board(VisitState.FAILING, error), _board(VisitState.FAILING, "404"))

    said = " ".join(_paint(render_doctor, _report(sources=(_source(boards=boards),)), NOW).split())

    assert "1 of these were skipped by Stage's circuit breaker" in said


def test_a_rate_state_reason_drops_class_names_and_semicolons() -> None:
    state = RateState(
        bucket="zshah101.github.io",
        consecutive_failures=2,
        reason="ConnectTimeout: CancelledError: Cancelled via cancel scope; reason: deadline",
        updated_at=NOW,
    )

    said = _paint(render_rate_state, (state,), NOW, width=200, verbose=True)

    assert "the connection timed out: the request was cancelled" in said
    assert "Error" not in said and ";" not in said


def _cells(count: int) -> list[list[Any]]:
    from stage.cli.render import Cell

    titles = ("Intern, Ottawa", "Stagiaire en génie logiciel", "東京 インターン", "a" * 90, "")
    return [
        [
            Cell(str(index), "dim"),
            Cell(titles[index % len(titles)], "bold green", f"https://example.test/{index}"),
            Cell("Montréal" if index % 2 else "-", "yellow"),
        ]
        for index in range(count)
    ]


@pytest.mark.parametrize("width", [20, 40, 80, 120])
@pytest.mark.parametrize("styled", [False, True])
def test_the_fast_grid_prints_what_a_rich_table_prints(width: int, styled: bool) -> None:
    import re

    from stage.cli.render import Column, _table, grid

    columns = [Column("#", 3, right=True, crop=False), Column("Title", 30), Column("Where", 10)]
    rows = _cells(12)

    def painted(draw: Callable[[Console], None]) -> str:
        buffer = io.StringIO()
        console = Console(file=buffer, width=width, force_terminal=styled, color_system=None)
        if styled:
            console = Console(
                file=buffer, width=width, force_terminal=True, color_system="truecolor"
            )
        draw(console)
        return re.sub(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\", "", buffer.getvalue())

    fast = painted(lambda console: grid(console, columns, rows))
    rich = painted(lambda console: console.print(_table(columns, rows)))

    assert fast == rich


def test_a_link_in_the_fast_grid_survives_only_in_a_terminal() -> None:
    from stage.cli.render import Cell, Column, grid

    rows = [[Cell("Apply here", "dim", "https://example.test/1")]]
    buffer = io.StringIO()
    grid(
        Console(file=buffer, width=40, force_terminal=True, legacy_windows=False),
        [Column("Title", 20)],
        rows,
    )
    piped = _paint(grid, [Column("Title", 20)], rows)

    assert "\x1b]8;" in buffer.getvalue() and "https://example.test/1" in buffer.getvalue()
    assert piped == "Title               \nApply here          \n"


@pytest.mark.parametrize(
    "value",
    ["plain ascii", "a" * 80, "tab\there", "ctl\x07x", "Montréal", "東京 インターン", "", " pad "],
)
@pytest.mark.parametrize("width", [0, 3, 10, 40])
def test_the_fast_cut_matches_the_shared_truncate(value: str, width: int) -> None:
    from stage.cli.render import cut
    from stage.domain.text import truncate

    assert cut(value, width) == truncate(value, width)


def test_every_rejected_posting_renders_without_a_rich_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rich import table

    from stage.cli.render.health import render_quarantine
    from stage.domain import QuarantinedJob, RejectionReason

    entries = [
        QuarantinedJob(
            id=f"greenhouse:acme:{index}",
            source="greenhouse",
            company="Acme",
            title_raw=f"Senior Engineer {index}",
            reason=RejectionReason.NOT_AN_INTERNSHIP,
            first_seen=NOW,
            last_seen=NOW,
            matched_phrase="senior",
        )
        for index in range(5_000)
    ]

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a Rich table renders every cell one by one")

    monkeypatch.setattr(table.Table, "__rich_console__", refuse)
    said = _paint(
        render_quarantine,
        entries,
        total_matching=5_000,
        reason_counts={"not-an-internship": 5_000},
        width=120,
    )

    assert len(said.splitlines()) == 5_004 and "5,000 rejected postings." in said


def test_the_upgrade_check_draws_an_ascii_spinner_and_clock_then_clears() -> None:
    import time

    from stage.cli.render.progress import waiting_on

    buffer = io.StringIO()
    console = Console(file=buffer, width=100, force_terminal=True, color_system=None)
    with waiting_on(console, "Checking"):
        time.sleep(0.3)
    drawn = buffer.getvalue()

    assert "Checking  0:00" in drawn
    assert drawn.isascii()
    assert drawn.endswith("\x1b[2K")


def test_the_upgrade_check_is_one_plain_line_off_a_terminal() -> None:
    from stage.cli.render.progress import waiting_on

    buffer = io.StringIO()
    with waiting_on(Console(file=buffer, width=100), "Checking"):
        pass

    assert buffer.getvalue() == "Checking\n"


def _rendered(*events: "SyncEvent") -> str:
    import asyncio
    from collections.abc import AsyncIterator

    from stage.cli.render.progress import render_sync

    async def stream() -> AsyncIterator["SyncEvent"]:
        for event in events:
            yield event

    buffer = io.StringIO()
    asyncio.run(render_sync(Console(file=buffer, width=200), stream()))
    return buffer.getvalue()


def test_skipped_boards_and_lists_are_named() -> None:
    from stage.domain import (
        SourceFresh,
        SourceResting,
        SourceStarted,
        SyncFinished,
        SyncOutcome,
        SyncStarted,
    )

    said = _rendered(
        SyncStarted(
            sources=("themuse", "greenhouse", "ashby"), companies=285, started_at=datetime.now(UTC)
        ),
        SourceStarted(source="themuse", companies=0, feed=True),
        SourceStarted(source="greenhouse", companies=3),
        SourceStarted(source="ashby", companies=0),
        SourceFresh(source="themuse", skipped=1, remaining=0, refresh_interval_h=4),
        SourceFresh(source="greenhouse", skipped=2, remaining=1, refresh_interval_h=5.5),
        SourceFresh(source="ashby", skipped=282, remaining=0, refresh_interval_h=5.5),
        SourceResting(source="greenhouse", skipped=1, remaining=1),
        SyncFinished(SyncOutcome.SUCCESS, 0, 0, 0, (), 1.0),
    )

    assert "themuse: feed" in said and "ashby: 0 boards" in said
    assert "fresh: 282 ashby boards read within 5.5h, 0 left" in said
    assert "fresh: themuse was read within 4h, so it waits for a later run" in said
    assert "fresh: 2 greenhouse boards read within 5.5h, 1 left" in said
    assert "resting: 1 greenhouse board backing off after repeated failures" in said


def test_a_sync_of_lists_alone_counts_no_boards() -> None:
    from stage.domain import SyncFinished, SyncOutcome, SyncStarted

    said = _rendered(
        SyncStarted(sources=("hanzili",), companies=0, started_at=datetime.now(UTC)),
        SyncFinished(SyncOutcome.SUCCESS, 0, 0, 0, (), 1.0),
    )

    assert said.startswith("Syncing hanzili\n")


def test_boards_that_refuse_this_computer_are_named_but_not_warned_about() -> None:
    refusing = _board(VisitState.REFUSING, "jobs.aon.com returned 403 (forbidden)")
    report = _report(sources=(_source(boards=(refusing,)),))

    output = _paint(render_doctor, report, NOW)

    assert "Boards that refuse this computer (1)" in output and "1 refuse here" in output
    assert "Boards needing a look" not in output
    assert report.warnings == 0


def test_the_quiet_board_advice_names_every_status_that_puts_a_board_there() -> None:
    from stage.domain.visits import gone, refused

    boards = (_board(VisitState.REFUSING, "returned 401"), _board(VisitState.GONE, "returned 410"))
    output = " ".join(
        _paint(render_doctor, _report(sources=(_source(boards=boards),)), NOW).split()
    )

    assert "They answer 401, 403 or 429" in output and "They answered 404 or 410" in output
    assert all(refused(f"x returned {code}") for code in (401, 403, 429))
    assert all(gone(f"x returned {code}") for code in (404, 410))


def test_boards_that_no_longer_exist_are_named_but_not_warned_about() -> None:
    gone = _board(VisitState.GONE, "boards-api.greenhouse.io returned 404 (not found)")
    report = _report(sources=(_source(boards=(gone,)),))

    output = _paint(render_doctor, report, NOW)

    assert "Boards that no longer exist (1)" in output and "1 gone" in output
    assert "Boards needing a look" not in output
    assert report.warnings == 0


def test_a_block_on_a_host_that_refuses_this_computer_is_not_a_warning() -> None:
    refusing = _board(VisitState.REFUSING, "careers.amd.com returned 403 (forbidden)")
    resting = RateState(
        bucket="careers.amd.com",
        consecutive_failures=3,
        reason="careers.amd.com returned 403 (forbidden)",
        updated_at=NOW,
        blocked_until=NOW + timedelta(hours=20),
    )
    other = RateState(
        bucket="jobs.scotiabank.com",
        consecutive_failures=3,
        reason="ReadError: connection reset by peer",
        updated_at=NOW,
        blocked_until=NOW + timedelta(minutes=4),
    )
    report = _report(sources=(_source(boards=(refusing,)),), blocks=(resting, other))

    output = _paint(render_doctor, report, NOW)

    assert report.resting_blocks == (resting,) and report.warning_blocks == (other,)
    assert report.warnings == 1
    assert "Resting after a refusal: careers.amd.com." in output
    blocked = output.split("Blocked buckets", 1)[1].split("Boards that refuse", 1)[0]
    named = {word.strip(",.:") for word in blocked.split()}
    assert {"jobs.scotiabank.com"} <= named and not {"careers.amd.com"} & named


def test_a_board_gone_again_is_a_quiet_row_not_a_failure() -> None:
    from stage.domain import CompanyFailed, SyncFinished, SyncOutcome, SyncStarted

    error = "boards-api.greenhouse.io returned 404 (not found)"
    said = _rendered(
        SyncStarted(sources=("greenhouse",), companies=1, started_at=datetime.now(UTC)),
        CompanyFailed("greenhouse", "Pretium", error, 1.0, gone=True),
        SyncFinished(SyncOutcome.SUCCESS, 0, 0, 0, (), 1.0),
    )

    assert "gone" in said and "Failed sources" not in said
    assert "1 board answered not found again" in said


def test_a_refusal_seen_before_is_a_quiet_row_not_a_failure() -> None:
    from stage.domain import CompanyFailed, SyncFinished, SyncOutcome, SyncStarted

    said = _rendered(
        SyncStarted(sources=("custom_json",), companies=1, started_at=datetime.now(UTC)),
        CompanyFailed(
            "custom_json", "Aon", "jobs.aon.com returned 403 (forbidden)", 1.0, refused=True
        ),
        SyncFinished(SyncOutcome.SUCCESS, 0, 0, 0, (), 1.0),
    )

    assert "denied" in said and "1 board refused this computer again" in said
    assert "Failed sources" not in said


@pytest.mark.parametrize(
    ("stored", "zone", "shown"),
    [
        ("2026-11-09T00:00:00+00:00", "America/Toronto", "2026-11-09"),
        ("2026-11-09T00:00:00+00:00", "Asia/Tokyo", "2026-11-09"),
        ("2026-11-09T03:30:00+00:00", "America/Toronto", "2026-11-08"),
    ],
)
def test_a_date_only_source_date_is_shown_on_its_own_day(
    monkeypatch: pytest.MonkeyPatch, stored: str, zone: str, shown: str
) -> None:
    import time

    from stage.domain.text import posted_day

    monkeypatch.setenv("TZ", zone)
    if not hasattr(time, "tzset"):
        pytest.skip("time zones are set per process only where tzset exists")
    time.tzset()
    try:
        assert posted_day(datetime.fromisoformat(stored)).isoformat() == shown
    finally:
        monkeypatch.undo()
        time.tzset()


def test_an_ordinary_failure_is_listed_under_failed_sources() -> None:
    from stage.domain import CompanyFailed, SyncFinished, SyncOutcome, SyncStarted

    said = _rendered(
        SyncStarted(sources=("greenhouse",), companies=1, started_at=datetime.now(UTC)),
        CompanyFailed("greenhouse", "Pretium", "boards-api.greenhouse.io returned 500", 1.0),
        SyncFinished(SyncOutcome.PARTIAL, 0, 0, 0, ("greenhouse",), 1.0),
    )

    assert "Failed sources" in said and "greenhouse/Pretium" in said
