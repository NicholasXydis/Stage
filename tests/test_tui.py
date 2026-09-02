from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from stage.domain import Job
    from stage.tui.app import StageApp
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.sync import SyncScreen

from stage.tui.state import FilterState


def test_toggling_a_filter_twice_clears_it() -> None:
    state = FilterState()

    state.toggle("role", "swe")
    assert state.values["role"] == "swe"

    state.toggle("role", "swe")
    assert "role" not in state.values


def test_filters_convert_to_domain_filters() -> None:
    from stage.domain import LocationBucket, RoleCategory

    state = FilterState()
    state.toggle("role", "swe")
    state.toggle("location", "montreal")

    filters = state.as_filters()

    assert filters.role is RoleCategory.SWE
    assert filters.location is LocationBucket.MONTREAL


def test_an_unknown_filter_value_is_ignored_rather_than_raising() -> None:
    state = FilterState()
    state.values["role"] = "not-a-role"

    assert state.as_filters().role is None


def test_clearing_resets_query_and_filters() -> None:
    state = FilterState()
    state.query = "python"
    state.toggle("role", "swe")

    state.clear()

    assert state.query == ""
    assert not state.values


def test_stat_bars_render_within_the_requested_width() -> None:
    from collections import Counter

    from stage.tui.screens.stats import bars

    rendered = bars(Counter({"swe": 100, "data": 50}), width=10)

    assert "swe" in rendered
    assert "data" in rendered


def test_stat_bars_handle_no_data() -> None:
    from collections import Counter

    from stage.tui.screens.stats import bars

    assert "nothing recorded" in bars(Counter())


def test_the_rendered_banner_is_padded_to_a_single_width() -> None:
    from stage.tui.screens.splash import BANNER, COMPACT, _block

    for art in (BANNER, COMPACT):
        assert len({len(line) for line in _block(art).split("\n")}) == 1


@pytest.mark.parametrize("size", [(40, 20), (80, 24), (200, 60)])
async def test_the_app_starts_at_any_terminal_size(tmp_path: Path, size: tuple[int, int]) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, PostingsScreen)


async def test_an_empty_database_shows_guidance_rather_than_failing(tmp_path: Path) -> None:
    from textual.widgets import DataTable

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.screen.query_one("#results", DataTable).row_count == 0


async def test_the_results_table_takes_focus_so_shortcuts_work(tmp_path: Path) -> None:
    from textual.widgets import DataTable

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.focused, DataTable)


async def test_the_filter_panel_applies_the_row_you_pick(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        first = screen.state.values.get("role")

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert screen.state.values.get("role") != first


async def test_the_summary_describes_an_empty_database(tmp_path: Path) -> None:
    from stage.tui.app import summarize

    assert "0 postings" in await summarize(tmp_path / "nothing.db")


async def test_the_summary_survives_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    from stage.tui.app import summarize

    target = tmp_path / "broken.db"
    target.write_text("plain text", encoding="utf-8")

    assert "run stage sync" in await summarize(target)


async def test_the_stats_screen_opens_and_renders(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.stats import StatsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, StatsScreen)


async def test_the_review_screen_opens_and_renders(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.review import ReviewScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, ReviewScreen)


async def test_the_boards_screen_opens_and_renders(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, BoardsScreen)


async def test_escape_returns_to_the_postings_screen(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, PostingsScreen)


async def test_slash_moves_focus_into_the_search_box(tmp_path: Path) -> None:
    from textual.widgets import Input

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()

        assert isinstance(app.focused, Input)


async def test_escape_leaves_the_search_box(tmp_path: Path) -> None:
    from textual.widgets import DataTable

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.focused, DataTable)


async def test_the_filter_panel_toggles(tmp_path: Path) -> None:
    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#filters")
        assert panel.has_class("hidden")

        await pilot.press("f")
        await pilot.pause()

        assert not panel.has_class("hidden")


async def test_the_splash_dismisses_itself(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "1 posting")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, PostingsScreen)


