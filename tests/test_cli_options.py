import os
import re
from collections.abc import AsyncIterator
from json import loads
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from stage.domain import Job

from stage.cli.app import app
from stage.cli.options import run_async
from stage.domain import PurgeResult

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _seeded(
    path: Path,
    *,
    count: int,
    aged: int = 0,
    runs: int = 1,
    fresh: int = 0,
    link: str = "https://example.com/",
) -> str:
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
                        apply_url_raw=f"{link}{index}",
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
    assert "not both" in result.stderr


def test_sources_rejects_conflicting_cache_resets() -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--clear-cache", "greenhouse", "--clear-cache-all"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stderr


def test_sources_json_contains_every_report_section(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["sources", "--boards", "--json", "--db", str(empty_db)],
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
    assert "No posting is past the retention window" in result.stdout
    assert "This was a dry run, so nothing was removed." in result.stdout


def test_discover_direct_only_requires_unregistered() -> None:
    result = CliRunner().invoke(app, ["discover", "--direct-only"])

    assert result.exit_code == 2
    assert "requires --unregistered" in result.stderr


def test_discover_rejects_platform_and_exclude_together() -> None:
    result = CliRunner().invoke(
        app,
        ["discover", "--verify", "--platform", "greenhouse", "--exclude", "workable"],
    )

    assert result.exit_code == 2
    assert "not both" in result.stderr


def test_posting_ids_are_not_rewritten_as_emoji(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["show", "lever:id:5", "--db", str(empty_db)],
    )

    assert "lever:id:5" in result.stderr
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
    assert "is damaged or is not a Stage database" in messages[0]


@pytest.mark.parametrize(
    ("problem", "said", "never"),
    [
        ("database is locked", "is busy with another Stage process", "aside"),
        ("attempt to write a readonly database", "Check that the file and its folder", "aside"),
        ("unable to open database file", "Check that the file and its folder", "aside"),
        ("file is not a database", "Move it aside", "remove"),
    ],
)
def test_each_database_problem_gets_advice_that_fits_it(
    problem: str, said: str, never: str
) -> None:
    import sqlite3

    from stage.cli.options import database_problem

    where = Path("/data/stage.db")
    advice = database_problem(sqlite3.OperationalError(problem), where)

    assert said in advice and never not in advice, "a locked database must never be deleted"
    assert str(where) in advice


def test_the_cli_words_a_damaged_database_as_every_surface_does(tmp_path: Path) -> None:
    import sqlite3
    import subprocess
    import sys

    from stage.storage.problems import database_problem

    damaged = tmp_path / "elsewhere.db"
    damaged.write_bytes(b"not a database at all" * 100)
    done = subprocess.run(
        [sys.executable, "-m", "stage", "list", "--json", "--db", str(damaged)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    worded = database_problem(sqlite3.DatabaseError("file is not a database"), damaged)

    assert done.returncode == 2 and done.stdout == ""
    assert " ".join(done.stderr.split()) == " ".join(worded.split())


def test_the_tui_refuses_a_damaged_database_before_it_starts(tmp_path: Path) -> None:
    import subprocess
    import sys

    damaged = tmp_path / "stage.db"
    damaged.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    done = subprocess.run(
        [sys.executable, "-m", "stage", "tui", "--db", str(damaged)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == 2 and "is damaged" in done.stderr, done.stderr[-400:]
    assert "Traceback" not in done.stdout + done.stderr


def test_the_tui_refuses_to_start_without_a_terminal(tmp_path: Path, empty_db: Path) -> None:
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "stage", "tui", "--db", str(empty_db)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == 2 and done.stdout == ""
    assert "needs a terminal" in done.stderr and "stage list" in done.stderr


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

    from stage.cli.render.postings import render_jobs

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


def test_brackets_in_a_company_or_place_print_without_a_backslash() -> None:
    import io
    from dataclasses import replace

    from rich.console import Console

    from stage.cli.render.postings import render_jobs

    buffer = io.StringIO()
    job = replace(_sample_job(1), company="[Remote] Acme", location_raw="[HQ] Montreal")
    render_jobs(
        Console(file=buffer, width=160, emoji=False),
        [job],
        total_matching=1,
        window_days=None,
        last_sync_at=None,
    )

    printed = buffer.getvalue()
    assert "[Remote] Acme" in printed and "[HQ] Montreal" in printed
    assert "\\[" not in printed


def test_sanitize_drops_characters_that_reorder_text_on_screen() -> None:
    from stage.domain.text import sanitize

    flipped = "Intern " + chr(0x202E) + "gnp.exe" + chr(0x2066) + chr(0x200F) + chr(0x061C)

    assert sanitize(flipped) == "Intern gnp.exe"
    assert sanitize("a" + chr(0x200D) + "b") == "a" + chr(0x200D) + "b", "joiners stay"


def test_an_empty_filtered_quarantine_says_the_filter_matched_nothing() -> None:
    import io

    from rich.console import Console

    from stage.cli.render.health import render_quarantine

    buffer = io.StringIO()
    render_quarantine(Console(file=buffer, width=100), [], total_matching=0, reason_counts={"x": 3})

    assert "No rejected posting matches those filters." in buffer.getvalue()


def test_a_narrow_terminal_drops_the_optional_columns() -> None:
    import io

    from rich.console import Console

    from stage.cli.render.health import NARROW_COLUMNS
    from stage.cli.render.postings import render_jobs

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
    assert "feed-only" in result.stderr
    assert "adapter-candidate" in result.stderr


@pytest.mark.parametrize("width", [50, 60, 80, 120, 200])
def test_the_quarantine_table_never_overflows(width: int) -> None:
    import io
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render.health import render_quarantine
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
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "_STAGE_COMPLETE": "complete_zsh",
            "_TYPER_COMPLETE_ARGS": words,
        },
        check=False,
    )
    return result.stdout


@pytest.mark.skipif(os.name == "nt", reason="zsh completion")
def test_the_completion_hook_answers_with_commands() -> None:
    output = _complete("stage ")
    if not output:
        return

    assert "_arguments" in output
    for command in ("list", "search", "tui", "sync"):
        assert f'"{command}"' in output


@pytest.mark.skipif(os.name == "nt", reason="zsh completion")
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

    from stage.cli.render.health import render_coverage
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

    from stage.cli.render.health import render_rate_state
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
    assert "not both" in result.stderr


@pytest.mark.parametrize(
    ("args", "pointer"),
    [
        (["list", "--role", "swe", "--role", "data"], "Save several with stage prefs"),
        (["search", "intern", "--company", "a", "--company", "b"], "stage prefs --follow"),
    ],
)
def test_a_repeated_filter_is_refused_with_a_pointer_to_prefs(
    args: list[str], pointer: str
) -> None:
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 2
    assert pointer in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["list", "--company", ""],
        ["search", "intern", "--company", " "],
        ["export", "--format", "csv", "--company", ""],
        ["quarantine", "--company", ""],
    ],
)
def test_an_empty_filter_is_refused_rather_than_matching_nothing(
    args: list[str], tmp_path: Path
) -> None:
    result = CliRunner().invoke(app, [*args, "--db", str(tmp_path / "stage.db")])

    assert result.exit_code == 2
    assert "--company needs a value" in result.stderr


@pytest.mark.parametrize(
    ("flag", "values"),
    [
        ("--reason", ["not-an-internship", "not-a-cs-role"]),
        ("--source", ["lever", "ashby"]),
        ("--company", ["a", "b"]),
    ],
)
def test_quarantine_refuses_a_repeated_filter(flag: str, values: list[str], tmp_path: Path) -> None:
    args = [part for value in values for part in (flag, value)]
    result = CliRunner().invoke(app, ["quarantine", *args, "--db", str(tmp_path / "q.db")])

    assert result.exit_code == 2
    assert f"{flag} takes one value. Run the command once for each." in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--role", "swe", "--role", "swe"],
        ["--role", "swe", "--role", "SWE "],
        ["--location", "usa", "--location", "USA"],
        ["--company", "Google", "--company", "google"],
    ],
)
def test_the_same_filter_given_twice_is_read_once(args: list[str]) -> None:
    result = CliRunner().invoke(app, ["list", *args])

    assert result.exit_code == 0, result.stderr


