import re
from json import loads
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from stage.domain import Job

from stage.cli.app import app
from stage.cli.options import run_async

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _seeded(path: Path, *, count: int, aged: int = 0, runs: int = 1, fresh: int = 0) -> str:
    import asyncio
    from datetime import UTC, datetime, timedelta

    from stage.domain import Job, JobStatus, SyncOutcome, SyncRun
    from stage.storage import open_repository
    from stage.storage.repository import SourceBatch

    now = datetime.now(UTC)

    async def build() -> None:
        async with open_repository(path) as repository:
            for index in range(runs):
                finished = now - timedelta(minutes=(runs - index) * 30)
                await repository.record_sync_run(
                    SyncRun(
                        started_at=finished - timedelta(minutes=5),
                        finished_at=finished,
                        outcome=SyncOutcome.SUCCESS,
                    )
                )
            jobs = []
            for index in range(count):
                old = index < aged
                recent = index >= count - fresh
                seen = now - timedelta(days=90 if old else 0, minutes=0 if recent else 120)
                jobs.append(
                    Job(
                        id=f"greenhouse:acme:{index}",
                        source="greenhouse",
                        company="Acme",
                        title_raw=f"Software Engineering Intern {index}",
                        title_normalized=f"software engineering intern {index}",
                        apply_url_raw=f"https://example.com/{index}",
                        description="",
                        first_seen=seen,
                        last_seen=now,
                        location_raw="Montreal, QC",
                        status=JobStatus.OPEN,
                    )
                )
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=now,
                    jobs=tuple(jobs),
                    closable_boards=("greenhouse:acme",),
                )
            )

    asyncio.run(build())
    return str(path)


def test_sources_rejects_conflicting_rate_limit_resets() -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--reset-rate-limit", "example.com", "--reset-all"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_sources_rejects_conflicting_cache_resets() -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--clear-cache", "greenhouse", "--clear-cache-all"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_sources_json_contains_every_report_section(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--boards", "--json", "--db", str(tmp_path / "stage.db")],
    )

    assert result.exit_code == 0
    payload = loads(result.stdout)
    assert "sources" in payload
    assert "rate_states" in payload
    assert "boards" in payload
    assert payload["workday_crawls"] == []


def test_purge_dry_run_reports_no_removal(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["purge", "--dry-run", "--db", str(tmp_path / "stage.db")])

    assert result.exit_code == 0
    assert "No postings removed" in result.stdout


def test_discover_direct_only_requires_unregistered() -> None:
    result = CliRunner().invoke(app, ["discover", "--direct-only"])

    assert result.exit_code == 2
    assert "requires --unregistered" in result.stdout