async def test_exporting_with_no_results_warns_instead_of_writing(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("e")
        await pilot.pause()

        assert not list(tmp_path.glob("stage-export.*"))


def test_the_source_bar_fills_proportionally() -> None:
    from stage.tui.screens.sync import source_bar

    assert source_bar(0, 10, width=10) == "-" * 10
    assert source_bar(10, 10, width=10) == "#" * 10
    assert source_bar(5, 10, width=10).count("#") == 5


def test_the_source_bar_handles_an_unknown_total() -> None:
    from stage.tui.screens.sync import source_bar

    assert set(source_bar(3, 0, width=6)) == {"-"}


def test_the_source_bar_never_overflows_its_width() -> None:
    from stage.tui.screens.sync import source_bar

    assert len(source_bar(99, 10, width=8)) == 8


async def test_the_sync_screen_opens_idle(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert isinstance(app.screen, SyncScreen)
        assert not app.screen.running


async def test_cancelling_an_idle_sync_is_harmless(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SyncScreen(dry_run=True)
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()

        assert not screen.running


async def test_sync_events_drive_the_progress_view(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from stage.domain import CompanyFinished, SourceStarted, SyncStarted
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SyncScreen(dry_run=True)
        app.push_screen(screen)
        await pilot.pause()

        screen._absorb(
            SyncStarted(sources=("greenhouse",), companies=2, started_at=datetime.now(UTC))
        )
        screen._absorb(SourceStarted(source="greenhouse", companies=2))
        screen._absorb(
            CompanyFinished(source="greenhouse", company="Acme", fetched=3, elapsed_ms=1.0)
        )

        assert screen._sources["greenhouse"] == (1, 2)
        assert screen._done == 1
        assert screen._fetched == 3


async def test_a_failed_company_is_recorded_as_a_warning(tmp_path: Path) -> None:
    from stage.domain import CompanyFailed
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SyncScreen(dry_run=True)
        app.push_screen(screen)
        await pilot.pause()

        screen._absorb(CompanyFailed(source="lever", company="Acme", error="boom", elapsed_ms=1.0))

        assert any("Acme" in line for line in screen._warnings)


async def test_the_warning_list_stays_bounded(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import MAX_WARNINGS, SyncScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SyncScreen(dry_run=True)
        app.push_screen(screen)
        await pilot.pause()

        for index in range(MAX_WARNINGS + 25):
            screen._note(f"warning {index}")

        assert len(screen._warnings) == MAX_WARNINGS


async def test_cycling_the_export_format_wraps(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        first = screen._export_format

        for _ in range(4):
            await pilot.press("E")
            await pilot.pause()

        assert screen._export_format == first


def test_the_trend_counts_only_recent_postings() -> None:
    from datetime import UTC, datetime, timedelta

    from stage.domain import Job, JobStatus
    from stage.tui.screens.stats import trend

    now = datetime.now(UTC)

    def posting(days: int, index: int) -> Job:
        moment = now - timedelta(days=days)
        return Job(
            id=f"greenhouse:acme:{index}",
            source="greenhouse",
            company="Acme",
            title_raw="Intern",
            title_normalized="intern",
            apply_url_raw="https://example.com",
            description="",
            first_seen=moment,
            last_seen=moment,
            status=JobStatus.OPEN,
        )

    counts = trend((posting(1, 1), posting(2, 2), posting(400, 3)), days=30)

    assert sum(counts.values()) == 2


def test_the_disposition_keys_are_unique() -> None:
    from stage.tui.screens.review import DISPOSITIONS

    keys = [key for key, _, _ in DISPOSITIONS]

    assert len(keys) == len(set(keys))


def test_every_disposition_is_a_real_domain_value() -> None:
    from stage.domain import CoverageDisposition
    from stage.tui.screens.review import DISPOSITIONS

    for _, label, _ in DISPOSITIONS:
        assert CoverageDisposition(label)


async def test_classifying_without_a_selection_warns(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.review import ReviewScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = ReviewScreen()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()

        assert screen.selected_employer() is None

        warned: list[str] = []
        screen.notify = lambda message, **_: warned.append(str(message))  # type: ignore[method-assign]
        screen.action_classify("feed-only")
        await pilot.pause()

        assert warned
        assert "Highlight" in warned[0]


async def test_the_boards_screen_lists_disabled_rows_so_they_can_return(
    tmp_path: Path,
) -> None:
    from stage.companies import load_companies
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    rows = load_companies(None)
    disabled = next(company.name for company in rows if not company.enabled)
    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        for _ in range(60):
            await pilot.pause(0.1)
            if screen._companies:
                break

        assert disabled in screen._companies


async def test_the_detail_pane_expands_and_collapses(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("w")
        await pilot.pause()
        assert screen._expanded

        await pilot.press("w")
        await pilot.pause()
        assert not screen._expanded


async def test_the_about_screen_can_be_reopened(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.splash import SplashScreen

    app = StageApp(tmp_path / "empty.db", "1 posting")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("A")
        await pilot.pause()

        assert isinstance(app.screen, SplashScreen)


async def test_the_export_directory_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    monkeypatch.setenv("STAGE_EXPORT_DIR", str(tmp_path / "out"))
    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("e")
        await pilot.pause()

        assert not list(tmp_path.glob("stage-*.csv"))


def test_every_drawn_glyph_has_an_unambiguous_terminal_width() -> None:
    import unicodedata
    from collections import Counter

    from stage.tui.screens.splash import BANNER, COMPACT
    from stage.tui.screens.stats import bars
    from stage.tui.screens.sync import source_bar

    drawn = "".join(
        (
            BANNER,
            COMPACT,
            source_bar(3, 10),
            source_bar(0, 0),
            bars(Counter({"swe": 10, "data": 5})),
        )
    )
    wide = {
        char
        for char in drawn
        if ord(char) > 127 and unicodedata.east_asian_width(char) in {"A", "W", "F"}
    }

    assert not wide


def test_the_progress_bar_is_exactly_the_requested_width() -> None:
    from stage.tui.screens.sync import source_bar

    for done in (0, 1, 5, 9, 10, 99):
        assert len(source_bar(done, 10, width=12)) == 12


async def test_the_help_overlay_opens_and_closes(tmp_path: Path) -> None:
    from textual.widgets import Static

    from stage.tui.app import StageApp, summarize

    db = tmp_path / "help.db"
    app = StageApp(db, await summarize(db))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.query_one("#help", Static).has_class("visible")
        await pilot.press("question_mark")
        await pilot.pause()
        assert not app.screen.query_one("#help", Static).has_class("visible")


def test_every_shortcut_appears_in_the_help_overlay() -> None:
    from textual.binding import Binding

    from stage.tui.screens.postings import HELP_TEXT, PostingsScreen

    names = {"slash": "/", "question_mark": "?", "full_stop": "."}
    documented = HELP_TEXT.lower()
    skipped = {"?"} | {f"f{n}" for n in range(1, 10)}
    for binding in PostingsScreen.BINDINGS:
        raw = binding.key if isinstance(binding, Binding) else binding[0]
        key = names.get(raw, raw)
        if key in skipped:
            continue
        assert key.lower() in documented, key


async def test_the_table_columns_fit_the_terminal(tmp_path: Path) -> None:
    from textual.widgets import DataTable

    from stage.tui.app import StageApp, summarize

    db = tmp_path / "fit.db"
    for width in (80, 100, 140):
        app = StageApp(db, await summarize(db))
        async with app.run_test(size=(width, 24)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            table = app.screen.query_one("#results", DataTable)
            total = sum(column.width for column in table.columns.values())
            assert total <= width, width


def _hostile_job(title: str, company: str = "Acme", url: str = "https://example.com/1") -> "Job":
    from datetime import UTC, datetime

    from stage.domain import Job, JobStatus

    now = datetime.now(UTC)
    return Job(
        id="greenhouse:acme:1",
        source="greenhouse",
        company=company,
        title_raw=title,
        title_normalized=title,
        apply_url_raw=url,
        description="",
        first_seen=now,
        last_seen=now,
        location_raw="Montreal, QC",
        status=JobStatus.OPEN,
    )


def test_a_hostile_title_is_never_parsed_as_markup() -> None:
    from stage.tui.safe import cell, quoted

    rendered = cell("Intern [/bold]")

    assert str(rendered) == "Intern [/bold]"
    assert "\\[" in quoted("[link=file:///etc/passwd]Click[/link]")


async def test_a_hostile_posting_does_not_crash_the_browser(tmp_path: Path) -> None:
    from textual.widgets import DataTable

    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "hostile.db", "")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = PostingsScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._jobs = (
            _hostile_job("Intern [/bold]"),
            _hostile_job("Fine", company="[link=file:///etc/passwd]Click[/link]"),
        )
        screen._fill(screen._jobs)
        await pilot.pause()

        assert screen.query_one("#results", DataTable).row_count == 2


async def test_a_non_web_apply_url_is_never_handed_to_the_browser(tmp_path: Path) -> None:
    import webbrowser

    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    opened: list[str] = []
    app = StageApp(tmp_path / "open.db", "")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = PostingsScreen()
        app.push_screen(screen)
        await pilot.pause()

        screen._jobs = (_hostile_job("Intern", url="file:///etc/passwd"),)
        screen._fill(screen._jobs)
        await pilot.pause()

        warned: list[str] = []
        screen.notify = lambda message, **_: warned.append(str(message))  # type: ignore[method-assign]

        def _fake_open(url: str, *_: object, **__: object) -> bool:
            opened.append(url)
            return True

        original = webbrowser.open
        webbrowser.open = _fake_open
        try:
            screen.action_open()
        finally:
            webbrowser.open = original

        assert opened == []
        assert warned
        assert "http" in warned[0]


async def test_disabling_without_a_selection_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "boards.db", "")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        monkeypatch.setattr(screen, "load", lambda: None)
        app.push_screen(screen)
        await pilot.pause()

        warned: list[str] = []
        screen.notify = lambda message, **_: warned.append(str(message))  # type: ignore[method-assign]
        screen.action_disable()

        assert warned
        assert "Highlight" in warned[0]


async def test_a_registry_write_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.companies import RegistryError
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "boards.db", "")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await pilot.pause()

        def explode(_: object) -> None:
            raise RegistryError("the registry is read-only")

        monkeypatch.setattr("stage.companies.update_registry", explode)
        screen.selected_company = lambda: "Acme"  # type: ignore[method-assign]
        warned: list[str] = []
        screen.notify = lambda message, **_: warned.append(str(message))  # type: ignore[method-assign]
        screen._set_enabled(False)

        assert warned
        assert "Could not write the registry" in warned[0]
        assert "read-only" in warned[0]


async def test_a_board_already_in_that_state_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "boards.db", "")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await pilot.pause()

        calls: list[object] = []

        def unchanged(apply: object) -> tuple[tuple[object, ...], int]:
            calls.append(apply)
            return (), 0

        monkeypatch.setattr("stage.companies.update_registry", unchanged)
        screen.selected_company = lambda: "Acme"  # type: ignore[method-assign]
        warned: list[str] = []
        screen.notify = lambda message, **_: warned.append(str(message))  # type: ignore[method-assign]
        screen._set_enabled(True)

        assert warned
        assert "already enabled" in warned[0]
        assert calls


async def test_disabling_a_board_confirms_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "boards.db", "")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await pilot.pause()

        applied: list[object] = []

        def record(apply: object) -> tuple[tuple[object, ...], int]:
            applied.append(apply)
            return (), 1

        monkeypatch.setattr("stage.companies.update_registry", record)
        monkeypatch.setattr(screen, "load", lambda: None)
        screen.selected_company = lambda: "Acme"  # type: ignore[method-assign]
        told: list[str] = []
        screen.notify = lambda message, **_: told.append(str(message))  # type: ignore[method-assign]
        screen._set_enabled(False)

        assert told
        assert "Disabled Acme" in told[0]
        assert applied


def _undecorated(screen: object, name: str) -> Any:
    return getattr(type(screen), name).__wrapped__


def test_a_hostile_error_message_is_never_parsed_as_markup() -> None:
    from rich.markup import render

    from stage.tui.safe import quoted

    hostile = "registry entry 3: bad slug '[/red]' here"

    render(f"[red]{quoted(hostile)}[/red]")


def test_every_error_panel_escapes_before_it_paints() -> None:
    from rich.markup import render

    from stage.tui.safe import quoted

    hostile = "boom '[/red]' and [link=file:///etc/passwd]x[/link]"
    for template in (
        f"[red]{quoted(hostile)}[/red]",
        f"[reverse] {quoted(hostile)} [/reverse]",
    ):
        rendered = render(template)
        assert "[/red]" in str(rendered)
        assert "file:///etc/passwd" in str(rendered)


def test_no_notification_interpolates_into_markup() -> None:
    import re
    from pathlib import Path

    screens = Path(__file__).resolve().parent.parent / "src" / "stage" / "tui" / "screens"
    raw = []
    for path in screens.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "self.notify(f" in line and re.search(r"\{", line):
                raw.append(f"{path.name}: {line.strip()[:70]}")

    assert not raw, f"use told() so the message is delivered as plain text: {raw}"


def test_a_notification_is_delivered_as_plain_text() -> None:
    from stage.tui.safe import told

    seen: list[tuple[str, bool]] = []

    class _Screen:
        def notify(self, message: str, severity: str = "information", markup: bool = True) -> None:
            seen.append((message, markup))

    told(_Screen(), "Enabled [/red]Acme", "warning")

    assert seen == [("Enabled [/red]Acme", False)]


async def _review_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot_size: tuple[int, int] = (120, 40)
) -> "tuple[StageApp, ReviewScreen, tuple[int, int]]":
    from stage.tui.app import StageApp
    from stage.tui.screens.review import ReviewScreen

    app = StageApp(tmp_path / "review.db", "")
    return app, ReviewScreen(), pilot_size


async def test_recording_a_decision_writes_it_and_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, screen, size = await _review_screen(tmp_path, monkeypatch)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        monkeypatch.setattr(screen, "load", lambda: None)
        app.push_screen(screen)
        await pilot.pause()

        written: list[Any] = []

        class _Repo:
            async def record_coverage_classification(self, entry: object) -> bool:
                written.append(entry)
                return False

        monkeypatch.setattr(type(screen), "repository", property(lambda _: _Repo()))
        monkeypatch.setattr(screen, "selected_employer", lambda: "Acme")
        told: list[str] = []
        monkeypatch.setattr(screen, "notify", lambda message, **_: told.append(str(message)))

        await _undecorated(screen, "action_classify")(screen, "feed-only")

        assert written
        assert written[0].company == "Acme"
        assert written[0].disposition.value == "feed-only"
        assert written[0].note
        assert told and "Recorded Acme" in told[0]


async def test_updating_an_existing_decision_says_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, screen, size = await _review_screen(tmp_path, monkeypatch)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        monkeypatch.setattr(screen, "load", lambda: None)
        app.push_screen(screen)
        await pilot.pause()

        class _Repo:
            async def record_coverage_classification(self, entry: object) -> bool:
                return True

        monkeypatch.setattr(type(screen), "repository", property(lambda _: _Repo()))
        monkeypatch.setattr(screen, "selected_employer", lambda: "Acme")
        told: list[str] = []
        monkeypatch.setattr(screen, "notify", lambda message, **_: told.append(str(message)))

        await _undecorated(screen, "action_classify")(screen, "feed-only")

        assert told and "Updated Acme" in told[0]


async def test_clearing_a_decision_that_is_not_there_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, screen, size = await _review_screen(tmp_path, monkeypatch)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        monkeypatch.setattr(screen, "load", lambda: None)
        app.push_screen(screen)
        await pilot.pause()

        class _Repo:
            async def clear_coverage_classification(self, company: str) -> bool:
                return False

        monkeypatch.setattr(type(screen), "repository", property(lambda _: _Repo()))
        monkeypatch.setattr(screen, "selected_employer", lambda: "Acme")
        told: list[str] = []
        monkeypatch.setattr(screen, "notify", lambda message, **_: told.append(str(message)))

        await _undecorated(screen, "action_unclassify")(screen)

        assert told and "no recorded decision" in told[0]


async def test_clearing_a_decision_confirms_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, screen, size = await _review_screen(tmp_path, monkeypatch)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        monkeypatch.setattr(screen, "load", lambda: None)
        app.push_screen(screen)
        await pilot.pause()

        class _Repo:
            async def clear_coverage_classification(self, company: str) -> bool:
                return True

        monkeypatch.setattr(type(screen), "repository", property(lambda _: _Repo()))
        monkeypatch.setattr(screen, "selected_employer", lambda: "Acme")
        told: list[str] = []
        monkeypatch.setattr(screen, "notify", lambda message, **_: told.append(str(message)))

        await _undecorated(screen, "action_unclassify")(screen)

        assert told and "Cleared the decision for Acme" in told[0]


async def _sync_screen(tmp_path: Path) -> "tuple[StageApp, SyncScreen]":
    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    return StageApp(tmp_path / "sync.db", ""), SyncScreen()


async def test_cancelling_when_nothing_runs_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, screen = await _sync_screen(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()

        told: list[str] = []
        monkeypatch.setattr(screen, "notify", lambda message, **_: told.append(str(message)))
        screen.action_cancel()

        assert told and "Nothing to cancel" in told[0]


async def test_starting_twice_refuses_the_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, screen = await _sync_screen(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()

        monkeypatch.setattr(type(screen), "running", property(lambda _: True))
        told: list[str] = []
        monkeypatch.setattr(screen, "notify", lambda message, **_: told.append(str(message)))
        screen.action_start()

        assert told and "already running" in told[0]


async def test_a_broken_registry_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Static

    from stage.companies import RegistryError

    app, screen = await _sync_screen(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(screen)
        await pilot.pause()

        def explode(_: object) -> None:
            raise RegistryError("registry entry 3: bad slug '[/red]'")

        monkeypatch.setattr("stage.companies.load_companies", explode)
        await _undecorated(screen, "run_sync")(screen)
        await pilot.pause()

        chips = str(screen.query_one("#chips", Static).content)
        assert "bad slug" in chips
        assert "[/red]" in chips


async def test_a_hostile_source_name_is_never_parsed_as_markup(tmp_path: Path) -> None:
    from textual.widgets import DataTable

    from stage.tui.app import StageApp
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "sources.db", "")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = SyncScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._sources = {"[/bold]evil": (1, 2)}
        screen._paint()
        await pilot.pause()

        table = screen.query_one("#sources", DataTable)
        painted = str(next(iter(table.get_row_at(0))))

    assert painted == "[/bold]evil"


def test_the_window_cycles_through_the_offered_day_counts() -> None:
    from stage.tui.state import LAST_DAYS_CHOICES

    state = FilterState()
    seen = [state.last_days]
    for _ in LAST_DAYS_CHOICES:
        seen.append(state.cycle_last_days())

    assert set(LAST_DAYS_CHOICES).issubset(set(seen))
    assert seen[-1] == seen[0]


def test_a_zero_day_window_means_no_window() -> None:
    state = FilterState()
    state.last_days = 0

    assert state.window_days is None


def test_the_default_window_matches_the_cli() -> None:
    from stage.domain import DEFAULT_WINDOW_DAYS

    assert FilterState().window_days == DEFAULT_WINDOW_DAYS


def test_showing_fewer_stops_at_one_page() -> None:
    from stage.tui.state import PAGE_SIZE

    state = FilterState()
    state.widen()
    assert state.limit == PAGE_SIZE * 2

    assert state.narrow() is True
    assert state.limit == PAGE_SIZE

    assert state.narrow() is False
    assert state.limit == PAGE_SIZE


def test_showing_every_match_lifts_the_row_cap() -> None:
    state = FilterState()
    state.show_all = True

    assert state.as_filters().limit is None


def test_an_open_string_filter_reaches_the_domain_filters() -> None:
    state = FilterState()
    state.toggle("company", "Coveo Solutions")

    filters = state.as_filters()

    assert filters.company == "Coveo Solutions"


def test_an_unknown_open_field_is_ignored() -> None:
    state = FilterState()
    state.values["nonsense"] = "value"

    filters = state.as_filters()

    assert not hasattr(filters, "nonsense")


def test_clearing_resets_the_window_and_the_toggles() -> None:
    from stage.domain import DEFAULT_WINDOW_DAYS

    state = FilterState()
    state.only_new = True
    state.show_all = True
    state.last_days = 0
    state.widen()

    state.clear()

    assert state.only_new is False
    assert state.show_all is False
    assert state.last_days == DEFAULT_WINDOW_DAYS


def _seed(path: Path, count: int = 3) -> None:
    from datetime import UTC, datetime

    from stage.domain import DegreeRequirement, Job, Language, LocationBucket, RoleCategory
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    when = datetime.now(UTC)
    repo = SqliteRepository.connect(path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=when,
            jobs=tuple(
                Job(
                    id=f"greenhouse:acme:{index}",
                    source="greenhouse",
                    company="Acme" if index else "Beta",
                    title_raw=f"Software Engineer Intern {index}",
                    title_normalized=f"software engineer intern {index}",
                    title_canonical=f"software engineer intern {index}",
                    apply_url_raw=f"https://boards.example.test/{index}",
                    description="",
                    first_seen=when,
                    last_seen=when,
                    location_raw="Montréal, QC",
                    location=LocationBucket.MONTREAL,
                    language=Language.EN,
                    role=RoleCategory.SWE,
                    term="summer-2027" if index else "fall-2026",
                    degree_requirement=DegreeRequirement.UNKNOWN,
                )
                for index in range(count)
            ),
        )
    )
    repo.close()


async def test_marking_a_row_then_opening_uses_every_mark(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "marks.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("space")
        await pilot.pause()
        assert len(screen._marked) == 1

        assert len(screen._open_targets()) == 1


async def test_marking_twice_unmarks(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "unmark.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("space")
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()

        assert not screen._marked


async def test_opening_with_no_marks_falls_back_to_the_cursor(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "cursor.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        assert not screen._marked
        assert len(screen._open_targets()) == 1


async def test_clearing_drops_the_marks(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "clear-marks.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("space")
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()

        assert not screen._marked


async def test_only_this_employer_narrows_then_releases(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "employer.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("f")
        await pilot.pause()
        row = next(r for r in screen._rows if r[0] == "company")

        screen.state.choose(row[0], row[1])
        assert "company" in screen.state.values

        screen.state.choose(row[0], row[1])
        assert "company" not in screen.state.values


async def test_showing_more_then_fewer_returns_to_one_page(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.state import PAGE_SIZE

    target = tmp_path / "paging.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        screen.state.widen()
        assert screen.state.limit > PAGE_SIZE

        await pilot.press("M")
        await pilot.pause()
        await pilot.pause()

        assert screen.state.limit == PAGE_SIZE


async def test_show_every_match_then_fewer_restores_the_page(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.state import PAGE_SIZE

    target = tmp_path / "showall.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        screen.state.choose("show_all", "on")
        await pilot.pause()
        armed = screen.state.show_all

        await pilot.press("M")
        await pilot.pause()
        await pilot.pause()

        assert armed
        assert not screen.state.show_all
        assert screen.state.limit == PAGE_SIZE


async def test_the_window_key_changes_the_lookback(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "window.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        before = screen.state.last_days

        screen.state.choose("last_days", "30")
        await pilot.pause()

        assert screen.state.last_days != before


async def test_the_new_only_key_toggles(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "newonly.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        screen.state.choose("only_new", "on")
        armed = screen.state.only_new

        screen.state.choose("only_new", "on")

        assert armed
        assert not screen.state.only_new


async def test_export_asks_before_it_writes(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "export.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("e")
        await pilot.pause()

        assert screen._arming_export is True


async def test_escape_cancels_an_armed_export(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "cancel.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("e")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert screen._arming_export is False


async def test_a_key_changes_the_format_while_armed(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "format.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        await pilot.press("e")
        await pilot.pause()
        before = screen._export_format

        await pilot.press("g")
        await pilot.pause()

        assert screen._export_format != before
        assert screen._arming_export is True


async def test_confirming_an_export_writes_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "write.db"
    _seed(target)
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setenv("STAGE_EXPORT_DIR", str(exports))
    app = StageApp(target, "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("e")
        await pilot.pause()
        await pilot.press("e")
        for _ in range(6):
            await pilot.pause()

        assert screen._arming_export is False

    assert list(exports.glob("stage-*.csv"))


def test_the_footer_only_advertises_a_handful_of_keys() -> None:
    from textual.binding import Binding

    from stage.tui.screens.postings import PostingsScreen

    shown = [
        binding
        for binding in PostingsScreen.BINDINGS
        if isinstance(binding, Binding) and binding.show
    ]

    assert len(shown) <= 8


def test_every_binding_carries_a_description() -> None:
    from textual.binding import Binding

    from stage.tui.screens.postings import PostingsScreen

    for binding in PostingsScreen.BINDINGS:
        assert isinstance(binding, Binding)
        assert binding.description


def test_the_filter_rows_cover_every_filter_the_keys_used_to_reach() -> None:
    from stage.tui.state import filter_rows

    state = FilterState()
    names = {row[0] for row in filter_rows(state, "Acme")}

    assert {"role", "location", "language", "last_days", "only_new", "show_all"} <= names
    assert "company" in names


def test_a_filter_row_is_marked_when_it_is_the_active_one() -> None:
    from stage.tui.state import filter_rows

    state = FilterState()
    state.toggle("role", "swe")

    chosen = [row for row in filter_rows(state) if row[3]]

    assert ("role", "swe") in [(row[0], row[1]) for row in chosen]


def test_the_window_rows_mark_the_current_window() -> None:
    from stage.domain import DEFAULT_WINDOW_DAYS
    from stage.tui.state import filter_rows

    state = FilterState()

    marked = [row for row in filter_rows(state) if row[0] == "last_days" and row[3]]

    assert marked and marked[0][1] == str(DEFAULT_WINDOW_DAYS)


def test_choosing_a_window_row_sets_the_window() -> None:
    state = FilterState()

    state.choose("last_days", "30")

    assert state.last_days == 30
    assert state.window_days == 30


def test_choosing_a_toggle_row_flips_it() -> None:
    state = FilterState()

    state.choose("only_new", "on")
    assert state.only_new

    state.choose("only_new", "on")
    assert not state.only_new


def test_choosing_a_row_returns_to_the_first_page() -> None:
    from stage.tui.state import PAGE_SIZE

    state = FilterState()
    state.widen()

    state.choose("role", "swe")

    assert state.limit == PAGE_SIZE


def test_no_employer_row_appears_when_nothing_is_highlighted() -> None:
    from stage.tui.state import filter_rows

    assert all(row[0] != "company" for row in filter_rows(FilterState()))


async def test_the_filter_panel_opens_focused_and_escape_closes_it(tmp_path: Path) -> None:
    from textual.widgets import DataTable, OptionList

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "panel.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#filters", OptionList)
        assert panel.has_class("hidden")

        await pilot.press("f")
        await pilot.pause()
        assert not panel.has_class("hidden")
        opened_on = type(app.focused).__name__

        await pilot.press("escape")
        await pilot.pause()

        assert opened_on == OptionList.__name__
        assert panel.has_class("hidden")
        assert type(app.focused).__name__ == DataTable.__name__


async def test_pressing_the_filter_key_twice_closes_the_panel(tmp_path: Path) -> None:
    from textual.widgets import OptionList

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "twice.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#filters", OptionList)

        await pilot.press("f")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()

        assert panel.has_class("hidden")


async def test_the_panel_offers_the_employer_you_are_sitting_on(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "panel-employer.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("f")
        await pilot.pause()

        assert any(row[0] == "company" for row in screen._rows)


async def test_a_hostile_employer_name_cannot_break_the_filter_panel(tmp_path: Path) -> None:
    from dataclasses import replace as _replace

    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "panel-hostile.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        screen._jobs = tuple(_replace(job, company="[/bold]evil") for job in screen._jobs)
        screen._fill(screen._jobs)
        await pilot.pause()

        await pilot.press("f")
        await pilot.pause()

        assert any(row[1] == "[/bold]evil" for row in screen._rows)


def test_a_row_action_key_never_becomes_a_filing_key() -> None:
    from textual.binding import Binding

    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.screens.review import ReviewScreen

    acted_on = {
        binding.key
        for binding in PostingsScreen.BINDINGS
        if isinstance(binding, Binding) and binding.action in {"open", "mark", "expand"}
    }
    filing = {
        binding.key
        for binding in ReviewScreen.BINDINGS
        if isinstance(binding, Binding) and binding.action.startswith("classify")
    }

    assert not acted_on & filing


def test_every_screen_offers_the_same_help_key() -> None:
    from textual.binding import Binding

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.stats import StatsScreen
    from stage.tui.screens.sync import SyncScreen

    for screen in (PostingsScreen, ReviewScreen, BoardsScreen, StatsScreen, SyncScreen):
        keys = {binding.key for binding in screen.BINDINGS if isinstance(binding, Binding)}
        assert "question_mark" in keys, screen.__name__


def test_every_screen_documents_its_own_keys() -> None:
    from textual.binding import Binding

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.stats import StatsScreen
    from stage.tui.screens.sync import SyncScreen

    names = {"question_mark": "?", "escape": "escape"}
    for screen in (ReviewScreen, BoardsScreen, StatsScreen, SyncScreen):
        text = screen.HELP_TEXT.lower()
        for binding in screen.BINDINGS:
            assert isinstance(binding, Binding)
            key = names.get(binding.key, binding.key)
            if key == "?":
                continue
            assert key.lower() in text, f"{binding.key} on {screen.__name__}"


def test_no_screen_shows_more_than_a_handful_of_keys() -> None:
    from textual.binding import Binding

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.stats import StatsScreen
    from stage.tui.screens.sync import SyncScreen

    for screen in (PostingsScreen, ReviewScreen, BoardsScreen, StatsScreen, SyncScreen):
        shown = [
            binding for binding in screen.BINDINGS if isinstance(binding, Binding) and binding.show
        ]
        assert len(shown) <= 8, screen.__name__


async def test_escape_closes_the_help_overlay(tmp_path: Path) -> None:
    from textual.widgets import Static

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "help.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#help", Static)

        await pilot.press("question_mark")
        await pilot.pause()
        assert panel.has_class("visible")

        await pilot.press("escape")
        await pilot.pause()

        assert not panel.has_class("visible")


async def test_the_help_key_still_closes_the_overlay(tmp_path: Path) -> None:
    from textual.widgets import Static

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "help-toggle.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#help", Static)

        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()

        assert not panel.has_class("visible")


async def test_opening_help_closes_the_filter_panel(tmp_path: Path) -> None:
    from textual.widgets import OptionList, Static

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "exclusive.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#filters", OptionList)
        help_panel = app.screen.query_one("#help", Static)

        await pilot.press("f")
        await pilot.pause()
        assert not panel.has_class("hidden")

        await pilot.press("question_mark")
        await pilot.pause()

        assert panel.has_class("hidden")
        assert help_panel.has_class("visible")


async def test_opening_the_filter_panel_closes_help(tmp_path: Path) -> None:
    from textual.widgets import OptionList, Static

    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "exclusive2.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one("#filters", OptionList)
        help_panel = app.screen.query_one("#help", Static)

        await pilot.press("question_mark")
        await pilot.pause()

        await pilot.press("f")
        await pilot.pause()

        assert not help_panel.has_class("visible")
        assert not panel.has_class("hidden")


async def test_opening_help_cancels_an_armed_export(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    target = tmp_path / "arm-help.db"
    _seed(target)
    app = StageApp(target, "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)

        await pilot.press("e")
        await pilot.pause()
        armed = screen._arming_export

        await pilot.press("question_mark")
        await pilot.pause()

        assert armed
        assert not screen._arming_export


def test_disabling_a_board_needs_a_second_press() -> None:
    from stage.tui.screens.boards import BoardsScreen

    screen = BoardsScreen()

    assert screen._arming_disable is None


async def test_a_board_is_only_disabled_on_the_second_press(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "boards-confirm.db", "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._companies = ("Acme",)

        screen.action_disable()

        assert screen._arming_disable == "Acme"


async def test_moving_the_cursor_forgets_a_pending_disable(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    app = StageApp(tmp_path / "boards-move.db", "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._arming_disable = "Acme"

        screen.on_data_table_row_highlighted()

        assert screen._arming_disable is None


async def test_enabling_forgets_a_pending_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.boards import BoardsScreen

    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("STAGE_REGISTRY", str(registry))
    app = StageApp(tmp_path / "boards-enable.db", "")

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._arming_disable = "Acme"

        screen.action_enable()

        assert screen._arming_disable is None


def test_the_app_does_not_let_a_widget_take_over_the_screen() -> None:
    from stage.tui.app import StageApp

    assert StageApp.ALLOW_MAXIMIZE is False


def test_no_binding_fights_a_key_textual_reserves() -> None:
    from textual.binding import Binding

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.stats import StatsScreen
    from stage.tui.screens.sync import SyncScreen

    reserved = {"tab", "shift+tab", "ctrl+c", "ctrl+q", "super+c"}
    for screen in (PostingsScreen, ReviewScreen, BoardsScreen, StatsScreen, SyncScreen):
        for binding in screen.BINDINGS:
            assert isinstance(binding, Binding)
            assert binding.key not in reserved, f"{binding.key} on {screen.__name__}"


def test_the_footer_stays_out_of_the_way() -> None:
    from textual.binding import Binding

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.postings import PostingsScreen
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.stats import StatsScreen
    from stage.tui.screens.sync import SyncScreen

    for screen in (PostingsScreen, ReviewScreen, BoardsScreen, StatsScreen, SyncScreen):
        shown = [
            binding for binding in screen.BINDINGS if isinstance(binding, Binding) and binding.show
        ]
        assert len(shown) <= 6, screen.__name__
        assert any(binding.key == "question_mark" for binding in shown), screen.__name__


def test_no_filter_row_offers_a_term() -> None:
    from stage.tui.state import filter_rows

    assert all(row[0] != "term" for row in filter_rows(FilterState(), "Acme"))


def test_text_on_the_blue_backgrounds_is_pinned() -> None:
    from pathlib import Path as _Path

    theme = (_Path("src/stage/tui/theme.tcss")).read_text(encoding="utf-8")
    blocks = [block for block in theme.split("}") if "background: $blue" in block]

    assert blocks
    for block in blocks:
        if "color:" in block:
            assert "$on-blue" in block, block.strip()[:60]


def test_every_help_line_fits_the_panel() -> None:
    from rich.markup import render

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.postings import HELP_TEXT
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.stats import StatsScreen
    from stage.tui.screens.sync import SyncScreen

    texts = [HELP_TEXT] + [
        screen.HELP_TEXT for screen in (ReviewScreen, BoardsScreen, StatsScreen, SyncScreen)
    ]
    for text in texts:
        for line in text.split("\n"):
            assert len(render(line).plain) <= 42, line


def test_help_lines_share_one_shape() -> None:
    from rich.markup import render

    from stage.tui.screens.boards import BoardsScreen
    from stage.tui.screens.postings import HELP_TEXT
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.sync import SyncScreen

    texts = [HELP_TEXT] + [screen.HELP_TEXT for screen in (ReviewScreen, BoardsScreen, SyncScreen)]
    for text in texts:
        for line in text.split("\n"):
            plain = render(line).plain
            if not plain.startswith("  "):
                continue
            assert plain[2:11].rstrip() == plain[2:11].strip(), line
            assert plain[11:12] != " ", line


def test_the_theme_is_not_a_fixed_palette() -> None:
    from pathlib import Path as _Path

    theme = _Path("src/stage/tui/theme.tcss").read_text(encoding="utf-8")
    header = theme.split("Screen {")[0]

    assert "#" not in header, "the palette should follow the active theme"


def test_the_theme_choice_round_trips(tmp_path: Path) -> None:
    from stage.tui.state import load_theme, store_theme

    target = tmp_path / "tui-theme"

    assert store_theme("nord", target)
    assert load_theme(target) == "nord"


def test_a_missing_theme_file_reads_as_no_choice(tmp_path: Path) -> None:
    from stage.tui.state import load_theme

    assert load_theme(tmp_path / "absent") is None


def test_an_empty_theme_file_reads_as_no_choice(tmp_path: Path) -> None:
    from stage.tui.state import load_theme

    target = tmp_path / "tui-theme"
    target.write_text("  \n", encoding="utf-8")

    assert load_theme(target) is None


async def test_the_theme_key_moves_to_another_theme(tmp_path: Path) -> None:
    from stage.tui.app import StageApp

    app = StageApp(tmp_path / "theme.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        before = app.theme

        await pilot.press("ctrl+t")
        await pilot.pause()

        assert app.theme != before
        assert app.theme in app.available_themes


async def test_a_stored_theme_is_used_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.tui.app import StageApp

    monkeypatch.setattr("stage.tui.state.theme_path", lambda: tmp_path / "tui-theme")
    (tmp_path / "tui-theme").write_text("nord", encoding="utf-8")
    app = StageApp(tmp_path / "startup.db", "")

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        assert app.theme == "nord"


def test_text_on_a_blue_background_reads_on_every_theme() -> None:
    from textual.app import App

    def luminance(value: str) -> float:
        value = value.lstrip("#")
        channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        adjusted = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * adjusted[0] + 0.7152 * adjusted[1] + 0.0722 * adjusted[2]

    app = App[None]()
    for name in app.available_themes:
        palette = app.available_themes[name].to_color_system().generate()
        front, back = palette["text-primary"], palette["primary-muted"]
        if not (front.startswith("#") and back.startswith("#")):
            continue
        high, low = sorted((luminance(front), luminance(back)), reverse=True)
        assert (high + 0.05) / (low + 0.05) >= 4.2, name
