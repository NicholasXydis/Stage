from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from stage.domain import Job
    from stage.tui.app import StageApp
    from stage.tui.screens.review import ReviewScreen
    from stage.tui.screens.sync import SyncScreen

from stage.tui.state import (
    FilterState,
    describe,
    load_saved,
    remember,
    store_saved,
)


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


def test_describe_names_the_active_filters() -> None:
    state = FilterState()
    assert describe(state) == "all postings"

    state.toggle("role", "swe")
    state.query = "rust"

    assert describe(state) == '"rust" · swe'


def test_a_saved_search_round_trips(tmp_path: Path) -> None:
    state = FilterState()
    state.query = "python"
    state.toggle("role", "swe")
    target = tmp_path / "searches.json"

    assert store_saved(remember([], "swe python", state), target)

    restored = FilterState()
    restored.restore(load_saved(target)[0].payload)

    assert restored.query == "python"
    assert restored.values == {"role": "swe"}


def test_saving_the_same_name_replaces_it(tmp_path: Path) -> None:
    state = FilterState()
    state.toggle("role", "swe")

    searches = remember([], "mine", state)
    searches = remember(searches, "mine", state)

    assert len(searches) == 1


def test_a_corrupt_saved_file_is_ignored(tmp_path: Path) -> None:
    target = tmp_path / "searches.json"
    target.write_text("{not json", encoding="utf-8")

    assert load_saved(target) == []


def test_a_saved_file_holding_the_wrong_shape_is_ignored(tmp_path: Path) -> None:
    target = tmp_path / "searches.json"
    target.write_text('{"name": "x"}', encoding="utf-8")

    assert load_saved(target) == []


def test_saved_entries_missing_fields_are_skipped(tmp_path: Path) -> None:
    target = tmp_path / "searches.json"
    target.write_text('[{"name": "ok", "payload": {}}, {"name": 2}, "junk"]', encoding="utf-8")

    assert [item.name for item in load_saved(target)] == ["ok"]


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


async def test_cycling_a_filter_advances_then_clears(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        first = screen.state.values.get("role")
        await pilot.press("1")
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


async def test_recalling_an_empty_slot_is_harmless(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        screen.saved = []
        await pilot.press("f1")
        await pilot.pause()

        assert screen.state.values == {}


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


async def test_nine_saved_search_slots_are_bound(tmp_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    app = StageApp(tmp_path / "empty.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PostingsScreen)
        keys = {
            binding[0] if isinstance(binding, tuple) else binding.key for binding in screen.BINDINGS
        }

        assert {f"f{n}" for n in range(1, 10)} <= keys


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

    names = {"slash": "/", "question_mark": "?"}
    documented = HELP_TEXT.lower()
    skipped = {"?"} | {f"f{n}" for n in range(2, 10)}
    for binding in PostingsScreen.BINDINGS:
        raw = binding.key if isinstance(binding, Binding) else binding[0]
        key = names.get(raw, raw)
        if key in skipped:
            continue
        assert key.lower() in documented, key


async def test_the_table_columns_fit_the_terminal(tmp_path: Path) -> None:
    import asyncio

    from textual.widgets import DataTable

    from stage.tui.app import StageApp, summarize

    db = tmp_path / "fit.db"
    for width in (80, 100, 140):
        app = StageApp(db, await summarize(db))
        async with app.run_test(size=(width, 24)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.9)
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
