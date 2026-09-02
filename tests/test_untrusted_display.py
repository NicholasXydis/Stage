from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from stage.cli.app import app
from stage.cli.render import (
    render_jobs,
    render_posting,
    render_quarantine,
)
from stage.domain import (
    Job,
    LocationBucket,
    QuarantinedJob,
    RejectionReason,
)
from stage.services.query import PostingDetail
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

HOSTILE = "[red]spoofed[/red] [link=https://evil.example]Apply[/link] [/bold] [not-a-tag]"
ANSI = "\x1b[31mred\x1b[0m"


def _console() -> Console:
    return Console(width=200, force_terminal=False, no_color=True, record=True)


def _job(identifier: str = "greenhouse:acme:1") -> Job:
    return Job(
        id=identifier,
        source="greenhouse",
        company=f"Acme {HOSTILE}",
        title_raw=f"Intern {HOSTILE}{ANSI}",
        title_normalized="intern",
        title_canonical="intern",
        apply_url_raw="https://boards.example.test/1",
        description=f"Body {HOSTILE}\nSecond line",
        first_seen=NOW,
        last_seen=NOW,
        location_raw=f"Montréal {HOSTILE}",
        location=LocationBucket.MONTREAL,
        compensation=f"$20/h {HOSTILE}",
    )


def _rendered(console: Console) -> str:
    return console.export_text()


def test_markup_in_a_title_is_shown_literally_and_never_interpreted() -> None:
    console = _console()
    render_jobs(console, [_job()], total_matching=1, window_days=None, last_sync_at=NOW, now=NOW)
    output = _rendered(console)
    assert "[red]spoofed[/red]" in output
    assert "\x1b" not in output


def test_a_malformed_closing_tag_does_not_crash_a_listing() -> None:
    console = _console()
    render_jobs(
        console,
        [_job()],
        total_matching=1,
        window_days=None,
        last_sync_at=NOW,
        now=NOW,
    )
    assert "[/bold]" in _rendered(console)


NOT_A_WEB_URL = (
    "javascript:alert(1)",
    "file:///c:/windows/system32/calc.exe",
    "http:no-host",
    "data:text/html,<script>alert(1)</script>",
    "",
)

CONTROLS_IN_URL = (
    "https://ok.example\x07evil",
    "https://ok.example\x1b]8;;https://evil.example\x1b\\",
    "https://ok.example\nhttps://evil.example",
)


def _terminal_output(url: str) -> str:
    import io

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=200, legacy_windows=False)
    job = Job(
        id="greenhouse:acme:1",
        source="greenhouse",
        company="Acme",
        title_raw="Intern",
        title_normalized="intern",
        apply_url_raw=url,
        description="",
        first_seen=NOW,
        last_seen=NOW,
    )
    render_jobs(console, [job], total_matching=1, window_days=None, last_sync_at=NOW, now=NOW)
    return buffer.getvalue()


@pytest.mark.parametrize("url", NOT_A_WEB_URL)
def test_a_url_that_is_not_a_web_address_becomes_no_hyperlink_at_all(url: str) -> None:
    assert "\x1b]8;" not in _terminal_output(url)


@pytest.mark.parametrize("url", CONTROLS_IN_URL)
def test_a_url_carrying_control_characters_is_refused_rather_than_repaired(url: str) -> None:
    from stage.domain import web_url

    assert web_url(url) is None
    assert "\x1b]8;" not in _terminal_output(url)


def test_surrounding_whitespace_is_the_only_thing_a_url_is_forgiven() -> None:
    from stage.domain import web_url

    assert web_url("  https://ok.example/apply  ") == "https://ok.example/apply"
    assert web_url("https://ok.example/a b") is None


def test_a_real_web_url_still_becomes_a_hyperlink() -> None:
    import io

    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        width=200,
        legacy_windows=False,
        color_system="truecolor",
        no_color=False,
    )
    render_jobs(
        console,
        [_job()],
        total_matching=1,
        window_days=None,
        last_sync_at=NOW,
        now=NOW,
    )
    rendered = buffer.getvalue()
    assert "\x1b]8;" in rendered
    assert "https://boards.example.test/1" in rendered


def test_a_hostile_description_renders_as_text_in_show() -> None:
    console = _console()
    render_posting(console, PostingDetail(job=_job(), duplicates=(), canonical=None), now=NOW)
    output = _rendered(console)
    assert "[link=https://evil.example]Apply[/link]" in output
    assert "$20/h [red]spoofed[/red]" in output
    assert "\x1b" not in output


def test_a_hostile_quarantine_row_renders_as_text() -> None:
    console = _console()
    render_quarantine(
        console,
        [
            QuarantinedJob(
                id="greenhouse:acme:2",
                source="greenhouse",
                company=f"Acme {HOSTILE}",
                title_raw=f"Intern {HOSTILE}",
                reason=RejectionReason.NOT_AN_INTERNSHIP,
                first_seen=NOW,
                last_seen=NOW,
                matched_phrase=HOSTILE,
            )
        ],
        total_matching=1,
        reason_counts={},
    )
    assert "[red]spoofed" in _rendered(console)


@pytest.fixture
def hostile_db(db_path: Path) -> Path:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=(_job(),))
    )
    repository.close()
    return db_path


@pytest.mark.parametrize(
    "command",
    [
        ["list", "--all"],
        ["search", "intern"],
        ["show", "greenhouse:acme:1"],
        ["quarantine"],
        ["stats"],
        ["doctor"],
        ["sources"],
    ],
)
def test_every_read_command_survives_hostile_stored_text(
    hostile_db: Path, command: list[str]
) -> None:
    result = CliRunner().invoke(app, [*command, "--db", str(hostile_db)])
    assert result.exit_code in (0, 1), result.stdout
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "\x1b[31m" not in result.stdout


def test_a_hostile_search_query_is_echoed_literally(hostile_db: Path) -> None:
    result = CliRunner().invoke(app, ["search", "[/]", "--db", str(hostile_db)])
    assert result.exit_code == 0, result.stdout
    assert "Nothing searchable in '[/]'" in result.stdout


def test_a_query_that_folds_to_a_word_still_searches(hostile_db: Path) -> None:
    result = CliRunner().invoke(app, ["search", "[/bold]", "--db", str(hostile_db)])
    assert result.exit_code == 0, result.stdout
    assert "Matched bold as prefixes" in result.stdout


def test_a_hostile_posting_id_is_echoed_literally(hostile_db: Path) -> None:
    result = CliRunner().invoke(app, ["show", "[/bold]nope", "--db", str(hostile_db)])
    assert result.exit_code == 1
    assert "[/bold]nope" in result.stdout


async def test_a_hostile_employer_name_survives_being_filtered_on(
    tmp_path: Path,
) -> None:
    import sys
    from dataclasses import replace

    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    sys.path.insert(0, "tests")
    from test_tui import _seed

    target = tmp_path / "hostile.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        screen._jobs = tuple(replace(job, company="[/bold]evil") for job in screen._jobs)
        screen._fill(screen._jobs)
        await pilot.pause()

        screen.state.choose("company", "[/bold]evil")
        screen._render_chips()
        screen._render_filters()
        await pilot.pause()

        assert screen.state.values.get("company") == "[/bold]evil"