def test_discover_refuses_verify_and_unregistered_together() -> None:
    result = CliRunner().invoke(app, ["discover", "--verify", "--unregistered"])

    assert result.exit_code == 2
    assert "not both" in result.stderr


def test_discover_refuses_apply_without_a_target() -> None:
    result = CliRunner().invoke(app, ["discover", "--apply"])

    assert result.exit_code == 2
    assert "Nothing to discover" in result.stderr


def test_discover_refuses_adopt_unnamed_without_direct_only() -> None:
    result = CliRunner().invoke(app, ["discover", "--unregistered", "--adopt-unnamed"])

    assert result.exit_code == 2
    assert "--adopt-unnamed" in result.stderr


def test_discover_refuses_platform_and_exclude_together() -> None:
    result = CliRunner().invoke(
        app, ["discover", "Acme", "--platform", "lever", "--exclude", "ashby"]
    )

    assert result.exit_code == 2
    assert "not both" in result.stderr


def test_discover_with_nothing_to_do_explains_itself() -> None:
    result = CliRunner().invoke(app, ["discover"])

    assert result.exit_code == 2
    assert "Nothing to discover" in result.stderr


def test_classify_refuses_clear_with_other_options() -> None:
    result = CliRunner().invoke(app, ["classify", "Acme", "--clear", "--status", "feed-only"])

    assert result.exit_code == 2
    assert "--clear" in result.stderr


def test_classify_reports_a_missing_decision(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["classify", "Nobody", "--clear", "--db", str(tmp_path / "c.db")]
    )

    assert result.exit_code == 1
    assert 'No classification is recorded for "Nobody"' in result.stderr