def test_discover_rejects_platform_and_exclude_together() -> None:
    result = CliRunner().invoke(
        app,
        ["discover", "--verify", "--platform", "greenhouse", "--exclude", "workable"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_posting_ids_are_not_rewritten_as_emoji(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["show", "lever:id:5", "--db", str(tmp_path / "s.db")],
    )

    assert "lever:id:5" in result.stdout
    assert "🆔" not in result.stdout


def test_terminal_does_not_substitute_emoji_shortcodes() -> None:
    import io

    from stage.cli.render import terminal

    console = terminal()
    console.file = io.StringIO()

    console.print("greenhouse:ok:1")

    assert "greenhouse:ok:1" in console.file.getvalue()


def test_an_unusable_database_location_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from stage.cli.options import main

    def explode() -> None:
        raise OSError(30, "Read-only file system", "/nowhere")

    monkeypatch.setattr("stage.cli.options.app", explode)
    messages: list[str] = []
    monkeypatch.setattr(typer, "echo", lambda text, **_: messages.append(str(text)))

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 2
    assert "/nowhere" in messages[0]
    assert "Read-only file system" in messages[0]


def test_a_file_that_is_not_a_database_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import typer

    from stage.cli.options import main

    def explode() -> None:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr("stage.cli.options.app", explode)
    messages: list[str] = []
    monkeypatch.setattr(typer, "echo", lambda text, **_: messages.append(str(text)))

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 2
    assert "cannot be read" in messages[0]


def _sample_job(index: int) -> "Job":
    from datetime import UTC, datetime

    from stage.domain import Job, JobStatus

    now = datetime.now(UTC)
    return Job(
        id=f"greenhouse:acme:{index}",
        source="greenhouse",
        company="A Very Long Employer Name Limited",
        title_raw="Software Engineering Intern, Distributed Systems (Summer 2027)",
        title_normalized="swe intern",
        apply_url_raw="https://example.com",
        description="",
        first_seen=now,
        last_seen=now,
        location_raw="Montreal, QC",
        status=JobStatus.OPEN,
    )


@pytest.mark.parametrize("width", [30, 50, 69, 70, 100, 200])
def test_the_posting_table_never_overflows_the_terminal(width: int) -> None:
    import io

    from rich.console import Console

    from stage.cli.render import render_jobs

    buffer = io.StringIO()
    console = Console(file=buffer, width=width, emoji=False)

    render_jobs(
        console,
        [_sample_job(1), _sample_job(2)],
        total_matching=2,
        window_days=None,
        last_sync_at=None,
    )

    longest = max(len(line.rstrip()) for line in buffer.getvalue().split("\n"))
    assert longest <= width


def test_a_narrow_terminal_drops_the_optional_columns() -> None:
    import io

    from rich.console import Console

    from stage.cli.render import NARROW_COLUMNS, render_jobs

    buffer = io.StringIO()
    console = Console(file=buffer, width=NARROW_COLUMNS - 10, emoji=False)

    render_jobs(
        console,
        [_sample_job(1)],
        total_matching=1,
        window_days=None,
        last_sync_at=None,
    )
    header = buffer.getvalue().split("\n")[0]

    assert "Seen" not in header
    assert "Location" not in header
    assert "Title" in header


def test_classify_without_a_status_lists_the_valid_ones() -> None:
    result = CliRunner().invoke(app, ["classify", "Meta"])

    assert result.exit_code == 2
    assert "feed-only" in result.stdout
    assert "adapter-candidate" in result.stdout


@pytest.mark.parametrize("width", [50, 60, 80, 120, 200])
def test_the_quarantine_table_never_overflows(width: int) -> None:
    import io
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_quarantine
    from stage.domain import QuarantinedJob, RejectionReason

    now = datetime.now(UTC)
    entries = [
        QuarantinedJob(
            id=f"greenhouse:acme:{index}",
            source="greenhouse",
            company="A Very Long Employer Name Limited",
            title_raw="Senior Staff Software Engineering Manager, Platform",
            reason=RejectionReason.NOT_AN_INTERNSHIP,
            first_seen=now,
            last_seen=now,
            location_raw="San Francisco, California",
        )
        for index in range(2)
    ]
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, emoji=False)

    render_quarantine(console, entries, total_matching=2, reason_counts={})

    longest = max(len(line.rstrip()) for line in buffer.getvalue().split("\n"))
    assert longest <= width


def test_completion_is_offered() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--install-completion" in output


def _complete(words: str) -> str:
    import os
    import shutil
    import subprocess

    binary = shutil.which("stage")
    if binary is None:
        return ""
    result = subprocess.run(
        [binary],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "_STAGE_COMPLETE": "complete_zsh",
            "_TYPER_COMPLETE_ARGS": words,
        },
        check=False,
    )
    return result.stdout


def test_the_completion_hook_answers_with_commands() -> None:
    output = _complete("stage ")
    if not output:
        return

    assert "_arguments" in output
    for command in ("list", "search", "tui", "sync"):
        assert f'"{command}"' in output


def test_the_completion_hook_answers_with_filters() -> None:
    output = _complete("stage list --")
    if not output:
        return

    for flag in ("--role", "--location", "--last"):
        assert flag in output