def test_an_unknown_role_names_the_valid_ones(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--role", "wizard", "--db", str(tmp_path / "r.db")])

    assert result.exit_code == 2
    assert "swe" in result.stderr


def test_an_unknown_export_format_names_the_valid_ones(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["export", "--format", "xlsx", "--db", str(tmp_path / "e.db")])

    assert result.exit_code == 2
    assert "csv" in result.stderr


def test_showing_a_missing_posting_suggests_where_to_look(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["show", "greenhouse:nobody:1", "--db", str(empty_db)])

    assert result.exit_code == 1
    assert "stage list" in result.stderr


def test_opening_a_missing_posting_fails_cleanly(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["open", "greenhouse:nobody:1", "--db", str(empty_db)])

    assert result.exit_code == 1


def test_list_json_is_valid_json(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--json", "--db", str(empty_db)])

    assert result.exit_code == 0
    assert isinstance(loads(result.stdout), list)


def test_search_json_is_valid_json(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["search", "python", "--json", "--db", str(empty_db)])

    assert result.exit_code == 0
    assert isinstance(loads(result.stdout), list)


def test_schedule_status_refuses_watch_with_json() -> None:
    result = CliRunner().invoke(app, ["schedule", "status", "--watch", "--json"])

    assert result.exit_code == 2
    assert "--watch" in result.stderr


def test_sync_refuses_source_and_exclude_together(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["sync", "--source", "lever", "--exclude", "ashby", "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == 2
    assert "not both" in result.stderr


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


def test_export_refuses_an_existing_file_without_force(tmp_path: Path, empty_db: Path) -> None:
    target = tmp_path / "taken.csv"
    target.write_text("already here", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["export", "--format", "csv", "--out", str(target), "--db", str(empty_db)],
    )

    assert result.exit_code == 2
    assert "--force" in result.stderr


@pytest.mark.parametrize(
    ("query", "terms", "note"),
    [
        ("c++", ("c",), 'Stage drops punctuation and symbols, so "c++" was searched as "c".'),
        ("node.js", ("node", "js"), 'so "node.js" was searched as "node js".'),
        ("日本語", (), 'Stage searches only Latin letters and digits, so nothing in "日本語"'),
        ("python", ("python",), ""),
        ("café", ("cafe",), ""),
        ("summer 2027", ("summer", "2027"), ""),
    ],
)
def test_a_query_that_loses_characters_says_how_it_was_searched(
    query: str, terms: tuple[str, ...], note: str
) -> None:
    from stage.services.query import searched_as

    said = searched_as(query, terms)
    assert note in said and bool(said) is bool(note)
    assert ";" not in said


def test_all_lifts_the_row_cap_on_list() -> None:
    result = CliRunner().invoke(app, ["list", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output
    assert "every match" in output


def test_all_lifts_the_row_cap_on_search() -> None:
    result = CliRunner().invoke(app, ["search", "--help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "--all" in output


def test_the_limit_ceiling_allows_a_full_database(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--limit", "50000", "--db", str(empty_db)])

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
    from stage.domain import MAX_OPEN_AT_ONCE

    rows = [str(index) for index in range(1, MAX_OPEN_AT_ONCE + 2)]
    result = CliRunner().invoke(app, ["open", *rows, "--db", str(tmp_path / "o.db")])
    output = ANSI_ESCAPE.sub("", result.stderr)

    assert result.exit_code == 2
    assert "--print" in output


def test_a_row_range_expands_in_place_and_leaves_ids_alone() -> None:
    from stage.cli.selection import expand

    assert expand(["1-3", "7", "workday:3m-search:r1"]) == [
        "1",
        "2",
        "3",
        "7",
        "workday:3m-search:r1",
    ]
    assert expand(["4-4"]) == ["4"]
    assert expand(["01-03", "007"]) == ["1", "2", "3", "7"]


def test_a_row_asked_for_twice_is_kept_once_in_first_order() -> None:
    from stage.cli.selection import expand

    assert expand(["3", "1-3", "01", "2-4"]) == ["3", "1", "2", "4"]


@pytest.mark.parametrize("text", ["1-", "-", "1-5-9", "1--5", "-3-"])
def test_text_shaped_like_a_range_is_refused(text: str) -> None:
    from stage.cli.selection import BadRangeError, expand

    with pytest.raises(BadRangeError, match="not a range"):
        expand([text])


def test_a_backwards_or_oversized_range_is_refused() -> None:
    from stage.cli.selection import BadRangeError, expand

    with pytest.raises(BadRangeError, match="backwards"):
        expand(["9-3"])
    with pytest.raises(BadRangeError, match="at most"):
        expand(["1-999999"])


@pytest.mark.serial
def test_a_long_argument_is_classified_in_linear_time() -> None:
    import time

    from stage.cli.selection import BadRangeError, expand

    started = time.perf_counter()
    with pytest.raises(BadRangeError):
        expand(["-" * 200_000])
    assert expand(["-" * 200_000 + "x"]) == ["-" * 200_000 + "x"]
    assert time.perf_counter() - started < 1.0


def test_a_row_number_too_long_to_convert_is_refused_not_crashed() -> None:
    from stage.cli.selection import BadRangeError, expand

    for text in ("9" * 5000, "1-" + "9" * 5000, "9999999"):
        with pytest.raises(BadRangeError, match="at most"):
            expand([text])
    assert expand(["0000001"]) == ["1"]


def test_open_counts_the_rows_a_range_expands_to(tmp_path: Path) -> None:
    from stage.domain import MAX_OPEN_AT_ONCE

    db = str(tmp_path / "o.db")
    wide = CliRunner().invoke(app, ["open", f"1-{MAX_OPEN_AT_ONCE + 1}", "--db", db])
    backwards = CliRunner().invoke(app, ["open", "9-3", "--db", db])

    assert MAX_OPEN_AT_ONCE == 50
    later = CliRunner().invoke(app, ["open", "50-100", "--db", db])
    assert "stage open 50-99" in " ".join(ANSI_ESCAPE.sub("", later.stderr).split())
    assert wide.exit_code == 2
    said = " ".join(ANSI_ESCAPE.sub("", wide.stderr).split())
    assert f"open {MAX_OPEN_AT_ONCE + 1} tabs" in said
    assert f"stage open 1-{MAX_OPEN_AT_ONCE}" in said
    assert backwards.exit_code == 2
    assert "backwards" in ANSI_ESCAPE.sub("", backwards.stdout + backwards.stderr)


def test_open_accepts_several_rows() -> None:
    import inspect

    from stage.cli.commands.postings import open_posting

    annotation = inspect.signature(open_posting).parameters["postings"].annotation

    assert "list" in str(annotation)


def test_show_hides_fields_the_posting_never_stated() -> None:
    import io

    from rich.console import Console

    from stage.cli.render.postings import render_posting
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

    from stage.cli.render.postings import render_posting
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


def test_quarantine_accepts_a_limit_beyond_the_old_ceiling(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["quarantine", "--limit", "50000", "--db", str(empty_db)])

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


def test_limit_has_no_ceiling_now_that_all_means_every_row(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["list", "--limit", "99999999", "--db", str(empty_db)])

    assert result.exit_code == 0


def test_showing_all_rows_returns_more_than_one_page(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "all.db", count=60)
    runner = CliRunner()

    paged = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--limit", "10", "--db", db]).stdout)
    every = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--all", "--db", db]).stdout)

    assert "10 postings of 60" in paged
    assert "60 postings" in every


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

    assert "4 postings" in every
    assert "6 postings" in unwindowed


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
        assert any(phrase in output for phrase in ("look like", "Did you mean", "Choose from")), (
            flag
        )


@pytest.mark.parametrize("source", ["simplify", "speedyapply", "greenhouse"])
def test_a_feed_is_a_source_like_any_board_platform(
    tmp_path: Path, source: str, empty_db: Path
) -> None:
    result = CliRunner().invoke(app, ["list", "--source", source, "--db", str(empty_db)])

    assert result.exit_code == 0, result.output


def test_clearing_the_cache_of_an_unknown_source_is_refused(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["sources", "--clear-cache", "greenhose", "--db", str(tmp_path / "s.db")]
    )
    output = " ".join(ANSI_ESCAPE.sub("", result.output).split())

    assert result.exit_code == 2 and 'Did you mean "greenhouse"?' in output


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

    assert 'Did you mean "list"?' in output


def test_the_guide_does_not_reprint_the_command_reference() -> None:
    result = CliRunner().invoke(app, ["help"])
    output = ANSI_ESCAPE.sub("", result.stdout)

    assert "Usage: " not in output
    assert "Run stage help for a guide" not in output


def test_only_new_returns_what_arrived_since_the_previous_sync(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "new.db", count=5, runs=2, fresh=2)

    output = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["list", "--new", "--db", db]).stdout)

    assert "2 postings" in output


def test_a_listing_with_no_earlier_sync_shows_everything(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "one.db", count=5, runs=1)

    output = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["list", "--new", "--db", db]).stdout)

    assert "5 postings" in output


def test_closed_postings_are_counted_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.cli import selection
    from stage.cli.commands.postings import _closed_since_you_looked

    class _Repo:
        async def closed_among(self, job_ids: tuple[str, ...]) -> int:
            return 2

    monkeypatch.setattr(selection, "read", lambda *_, **__: selection.Selection(("a", "b"), ""))
    message = run_async(_closed_since_you_looked(_Repo()))

    assert "2 postings" in message
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
    async def never(*_: object, **__: object) -> AsyncIterator[object]:
        import asyncio

        await asyncio.sleep(60)
        yield AssertionError("should have timed out")

    monkeypatch.setattr("stage.services.sync.sync", never)
    result = CliRunner().invoke(app, ["canary", "--timeout", "1", "--db", str(tmp_path / "c.db")])
    output = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())

    assert result.exit_code == 1
    assert "Gave up after 1s" in output
    assert "--timeout" in output


def test_purging_nothing_says_so(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["purge", "--db", str(tmp_path / "p.db")])
    output = ANSI_ESCAPE.sub("", result.output)

    assert result.exit_code == 0
    assert "No posting is past the retention window, and every rejected posting" in output


@pytest.mark.parametrize(
    ("result", "dry_run", "said"),
    [
        (
            PurgeResult(forgotten=131_467),
            True,
            [
                "Would forget 131,467 rejected postings that no board has listed for 30 days.",
                "[dim]This was a dry run, so nothing was removed.[/dim]",
            ],
        ),
        (
            PurgeResult(promoted=2, forgotten=5),
            False,
            [
                "Promoted 2 orphaned duplicates back into view.",
                "Forgot 5 rejected postings that no board has listed for 30 days.",
            ],
        ),
        (
            PurgeResult(purged=1, tombstoned=1),
            False,
            ["Purged 1 posting past the retention window."],
        ),
    ],
)
def test_purge_says_each_thing_it_did_once(
    result: PurgeResult, dry_run: bool, said: list[str]
) -> None:
    from stage.cli.commands.pipeline import _purge_lines

    assert _purge_lines(result, dry_run) == said


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


async def test_a_sync_lists_its_postings_newest_posted_first(db_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import Job, JobFilters, JobStatus
    from stage.storage import open_repository
    from stage.storage.repository import SourceBatch

    synced = datetime(2026, 10, 20, 12, tzinfo=UTC)

    def job(name: str, seen: datetime, posted: datetime | None) -> Job:
        return Job(
            id=f"greenhouse:acme:{name}",
            source="greenhouse",
            company="Acme",
            title_raw=f"Software Intern {name}",
            title_normalized=f"software intern {name}",
            apply_url_raw=f"https://example.com/{name}",
            description="",
            first_seen=seen,
            last_seen=seen,
            source_posted_at=posted,
            status=JobStatus.OPEN,
        )

    later = synced + timedelta(hours=6)
    jobs = (
        job("a", synced, datetime(2026, 10, 1, tzinfo=UTC)),
        job("b", synced, datetime(2026, 10, 10, tzinfo=UTC)),
        job("c", synced, None),
        job("d", later, datetime(2026, 9, 1, tzinfo=UTC)),
    )
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=later, jobs=jobs)
        )
        listed = await repository.list_jobs(JobFilters())

    assert [item.id.rsplit(":", 1)[1] for item in listed] == ["d", "b", "a", "c"]


def test_new_after_a_single_sync_says_everything_is_new(tmp_path: Path) -> None:
    once = _seeded(tmp_path / "once.db", count=3, runs=1)
    twice = _seeded(tmp_path / "twice.db", count=3, runs=2)
    runner = CliRunner()

    first = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--new", "--db", once]).stdout)
    later = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--new", "--db", twice]).stdout)
    plain = ANSI_ESCAPE.sub("", runner.invoke(app, ["list", "--db", once]).stdout)

    assert "Only one sync so far" in first
    assert "Only one sync so far" not in later
    assert "Only one sync so far" not in plain


def test_open_allows_exactly_the_cap_and_print_skips_it(tmp_path: Path, empty_db: Path) -> None:
    from stage.domain import MAX_OPEN_AT_ONCE

    db = str(empty_db)
    runner = CliRunner()
    at_cap = runner.invoke(app, ["open", f"1-{MAX_OPEN_AT_ONCE}", "--db", db])
    printed = runner.invoke(app, ["open", "1-60", "--print", "--db", db])

    for result in (at_cap, printed):
        said = " ".join(ANSI_ESCAPE.sub("", result.stdout + result.stderr).split())
        assert "at most" not in said
        assert "No listing" in said


def test_ranges_resolve_against_a_real_listing_in_order(tmp_path: Path) -> None:
    db = _seeded(tmp_path / "r.db", count=3)
    runner = CliRunner()
    runner.invoke(app, ["list", "--db", db])

    both = loads(runner.invoke(app, ["show", "1-2", "--json", "--db", db]).stdout)
    one = loads(runner.invoke(app, ["show", "2-2", "--json", "--db", db]).stdout)
    printed = runner.invoke(app, ["open", "3", "1-2", "1", "--print", "--db", db]).stdout
    past = runner.invoke(app, ["open", "1-5", "--print", "--db", db])

    assert [item["job"]["id"] for item in both] == ["greenhouse:acme:0", "greenhouse:acme:1"]
    assert one["job"]["id"] == "greenhouse:acme:1"
    assert printed.split() == [
        "https://example.com/2",
        "https://example.com/0",
        "https://example.com/1",
    ]
    assert past.exit_code == 2
    assert "out of range" in ANSI_ESCAPE.sub("", past.stdout + past.stderr)
    assert not [line for line in past.stdout.split() if line.startswith("https://")]


def test_open_launches_a_whole_page_of_a_long_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webbrowser

    opened: list[str] = []

    def record(url: str, new: int = 0, autoraise: bool = True) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    db = _seeded(tmp_path / "w.db", count=100)
    runner = CliRunner()
    runner.invoke(app, ["list", "--limit", "100", "--db", db])

    first = runner.invoke(app, ["open", "1-50", "--db", db])
    first_tabs = list(opened)
    second = runner.invoke(app, ["open", "51-100", "--db", db])
    too_many = runner.invoke(app, ["open", "50-100", "--db", db])

    assert first.exit_code == second.exit_code == 0
    assert len(first_tabs) == 50
    assert len(opened) == len(set(opened)) == 100
    assert too_many.exit_code == 2
    assert "stage open 50-99" in " ".join(ANSI_ESCAPE.sub("", too_many.stderr).split())


def test_a_long_link_stays_on_one_line_to_copy_or_pipe(tmp_path: Path) -> None:
    link = "https://example.com/" + "Software-Engineering-Intern-" * 6
    db = _seeded(tmp_path / "l.db", count=2, link=link)
    runner = CliRunner()
    runner.invoke(app, ["list", "--db", db])

    printed = runner.invoke(app, ["open", "1-2", "--print", "--db", db]).stdout
    shown = runner.invoke(app, ["show", "1", "--db", db]).stdout

    assert sorted(printed.split("\n")[:2]) == [f"{link}0", f"{link}1"]
    assert {f"{link}0", f"{link}1"} & set(ANSI_ESCAPE.sub("", shown).split("\n"))


def test_a_superscript_digit_is_a_missing_id_not_a_crash(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["show", "\u00b2", "--db", str(empty_db)])

    assert result.exit_code == 1
    assert not isinstance(result.exception, ValueError)


def _same_title_batch() -> tuple["Job", ...]:
    from datetime import UTC, datetime, timedelta

    from stage.domain import Job, JobStatus

    seen = datetime.now(UTC) - timedelta(hours=1)

    def job(name: str, posted: datetime | None) -> Job:
        return Job(
            id=f"greenhouse:acme:{name}",
            source="greenhouse",
            company="Acme",
            title_raw="Data Intern",
            title_normalized="data intern",
            apply_url_raw=f"https://example.com/{name}",
            description="",
            first_seen=seen,
            last_seen=seen,
            location_raw="Montreal, QC",
            source_posted_at=posted,
            status=JobStatus.OPEN,
        )

    return (
        job("a", datetime(2026, 10, 1, tzinfo=UTC)),
        job("b", datetime(2026, 10, 10, tzinfo=UTC)),
        job("c", None),
        job("z", datetime(2099, 1, 1, tzinfo=UTC)),
    )


async def _store(path: Path, jobs: tuple["Job", ...]) -> str:
    from datetime import UTC, datetime

    from stage.domain import SyncOutcome, SyncRun
    from stage.storage import open_repository
    from stage.storage.repository import SourceBatch

    now = datetime.now(UTC)
    async with open_repository(path) as repository:
        await repository.record_sync_run(
            SyncRun(started_at=now, finished_at=now, outcome=SyncOutcome.SUCCESS)
        )
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=now, jobs=jobs)
        )
    return str(path)


async def test_a_future_posted_date_cannot_jump_its_sync(db_path: Path) -> None:
    from stage.domain import JobFilters
    from stage.services.query import search_jobs
    from stage.storage import open_repository

    await _store(db_path, _same_title_batch())
    async with open_repository(db_path) as repository:
        listed = await repository.list_jobs(JobFilters())
        searched = await search_jobs(repository, "data intern", JobFilters(), window_days=None)

    expected = ["b", "a", "c", "z"]
    assert [job.id.rsplit(":", 1)[1] for job in listed] == expected
    assert [job.id.rsplit(":", 1)[1] for job in searched.jobs] == expected


def test_an_export_keeps_the_newest_posted_order(tmp_path: Path) -> None:
    import asyncio
    import csv

    db = asyncio.run(_store(tmp_path / "x.db", _same_title_batch()))
    out = tmp_path / "out.csv"
    result = CliRunner().invoke(app, ["export", "--format", "csv", "--out", str(out), "--db", db])

    assert result.exit_code == 0
    with out.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["id"].rsplit(":", 1)[1] for row in rows] == ["b", "a", "c", "z"]


def test_the_first_sync_note_reaches_search_but_not_json_or_an_empty_database(
    tmp_path: Path,
) -> None:
    once = _seeded(tmp_path / "once.db", count=3, runs=1)
    empty = _seeded(tmp_path / "empty.db", count=0, runs=0)
    runner = CliRunner()

    searched = runner.invoke(app, ["search", "software", "--new", "--db", once]).stdout
    as_json = runner.invoke(app, ["list", "--new", "--json", "--db", once]).stdout
    nothing = runner.invoke(app, ["list", "--new", "--db", empty]).stdout

    assert "Only one sync so far" in ANSI_ESCAPE.sub("", searched)
    assert len(loads(as_json)) == 3
    assert "Only one sync so far" not in nothing


@pytest.mark.parametrize("name", ["last-listing.json", "notify.json"])
def test_a_garbled_small_file_is_treated_as_missing(tmp_path: Path, name: str) -> None:
    from stage.cli import notify, selection

    damaged = tmp_path / name
    damaged.write_bytes(b"\xff\xfe\x00garbage\x9c")

    if name == "notify.json":
        assert notify.read(damaged) == ""
    else:
        assert selection.read(damaged) is None


@pytest.mark.parametrize("flag", ["--verify", "--unregistered"])
def test_discover_refuses_a_url_with_a_registry_wide_run(flag: str) -> None:
    result = CliRunner().invoke(app, ["discover", "--url", "https://boards.greenhouse.io/x", flag])

    assert result.exit_code == 2 and f"--url or {flag}, not both" in result.stderr