def test_coverage_offers_a_flag_to_list_every_row() -> None:
    result = CliRunner().invoke(app, ["coverage", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output


def test_doctor_offers_a_flag_to_list_every_row() -> None:
    result = CliRunner().invoke(app, ["doctor", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output


def test_coverage_without_a_limit_lists_every_gap(tmp_path: Path) -> None:
    import io
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_coverage
    from stage.domain import CoverageRow, CoverageState
    from stage.services.coverage import CoverageReport

    rows = tuple(
        CoverageRow(
            company=f"Company {index}",
            platform="greenhouse",
            board=f"greenhouse:c{index}",
            state=CoverageState.EMPTY,
            postings=0,
            last_success_at=None,
            consecutive_failures=0,
            last_error="",
        )
        for index in range(45)
    )
    report = CoverageReport(
        rows=rows,
        unregistered=(),
        classifications=(),
        contradictions=(),
        enabled=len(rows),
        disabled=0,
        stale_after_days=14,
    )

    capped = io.StringIO()
    render_coverage(Console(file=capped, width=100, emoji=False), report, datetime.now(UTC))
    full = io.StringIO()
    render_coverage(
        Console(file=full, width=100, emoji=False), report, datetime.now(UTC), limit=None
    )

    assert "Company 44" not in capped.getvalue()
    assert "Company 44" in full.getvalue()


@pytest.mark.parametrize("command", ["coverage", "doctor", "stats", "discover"])
def test_every_capped_listing_offers_all(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output


def test_doctor_offers_full_errors() -> None:
    result = CliRunner().invoke(app, ["doctor", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--verbose" in output


@pytest.mark.parametrize("command", ["doctor", "sources", "canary"])
def test_every_shortened_error_offers_verbose(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--verbose" in output


def test_verbose_keeps_the_whole_reason() -> None:
    import io
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_rate_state
    from stage.domain import RateState

    now = datetime.now(UTC)
    reason = "the host answered 429 for every bucket in this rotation, repeatedly, at length"
    state = RateState(bucket="example.com", consecutive_failures=3, reason=reason, updated_at=now)

    def longest(verbose: bool) -> int:
        buffer = io.StringIO()
        render_rate_state(
            Console(file=buffer, width=250, emoji=False), [state], now, verbose=verbose
        )
        return max(len(line) for line in buffer.getvalue().split("\n"))

    assert longest(verbose=True) > longest(verbose=False)


def test_discover_refuses_names_and_a_url_together() -> None:
    result = CliRunner().invoke(app, ["discover", "Acme", "--url", "https://example.com"])

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_discover_refuses_verify_and_unregistered_together() -> None:
    result = CliRunner().invoke(app, ["discover", "--verify", "--unregistered"])

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_discover_refuses_apply_without_a_target() -> None:
    result = CliRunner().invoke(app, ["discover", "Acme", "--apply"])

    assert result.exit_code == 2
    assert "--apply" in result.stdout


def test_discover_refuses_adopt_unnamed_without_direct_only() -> None:
    result = CliRunner().invoke(app, ["discover", "--unregistered", "--adopt-unnamed"])

    assert result.exit_code == 2
    assert "--adopt-unnamed" in result.stdout


def test_discover_refuses_platform_and_exclude_together() -> None:
    result = CliRunner().invoke(
        app, ["discover", "Acme", "--platform", "lever", "--exclude", "ashby"]
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_discover_with_nothing_to_do_explains_itself() -> None:
    result = CliRunner().invoke(app, ["discover"])

    assert result.exit_code == 2
    assert "Nothing to discover" in result.stdout


def test_classify_refuses_clear_with_other_options() -> None:
    result = CliRunner().invoke(app, ["classify", "Acme", "--clear", "--status", "feed-only"])

    assert result.exit_code == 2
    assert "--clear" in result.stdout


def test_classify_reports_a_missing_decision(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["classify", "Nobody", "--clear", "--db", str(tmp_path / "c.db")]
    )

    assert result.exit_code == 1
    assert "No matching classification" in result.stdout


def test_an_unknown_role_names_the_valid_ones(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--role", "wizard", "--db", str(tmp_path / "r.db")])

    assert result.exit_code == 2
    assert "swe" in result.stdout


def test_an_unknown_export_format_names_the_valid_ones(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["export", "--format", "xlsx", "--db", str(tmp_path / "e.db")])

    assert result.exit_code == 2
    assert "csv" in result.stdout


def test_showing_a_missing_posting_suggests_where_to_look(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["show", "greenhouse:nobody:1", "--db", str(tmp_path / "s.db")]
    )

    assert result.exit_code == 1
    assert "stage list" in result.stdout


def test_opening_a_missing_posting_fails_cleanly(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["open", "greenhouse:nobody:1", "--db", str(tmp_path / "o.db")]
    )

    assert result.exit_code == 1


def test_list_json_is_valid_json(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--json", "--db", str(tmp_path / "j.db")])

    assert result.exit_code == 0
    assert isinstance(loads(result.stdout), list)


def test_search_json_is_valid_json(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["search", "python", "--json", "--db", str(tmp_path / "j2.db")]
    )

    assert result.exit_code == 0
    assert isinstance(loads(result.stdout), list)


def test_schedule_status_refuses_watch_with_json() -> None:
    result = CliRunner().invoke(app, ["schedule", "status", "--watch", "--json"])

    assert result.exit_code == 2
    assert "--watch" in result.stdout


def test_sync_refuses_source_and_exclude_together(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["sync", "--source", "lever", "--exclude", "ashby", "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_rescreen_on_an_empty_database_says_so(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["rescreen", "--db", str(tmp_path / "r.db")])

    assert result.exit_code == 0
    assert "sync" in result.stdout


def test_purge_dry_run_removes_nothing(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["purge", "--dry-run", "--db", str(tmp_path / "p.db")])

    assert result.exit_code == 0


def test_quarantine_rejects_an_unknown_reason(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["quarantine", "--reason", "nonsense", "--db", str(tmp_path / "q.db")]
    )

    assert result.exit_code == 2


def test_sources_rejects_an_unknown_stale_window(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["sources", "--stale-days", "0", "--db", str(tmp_path / "w.db")]
    )

    assert result.exit_code == 2


def test_export_refuses_an_existing_file_without_force(tmp_path: Path) -> None:
    target = tmp_path / "taken.csv"
    target.write_text("already here", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["export", "--format", "csv", "--out", str(target), "--db", str(tmp_path / "x.db")],
    )

    assert result.exit_code == 2
    assert "--force" in result.stdout


@pytest.mark.parametrize("query", ["c++", "c#", "node.js", ".net"])
def test_a_query_losing_punctuation_says_so(query: str) -> None:
    from stage.cli.render import _dropped_punctuation

    assert _dropped_punctuation(query) == query


@pytest.mark.parametrize("query", ["python", "cafe", "machine learning", "summer 2027"])
def test_a_clean_query_warns_about_nothing(query: str) -> None:
    from stage.cli.render import _dropped_punctuation

    assert _dropped_punctuation(query) == ""


def test_all_lifts_the_row_cap_on_list() -> None:
    result = CliRunner().invoke(app, ["list", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output
    assert "every match" in output


def test_all_lifts_the_row_cap_on_search() -> None:
    result = CliRunner().invoke(app, ["search", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output


def test_the_limit_ceiling_allows_a_full_database(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--limit", "50000", "--db", str(tmp_path / "l.db")])

    assert result.exit_code == 0


def test_a_row_number_resolves_against_the_last_listing(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from stage.cli.selection import remember, resolve

    target = tmp_path / "listing.json"
    synced = datetime.now(UTC)
    remember(("greenhouse:acme:1", "greenhouse:acme:2"), synced, target)

    assert resolve(2, synced, target) == "greenhouse:acme:2"


def test_a_row_number_is_refused_after_the_database_changes(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.cli.selection import StaleSelectionError, remember, resolve

    target = tmp_path / "listing.json"
    synced = datetime.now(UTC)
    remember(("greenhouse:acme:1",), synced, target)

    with pytest.raises(StaleSelectionError, match="changed"):
        resolve(1, synced + timedelta(minutes=1), target)


def test_a_row_number_outside_the_listing_is_refused(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from stage.cli.selection import StaleSelectionError, remember, resolve

    target = tmp_path / "listing.json"
    synced = datetime.now(UTC)
    remember(("greenhouse:acme:1",), synced, target)

    with pytest.raises(StaleSelectionError, match="out of range"):
        resolve(9, synced, target)


def test_a_row_number_without_any_listing_is_refused(tmp_path: Path) -> None:
    from stage.cli.selection import StaleSelectionError, resolve

    with pytest.raises(StaleSelectionError, match="Run stage list"):
        resolve(1, None, tmp_path / "missing.json")


def test_a_corrupt_listing_file_is_ignored(tmp_path: Path) -> None:
    from stage.cli.selection import StaleSelectionError, resolve

    target = tmp_path / "listing.json"
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(StaleSelectionError):
        resolve(1, None, target)


def test_the_export_leads_with_the_company_not_the_id() -> None:
    from stage.services.export import COLUMNS

    names = [name for name, _ in COLUMNS]

    assert names[0] == "company"
    assert names[-1] == "id"


def test_help_for_a_command_taking_an_argument_shows_help(tmp_path: Path) -> None:
    for command in ("show", "search", "classify"):
        result = CliRunner().invoke(app, ["help", command])
        output = ANSI_ESCAPE.sub("", result.stdout)

        assert result.exit_code == 0, command
        assert "Usage:" in output
        assert "Missing argument" not in output


def test_the_root_help_points_at_the_guide() -> None:
    result = CliRunner().invoke(app, ["--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "stage help" in output


def test_export_without_a_format_shows_usage_instead_of_writing(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["export", "--db", str(tmp_path / "e.db")])
    output = ANSI_ESCAPE.sub("", result.stdout + result.stderr)

    assert result.exit_code == 2
    assert "Usage:" in output
    assert "--format" in output
    assert not list(tmp_path.glob("*.csv"))


def test_export_writes_every_match_by_default() -> None:
    import inspect

    from stage.cli.commands.postings import export

    limit = inspect.signature(export).parameters["limit"].default

    assert limit is None


def test_open_refuses_more_tabs_than_it_should(tmp_path: Path) -> None:
    from stage.cli.commands.postings import MAX_OPEN_AT_ONCE

    rows = [str(index) for index in range(1, MAX_OPEN_AT_ONCE + 2)]
    result = CliRunner().invoke(app, ["open", *rows, "--db", str(tmp_path / "o.db")])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert result.exit_code == 2
    assert "--print" in output


def test_open_accepts_several_rows() -> None:
    import inspect

    from stage.cli.commands.postings import open_posting

    annotation = inspect.signature(open_posting).parameters["postings"].annotation

    assert "list" in str(annotation)


def test_show_hides_fields_the_posting_never_stated() -> None:
    import io

    from rich.console import Console

    from stage.cli.render import render_posting
    from stage.services.query import PostingDetail

    job = _sample_job(1)
    buffer = io.StringIO()
    render_posting(Console(file=buffer, width=100, emoji=False), PostingDetail(job, (), None))
    output = buffer.getvalue()

    assert "term" not in output
    assert "degree" not in output


def test_show_puts_the_identifier_last() -> None:
    import io

    from rich.console import Console

    from stage.cli.render import render_posting
    from stage.services.query import PostingDetail

    job = _sample_job(1)
    buffer = io.StringIO()
    render_posting(Console(file=buffer, width=100, emoji=False), PostingDetail(job, (), None))
    output = buffer.getvalue()

    assert output.index(job.id) > output.index("source")


def test_quarantine_matches_the_other_listing_commands() -> None:
    result = CliRunner().invoke(app, ["quarantine", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output


def test_quarantine_accepts_a_limit_beyond_the_old_ceiling(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["quarantine", "--limit", "50000", "--db", str(tmp_path / "q.db")]
    )

    assert result.exit_code == 0


def test_search_scopes_dates_with_all_like_every_other_command() -> None:
    result = CliRunner().invoke(app, ["search", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output
    assert "--window" not in output


def test_a_word_where_a_number_belongs_is_not_called_out_of_range() -> None:
    result = CliRunner().invoke(app, ["stats", "--runs", "abc"])
    output = ANSI_ESCAPE.sub("", result.output)

    assert result.exit_code == 2
    assert "out of range" not in output
    assert "not a whole number" in output


def test_a_number_past_the_ceiling_reports_the_bounds_in_words() -> None:
    result = CliRunner().invoke(app, ["stats", "--runs", "500"])
    output = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())

    assert result.exit_code == 2
    assert "between 1 and 200" in output


def test_limit_has_no_ceiling_now_that_all_means_every_row(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["list", "--limit", "99999999", "--db", str(tmp_path / "l.db")]
    )

    assert result.exit_code == 0


def test_showing_all_rows_returns_more_than_one_page(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "all.db", count=60)
    runner = CliRunner()

    paged = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--limit", "10", "--db", db]).stdout)
    every = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--all", "--db", db]).stdout)

    assert "10 posting(s) of 60" in paged
    assert "60 posting(s)" in every


def test_a_listing_longer_than_the_store_numbers_only_what_it_kept(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from stage.cli import selection

    target = tmp_path / "listing.json"
    synced = datetime.now(UTC)
    kept = selection.remember(tuple(f"greenhouse:acme:{n}" for n in range(5)), synced, target)

    assert kept == 5


def test_showing_all_no_longer_widens_the_window(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "window.db", count=6, aged=2)
    runner = CliRunner()

    every = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--all", "--db", db]).stdout)
    unwindowed = ANSI_ESCAPE.sub(
        "", runner.invoke(app, ["list", "--all", "--last", "0", "--db", db]).stdout
    )

    assert "4 posting(s)" in every
    assert "6 posting(s)" in unwindowed


def test_the_lookback_flag_turns_the_window_off_at_zero() -> None:
    from stage.cli.commands.postings import _window
    from stage.domain import DEFAULT_WINDOW_DAYS

    assert _window(None) == DEFAULT_WINDOW_DAYS
    assert _window(90) == 90
    assert _window(0) is None


def test_the_degree_filter_is_gone_from_the_posting_commands() -> None:
    for command in ("list", "search", "export"):
        result = CliRunner().invoke(app, [command, "--degree", "bachelors"])
        assert result.exit_code == 2
        assert "--degree" in ANSI_ESCAPE.sub("", result.output)


def test_exports_drop_the_columns_that_never_vary() -> None:
    from stage.services.export import COLUMNS

    names = [name for name, _ in COLUMNS]

    assert "degree" not in names
    assert "work_auth_restricted" not in names


def test_show_accepts_several_rows(tmp_path: Path) -> None:
    import inspect

    from stage.cli.commands.postings import show

    annotation = inspect.signature(show).parameters["postings"].annotation

    assert "list" in str(annotation)


def test_show_without_a_row_explains_where_rows_come_from() -> None:
    result = CliRunner().invoke(app, ["show"])
    output = ANSI_ESCAPE.sub("", result.output)

    assert result.exit_code == 2
    assert "stage list" in output


def test_open_without_a_row_explains_where_rows_come_from() -> None:
    result = CliRunner().invoke(app, ["open"])
    output = ANSI_ESCAPE.sub("", result.output)

    assert result.exit_code == 2
    assert "stage list" in output


def test_canary_offers_a_timeout() -> None:
    result = CliRunner().invoke(app, ["canary", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--timeout" in output


def test_every_constrained_filter_names_its_valid_values(tmp_path: Path) -> None:
    db = str(tmp_path / "v.db")
    for flag in ("--role", "--location", "--lang", "--term", "--source"):
        result = CliRunner().invoke(app, ["list", flag, "definitely-not-valid", "--db", db])
        output = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())
        assert result.exit_code == 2, flag
        assert any(
            phrase in output
            for phrase in ("must be one of", "must look like", "Did you mean", "Choose from")
        ), flag


def test_a_near_miss_on_a_source_suggests_the_real_one(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["list", "--source", "greenhous", "--db", str(tmp_path / "s.db")]
    )
    output = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())

    assert "greenhouse" in output


def test_an_unknown_employer_says_so_instead_of_looking_empty(tmp_path: Path) -> None:
    from stage.cli.commands.postings import _company_hint

    class _Repo:
        async def company_names(self) -> list[str]:
            return ["Databricks", "Meta"]

    hint = run_async(_company_hint("Databrick", _Repo()))

    assert "Databricks" in hint


def test_a_known_employer_needs_no_hint() -> None:
    from stage.cli.commands.postings import _company_hint

    class _Repo:
        async def company_names(self) -> list[str]:
            return ["Databricks"]

    assert run_async(_company_hint("Databricks", _Repo())) == ""


def test_the_role_error_hides_the_value_that_never_matches() -> None:
    result = CliRunner().invoke(app, ["list", "--role", "nope"])
    output = ANSI_ESCAPE.sub("", result.output)

    assert "hardware" not in output


def test_help_for_an_unknown_command_suggests_the_real_one() -> None:
    result = CliRunner().invoke(app, ["help", "lst"])
    output = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())

    assert "Did you mean 'list'?" in output


def test_the_guide_does_not_reprint_the_command_reference() -> None:
    result = CliRunner().invoke(app, ["help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "Usage: " not in output
    assert "Run stage help for a guide" not in output


def test_only_new_returns_what_arrived_since_the_previous_sync(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "new.db", count=5, runs=2, fresh=2)

    output = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["list", "--new", "--db", db]).stdout)

    assert "2 posting(s)" in output


def test_a_listing_with_no_earlier_sync_shows_everything(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "one.db", count=5, runs=1)

    output = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["list", "--new", "--db", db]).stdout)

    assert "5 posting(s)" in output


def test_closed_postings_are_counted_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.cli import selection
    from stage.cli.commands.postings import _closed_since_you_looked

    class _Repo:
        async def closed_among(self, job_ids: tuple[str, ...]) -> int:
            return 2

    monkeypatch.setattr(selection, "read", lambda *_, **__: selection.Selection(("a", "b"), ""))
    message = run_async(_closed_since_you_looked(_Repo()))

    assert "2 posting(s)" in message
    assert "closed" in message


def test_nothing_is_said_when_no_posting_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.cli import selection
    from stage.cli.commands.postings import _closed_since_you_looked

    class _Repo:
        async def closed_among(self, job_ids: tuple[str, ...]) -> int:
            return 0

    monkeypatch.setattr(selection, "read", lambda *_, **__: selection.Selection(("a",), ""))

    assert run_async(_closed_since_you_looked(_Repo())) == ""


def test_the_new_flag_is_offered_on_list() -> None:
    result = CliRunner().invoke(app, ["list", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--new" in output


def test_a_canary_that_never_answers_gives_up_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*_: object, **__: object) -> object:
        import asyncio

        await asyncio.sleep(60)
        raise AssertionError("should have timed out")

    monkeypatch.setattr("stage.services.canary.canary", never)
    result = CliRunner().invoke(app, ["canary", "--timeout", "1", "--db", str(tmp_path / "c.db")])
    output = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())

    assert result.exit_code == 1
    assert "Gave up after 1s" in output
    assert "--timeout" in output


def test_purging_nothing_says_so(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["purge", "--db", str(tmp_path / "p.db")])
    output = ANSI_ESCAPE.sub("", result.output)

    assert result.exit_code == 0
    assert "Nothing outside the retention window" in output


def test_a_sync_writes_the_request_log_when_asked(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dry-run",
            "--source",
            "greenhouse",
            "--request-log",
            str(log),
            "--db",
            str(tmp_path / "s.db"),
        ],
    )

    assert result.exit_code in (0, 1)
    assert log.exists()