@pytest.mark.parametrize(
    "flags", [["--reset-all"], ["--clear-cache-all"], ["--clear-cache", "greenhouse"]]
)
def test_sources_json_stays_parseable_after_a_reset(
    tmp_path: Path, flags: list[str], empty_db: Path
) -> None:
    result = CliRunner().invoke(app, ["sources", *flags, "--json", "--db", str(empty_db)])

    assert result.exit_code == 0 and "sources" in loads(result.stdout)


def test_a_missing_word_list_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import typer

    from stage.cli.options import main
    from stage.lexicon import LexiconError

    def explode() -> None:
        raise LexiconError(f"lexicon not found at {tmp_path}")

    monkeypatch.setenv("STAGE_LEXICON", str(tmp_path))
    monkeypatch.setattr("stage.cli.options.app", explode)
    messages: list[str] = []
    monkeypatch.setattr(typer, "echo", lambda text, **_: messages.append(str(text)))

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 2
    assert "could not read its word lists" in messages[0] and "STAGE_LEXICON" in messages[0]


def test_classify_without_options_says_it_once(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["classify", "Tesla", "--db", str(tmp_path / "c.db")])

    assert result.exit_code == 2
    assert result.stderr.count("\n") <= 3
    assert "Pass both --status and --note, or pass --clear." in " ".join(result.stderr.split())
    assert "--status takes one of: feed-only" in " ".join(result.stderr.split())


def test_notify_refuses_a_url_and_clear_together() -> None:
    from stage.cli import notify

    notify.remember("https://discord.com/api/webhooks/1/abc")
    result = CliRunner().invoke(
        app, ["schedule", "notify", "https://discord.com/api/webhooks/2/def", "--clear"]
    )

    assert result.exit_code == 2 and "not both" in result.stderr
    assert notify.read() == "https://discord.com/api/webhooks/1/abc"


def test_export_paths_are_printed_whole(tmp_path: Path, empty_db: Path) -> None:
    deep = tmp_path / ("a-rather-long-folder-name-" * 4) / "postings.csv"
    deep.parent.mkdir()
    deep.write_text("", encoding="utf-8")
    db = str(empty_db)

    exists = CliRunner().invoke(app, ["export", "--format", "csv", "--out", str(deep), "--db", db])

    assert exists.exit_code == 2
    assert f"{deep} already exists" in exists.stderr


def test_schedule_status_prints_the_log_folder_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli import schedule

    logs = tmp_path / ("a-rather-long-folder-name-" * 4) / "logs"
    report = schedule.ScheduleStatus("macOS launchd", (), logs)
    monkeypatch.setattr(schedule, "status", lambda: report)

    result = CliRunner().invoke(app, ["schedule", "status"])

    assert f"Logs: {logs}" in result.stdout


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["list", "--role", "swee"], 'Unknown role "swee". Did you mean "swe"?'),
        (["list", "--lang", "klingon"], 'Unknown language "klingon". Choose from: en, fr'),
        (["export", "--format", "jsn"], 'Unknown format "jsn". Did you mean "json"?'),
        (["export", "--format", " "], "--format needs a value. Choose from: csv, json, md, pdf."),
        (["quarantine", "--reason", "senior"], 'Unknown reason "senior". Choose from:'),
        (["list", "--term", "fall"], 'Unknown term "fall". Terms look like summer-2027'),
        (["list", "--limit", "0"], "0 is out of range. Give a number 1 or more."),
        (["help", "lst"], 'Unknown command "lst". Did you mean "list"?'),
        (["schedule", "enable", "--action", "synk"], 'Unknown action "synk". Did you mean "sync"?'),
    ],
)
def test_a_wrong_value_is_named_with_the_closest_or_every_choice(
    args: list[str], said: str
) -> None:
    result = CliRunner().invoke(app, args)
    shown = " ".join(ANSI_ESCAPE.sub("", result.output).replace("│", " ").split())

    assert result.exit_code == 2, shown
    assert said in shown and ";" not in shown


def test_every_did_you_mean_is_built_by_the_shared_helper() -> None:
    from stage.cli.options import _unknown
    from stage.domain.text import did_you_mean

    assert _unknown("source", "lever ", ["greenhouse", "lever"]).endswith(did_you_mean("lever"))
    assert _unknown("source", "x" * 99, ["a"]) == f'Unknown source "{"x" * 57}...". Choose from: a.'


def test_a_refusal_from_a_service_reads_as_a_sentence(tmp_path: Path, empty_db: Path) -> None:
    gone = tmp_path / "gone"
    db = str(empty_db)
    export = CliRunner().invoke(
        app, ["export", "--format", "csv", "--out", str(gone / "x.csv"), "--db", db]
    )
    registry = CliRunner().invoke(app, ["coverage", "--registry", str(gone), "--db", db])

    assert export.exit_code == registry.exit_code == 2
    assert " ".join(export.stderr.split()) == (
        f"The folder {gone.resolve()} does not exist. Create it or pass another --out."
    )
    assert " ".join(registry.stderr.split()).startswith("Registry not found at ")
    assert registry.stderr.strip().endswith("STAGE_REGISTRY.")


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (
            ["prefs", "--location", "mars"],
            'Unknown location "mars". Choose from: montreal, canada, usa.',
        ),
        (["tracker", "--status", "zzz"], "Choose from: saved, applied"),
        (["track", "x:y:1", "--status", " "], "Choose a status from: saved, applied"),
        (["prefs", "--role", "x" * 99], f'Unknown role "{"x" * 57}...".'),
    ],
)
def test_an_unknown_value_reads_the_same_on_every_command(args: list[str], said: str) -> None:
    result = CliRunner().invoke(app, args)
    shown = " ".join(result.stderr.split())

    assert result.exit_code == 2
    assert said in shown and shown.endswith(".")


def test_a_sync_checks_its_source_names_before_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(_patience: float) -> bool:
        raise AssertionError("the network was checked before the flags")

    monkeypatch.setattr("stage.http.wait_until_online", never)
    result = CliRunner().invoke(
        app, ["sync", "--exclude", "grenhouse", "--db", str(tmp_path / "s")]
    )

    assert result.exit_code == 2
    assert 'Unknown source "grenhouse". Did you mean "greenhouse"?' in result.stderr


def test_ctrl_c_stops_with_a_plain_line_and_the_interrupt_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    async def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(typer.Exit) as stopped:
        run_async(interrupted(), stopped="Sync stopped.")

    assert stopped.value.exit_code == 130
    assert capsys.readouterr().err.strip() == "Sync stopped."


@pytest.mark.parametrize("command", ["purge", "rescreen"])
def test_ctrl_c_while_waiting_for_a_sync_says_it_stopped(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Callable, Iterator
    from contextlib import contextmanager

    from stage import runlock

    @contextmanager
    def interrupted(*_args: object, waiting: Callable[[str], None], **_: object) -> Iterator[None]:
        stops = [KeyboardInterrupt()]
        waiting("stage sync (pid 1)")
        if stops:
            raise stops[0]
        yield

    monkeypatch.setattr(runlock, "single_run", interrupted)
    result = CliRunner().invoke(app, [command, "--db", str(tmp_path / "stage.db")])

    assert result.exit_code == 130
    assert "Waiting for stage sync" in result.stdout and "Stopped" in result.stderr


def test_a_listing_through_the_real_entry_point_loads_no_word_lists(
    tmp_path: Path, empty_db: Path
) -> None:
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "from stage.cli.app import main\n"
        "sys.argv = ['stage', 'list', '--db', sys.argv[1]]\n"
        "try:\n"
        "    main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('LOADED:' + ','.join(m for m in ('yaml', 'stage.lexicon') if m in sys.modules), "
        "file=sys.stderr)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, str(empty_db)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert "No postings yet" in done.stdout, done.stderr
    assert done.stderr.strip().splitlines()[-1] == "LOADED:"


def test_a_missing_posting_in_open_goes_to_stderr(tmp_path: Path, empty_db: Path) -> None:
    result = CliRunner().invoke(app, ["open", "greenhouse:nope:1", "--db", str(empty_db)])

    assert result.exit_code == 1 and not result.stdout
    assert 'No posting has the id "greenhouse:nope:1".' in " ".join(result.stderr.split())


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["classify", " ", "--status", "deferred", "--note", "x"], "needs the employer's name"),
        (["classify", "Acme", "--status", "deferred", "--note", " "], "--note needs the evidence"),
    ],
)
def test_classify_refuses_a_blank_name_or_note_in_words(
    tmp_path: Path, args: list[str], said: str
) -> None:
    result = CliRunner().invoke(app, [*args, "--db", str(tmp_path / "c.db")])

    assert result.exit_code == 2 and said in result.stderr
    assert "classification" not in result.stderr


def test_a_row_past_the_end_says_which_rows_exist(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from stage.cli.selection import StaleSelectionError, remember, resolve

    listing = tmp_path / "last-listing.json"
    synced = datetime(2026, 11, 1, tzinfo=UTC)
    remember(("a", "b", "c"), synced, listing)

    with pytest.raises(StaleSelectionError, match="Use a number from 1 to 3, or run stage list"):
        resolve(9, synced, listing)


def test_rejected_postings_serialize_like_asdict() -> None:
    from dataclasses import asdict
    from datetime import UTC, datetime

    from stage.cli.serialize import quarantine_to_json
    from stage.domain import QuarantinedJob, RejectionReason
    from stage.domain.text import dump

    entry = QuarantinedJob(
        id="greenhouse:acme:1",
        source="greenhouse",
        company="Acme",
        title_raw="Senior Engineer",
        reason=RejectionReason.NOT_AN_INTERNSHIP,
        first_seen=datetime(2026, 11, 1, tzinfo=UTC),
        last_seen=datetime(2026, 11, 2, tzinfo=UTC),
    )

    assert quarantine_to_json([entry, entry]) == dump([asdict(entry), asdict(entry)])


@pytest.mark.parametrize("command", [["list"], ["search", "intern"], ["doctor"], ["quarantine"]])
def test_reading_a_database_that_does_not_exist_refuses_and_creates_nothing(
    tmp_path: Path, command: list[str]
) -> None:
    missing = tmp_path / "typo.db"

    result = CliRunner().invoke(app, [*command, "--db", str(missing)])

    assert result.exit_code == 2
    assert "There is no Stage database at" in result.stderr and "stage sync --db" in result.stderr
    assert not missing.exists()
