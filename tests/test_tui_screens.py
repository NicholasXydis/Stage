import ast
import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Footer, Input, Static
from textual.worker import WorkerCancelled

from stage.domain import Job, LocationBucket, QuarantinedJob, RejectionReason
from stage.tui.app import StageApp
from stage.tui.screens.postings import PostingsScreen

NOW = datetime.now(UTC)
LONG = "Zyxwvut Holdings International Group Incorporated"


def _seed(path: Path, count: int = 3, description: str = "") -> Path:
    from stage.domain import SyncOutcome, SyncRun
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    title = "Senior Principal Staff Software Engineering Intern " * 3
    repo = SqliteRepository.connect(path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=tuple(
                Job(
                    id=f"greenhouse:zyx:{index}",
                    source="greenhouse",
                    company=LONG,
                    title_raw=f"{title}{index}",
                    title_normalized=f"intern {index}",
                    apply_url_raw=f"https://boards.example.test/{index}",
                    description=description,
                    first_seen=NOW,
                    last_seen=NOW,
                    location_raw="Montreal, QC, Canada; Toronto, ON, Canada",
                    location=LocationBucket.MONTREAL,
                )
                for index in range(count)
            ),
            quarantined=(
                QuarantinedJob(
                    id="greenhouse:zyx:q",
                    source="greenhouse",
                    company=LONG,
                    title_raw=title,
                    reason=RejectionReason.OUT_OF_SCOPE_LOCATION,
                    first_seen=NOW,
                    last_seen=NOW,
                    location_raw="Lyon, France",
                ),
            ),
        )
    )
    repo.record_sync_run(SyncRun(started_at=NOW, finished_at=NOW, outcome=SyncOutcome.SUCCESS))
    repo.close()
    return path


async def _settled(app: StageApp, pilot: Any) -> None:
    for _ in range(4):
        for worker in list(app.workers):
            with suppress(WorkerCancelled):
                await worker.wait()
        await pilot.pause()


def _said(app: StageApp) -> list[str]:
    return [str(note.message) for note in app._notifications]


def _text(widget: Static) -> str:
    return str(widget.render())


async def _postings(app: StageApp, pilot: Any) -> PostingsScreen:
    await _settled(app, pilot)
    screen = app.screen
    assert isinstance(screen, PostingsScreen)
    return screen


async def _search(app: StageApp, pilot: Any, query: str) -> PostingsScreen:
    screen = await _postings(app, pilot)
    await pilot.press("slash")
    screen.query_one("#search", Input).value = query
    await pilot.pause(0.3)
    await _settled(app, pilot)
    return screen


@pytest.mark.parametrize("size", [(60, 24), (80, 24), (100, 30), (200, 50)])
async def test_every_screen_fits_the_terminal(tmp_path: Path, size: tuple[int, int]) -> None:
    width, _ = size
    app = StageApp(_seed(tmp_path / "fit.db"), "")

    async with app.run_test(size=size) as pilot:
        await _postings(app, pilot)
        await pilot.press("s")
        await _settled(app, pilot)
        for key in ("", "l", "v", "b", "y", "t"):
            if key:
                await pilot.press("escape", key)
                await _settled(app, pilot)
            footer = app.screen.query_one(Footer)
            assert max(child.region.right for child in footer.children) <= width, key
            for table in app.screen.query(DataTable):
                used = sum(column.get_render_width(table) for column in table.columns.values())
                assert used < width, (key, table.id, used)


async def test_narrow_tables_drop_the_columns_the_detail_repeats(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "narrow.db"), "")

    async with app.run_test(size=(60, 24)) as pilot:
        await _postings(app, pilot)
        await pilot.press("s")
        await _settled(app, pilot)
        await pilot.press("l")
        await _settled(app, pilot)
        tracked = [str(c.label) for c in app.screen.query_one(DataTable).columns.values()]
        await pilot.press("escape", "b")
        await _settled(app, pilot)
        boards = [str(c.label) for c in app.screen.query_one(DataTable).columns.values()]
        await pilot.resize_terminal(120, 30)
        await _settled(app, pilot)
        wide = [str(c.label) for c in app.screen.query_one(DataTable).columns.values()]

    assert tracked == ["#", "Status", "Company", "Title"]
    assert "Platform" not in boards and "Platform" in wide


async def test_stats_bars_fit_a_narrow_terminal(tmp_path: Path) -> None:
    from rich.markup import render

    app = StageApp(_seed(tmp_path / "stats.db"), "")

    async with app.run_test(size=(60, 24)) as pilot:
        await _postings(app, pilot)
        await pilot.press("t")
        await _settled(app, pilot)
        lines = _text(app.screen.query_one("#stats-grid", Static)).split("\n")

    assert max(render(line).cell_len for line in lines) <= 56


async def test_a_blank_search_is_not_shown_as_a_search(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "blank.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _search(app, pilot, "   ")
        chips = _text(screen.query_one("#chips", Static))

    assert '"' not in chips and "c to clear" not in chips
    assert len(screen._jobs) == 3


async def test_a_search_with_nothing_searchable_says_why(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "symbols.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _search(app, pilot, "!!!")
        detail = _text(screen.query_one("#detail", Static))

    assert 'nothing in "!!!" could be searched' in detail
    assert "press c to clear it" in detail


async def test_a_search_that_drops_symbols_says_what_it_searched(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "dropped.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _search(app, pilot, "senior \U0001f680")
        chips = _text(screen.query_one("#chips", Static))

    assert 'was searched as "senior"' in chips and len(screen._jobs) == 3


async def test_a_search_that_matches_nothing_names_the_way_out(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "none.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _search(app, pilot, "quantum")
        detail = _text(screen.query_one("#detail", Static))

    assert 'No posting matches "quantum"' in detail and "press c to clear" in detail


async def test_marking_a_row_changes_only_that_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = StageApp(_seed(tmp_path / "mark.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _postings(app, pilot)
        filled: list[object] = []
        monkeypatch.setattr(screen, "_fill", filled.append)
        await pilot.press("down", "space")
        await pilot.pause()
        table = screen.query_one("#results", DataTable)
        numbers = [str(table.get_row_at(row)[0]) for row in range(table.row_count)]
        chips = _text(screen.query_one("#chips", Static))

    assert filled == []
    assert numbers == [" 1", "*2", " 3"] and "1 marked" in chips


async def test_an_export_folder_that_is_missing_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nowhere"
    monkeypatch.setenv("STAGE_EXPORT_DIR", str(missing))
    app = StageApp(_seed(tmp_path / "export.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        await _postings(app, pilot)
        await pilot.press("e", "e")
        await _settled(app, pilot)

    said = _said(app)[-1]
    assert str(missing) in said and "STAGE_EXPORT_DIR" in said and "--out" not in said


async def test_sync_warnings_name_the_failure_without_class_names(tmp_path: Path) -> None:
    from stage.domain import CompanyFailed, SourceBlocked, SourceFailed
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "warnings.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SyncScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._absorb(
            CompanyFailed(
                source="lever",
                company="Acme",
                error="HttpStatusError: api.lever.co returned 404",
                elapsed_ms=1.0,
            )
        )
        screen._absorb(SourceFailed(source="ashby", error="BreakerOpenError: ashby is resting"))
        screen._absorb(
            SourceBlocked(
                source="workday",
                bucket="workday:wd5",
                blocked_until=NOW + timedelta(hours=1),
                remaining_s=3600.0,
                reason="BucketBlockedError: 429 from wd5",
                consecutive_failures=2,
            )
        )
        shown = _text(screen.query_one("#warnings", Static))

    assert "lever/Acme: api.lever.co returned 404" in shown
    assert "ashby: ashby is resting" in shown
    assert "workday blocked until" in shown and "429 from wd5" in shown
    assert "Error" not in shown


async def test_the_sync_summary_counts_closed_and_rejected_postings(tmp_path: Path) -> None:
    from stage.domain import SyncFinished, SyncOutcome
    from stage.tui.screens.sync import SyncScreen

    app = StageApp(tmp_path / "summary.db", "")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = SyncScreen()
        app.push_screen(screen)
        await pilot.pause()
        screen._absorb(
            SyncFinished(
                outcome=SyncOutcome.PARTIAL,
                added=4,
                updated=7,
                closed=1,
                failed_sources=("lever",),
                elapsed_ms=1.0,
                quarantined=3,
            )
        )
        chips = _text(screen.query_one("#chips", Static))
        detail = _text(screen.query_one("#detail", Static))

    assert "partial: 4 added, 7 updated, 1 closed, 3 rejected" in chips
    assert "Rejected postings are kept" in detail


async def test_a_board_error_is_shown_in_plain_words(tmp_path: Path) -> None:
    from stage.tui.screens.boards import BoardRow, BoardsScreen

    app = StageApp(tmp_path / "boards.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = BoardsScreen()
        app.push_screen(screen)
        await _settled(app, pilot)
        screen._rows = (
            BoardRow(
                "failing",
                "Acme",
                "lever",
                "lever:acme",
                error="HttpStatusError: api.lever.co returned 404",
                failures=3,
            ),
        )
        screen._show_detail()
        detail = _text(screen.query_one("#detail", Static))

    assert "Last error (3 failures in a row): api.lever.co returned 404" in detail
    assert "HttpStatusError" not in detail


async def test_review_shows_employers_before_the_rejected_postings_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import quarantine

    gate = asyncio.Event()
    listed = quarantine.list_quarantined

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await gate.wait()
        return await listed(*args, **kwargs)

    monkeypatch.setattr(quarantine, "list_quarantined", slow)
    app = StageApp(_seed(tmp_path / "review.db"), "")

    async with app.run_test(size=(120, 40)) as pilot:
        await _postings(app, pilot)
        await pilot.press("v")
        for _ in range(4):
            await pilot.pause()
        screen = app.screen
        early = screen.query_one("#unregistered", DataTable).row_count
        waiting = _text(screen.query_one("#chips", Static))
        gate.set()
        await _settled(app, pilot)
        late = screen.query_one("#quarantine", DataTable).row_count

    assert early == 1 and "loading rejected postings" in waiting and late == 1


async def test_loading_more_review_rows_reuses_the_employer_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import coverage

    counted: list[bool] = []
    measured = coverage.coverage

    async def counting(*args: Any, **kwargs: Any) -> Any:
        counted.append(True)
        return await measured(*args, **kwargs)

    monkeypatch.setattr(coverage, "coverage", counting)
    app = StageApp(_seed(tmp_path / "more.db"), "")

    async with app.run_test(size=(120, 40)) as pilot:
        await _postings(app, pilot)
        await pilot.press("v")
        await _settled(app, pilot)
        await pilot.press("m")
        await _settled(app, pilot)
        await pilot.press("r")
        await _settled(app, pilot)

    assert len(counted) == 2


def test_an_error_from_another_layer_reads_as_a_sentence() -> None:
    from stage.tui.safe import sentence

    assert sentence("ReadTimeout: ") == "The answer timed out."
    assert sentence("the list is read-only") == "The list is read-only."
    assert sentence("Already done!") == "Already done!"
    assert (
        sentence("boards.yaml: line 3 is not a mapping") == "boards.yaml: line 3 is not a mapping."
    )


def _old(path: Path) -> Path:
    from stage.domain import SyncOutcome, SyncRun
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    seen = NOW - timedelta(days=40)
    repo = SqliteRepository.connect(path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=seen,
            jobs=(
                Job(
                    id="greenhouse:acme:old",
                    source="greenhouse",
                    company="Acme",
                    title_raw="Data Intern",
                    title_normalized="data intern",
                    apply_url_raw="https://boards.example.test/old",
                    description="",
                    first_seen=seen,
                    last_seen=seen,
                    location_raw="Montreal, QC",
                    location=LocationBucket.MONTREAL,
                ),
            ),
        )
    )
    repo.record_sync_run(SyncRun(started_at=NOW, finished_at=NOW, outcome=SyncOutcome.SUCCESS))
    repo.close()
    return path


async def test_an_empty_window_says_how_to_widen_it(tmp_path: Path) -> None:
    app = StageApp(_old(tmp_path / "old.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _postings(app, pilot)
        detail = _text(screen.query_one("#detail", Static))
        await pilot.press("s")
        await pilot.pause()

    assert "No posting was first seen in the last 14 days" in detail
    assert _said(app) == ["No posting from the last 14 days. Press f for a longer window."]


async def test_filters_that_match_nothing_say_how_to_change_them(tmp_path: Path) -> None:
    app = StageApp(_old(tmp_path / "filtered.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _postings(app, pilot)
        screen.state.choose("last_days", "7")
        screen.refresh_results()
        await _settled(app, pilot)
        detail = _text(screen.query_one("#detail", Static))
        await pilot.press("o")
        await pilot.pause()

    assert "No posting matches these filters. Press f to change them" in detail
    assert _said(app) == ["No posting matches. Press c to clear the search and filters."]


async def test_an_export_the_disk_refuses_names_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import export

    async def refuse(*_: object, **__: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setenv("STAGE_EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr(export, "export_jobs", refuse)
    app = StageApp(_seed(tmp_path / "refused.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        await _postings(app, pilot)
        await pilot.press("e", "e")
        await _settled(app, pilot)

    assert _said(app)[-1] == (
        f"Could not write the export to {tmp_path} (Permission denied). "
        "Check the folder is writable and has room."
    )


async def test_review_reports_a_busy_database_in_the_shared_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from stage.tui.screens.review import ReviewScreen

    class Busy:
        async def record_coverage_classification(self, _: object) -> bool:
            raise sqlite3.OperationalError("database is locked")

        async def clear_coverage_classification(self, _: str) -> bool:
            raise sqlite3.OperationalError("database is locked")

    app = StageApp(tmp_path / "busy.db", "")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = ReviewScreen()
        monkeypatch.setattr(screen, "load", lambda *_, **__: None)
        app.push_screen(screen)
        await pilot.pause()
        monkeypatch.setattr(ReviewScreen, "repository", property(lambda _: Busy()))
        monkeypatch.setattr(screen, "selected_employer", lambda: "Acme")
        await type(screen).action_classify.__wrapped__(screen, "feed-only")  # type: ignore[attr-defined]
        screen._filed = "Acme"
        await type(screen).action_unclassify.__wrapped__(screen)  # type: ignore[attr-defined]

    said = _said(app)
    assert said[0].startswith("Could not record Acme. The database at")
    assert said[1].startswith("Could not clear Acme. The database at")
    assert all("busy.db is busy with another Stage process" in line for line in said)


async def test_a_registry_review_cannot_read_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.companies import RegistryError

    def broken(_: object) -> None:
        raise RegistryError("boards.yaml: line 3 is not a mapping")

    monkeypatch.setattr("stage.companies.load_boards", broken)
    app = StageApp(_seed(tmp_path / "registry.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        await _postings(app, pilot)
        await pilot.press("v")
        await _settled(app, pilot)
        detail = _text(app.screen.query_one("#detail", Static))
        await pilot.press("escape", "b")
        await _settled(app, pilot)
        boards = _text(app.screen.query_one("#detail", Static))

    assert detail == boards == "boards.yaml: line 3 is not a mapping."


async def test_sync_problems_are_said_in_plain_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from stage.companies import PersonalBoards
    from stage.services import sync
    from stage.tui.screens.sync import SyncScreen

    def own_problem(_: object) -> tuple[tuple[()], PersonalBoards]:
        return (), PersonalBoards(path=tmp_path, problem="boards.yaml could not be read")

    async def locked(*_: object, **__: object) -> AsyncIterator[object]:
        if tmp_path.exists():
            raise sqlite3.OperationalError("database is locked")
        yield None

    monkeypatch.setattr("stage.companies.load_boards", own_problem)
    monkeypatch.setattr(sync, "sync", locked)
    app = StageApp(tmp_path / "problems.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = SyncScreen()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("s")
        await _settled(app, pilot)
        chips = _text(screen.query_one("#chips", Static))

    assert _said(app) == ["Your own boards were left out. boards.yaml could not be read."]
    assert "problems.db is busy with another Stage process" in chips


def _strings(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


def test_no_tui_text_carries_a_semicolon_or_a_class_name() -> None:
    root = Path("src/stage/tui")
    names = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]*(Error|Exception)\b")
    for path in sorted(root.rglob("*.py")):
        for text in _strings(path):
            assert ";" not in text, (path.name, text)
            assert not names.search(text), (path.name, text)


def _crowded(path: Path, count: int = 40) -> Path:
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    repo = SqliteRepository.connect(path)
    repo.apply_source_batch(
        SourceBatch(
            source="greenhouse",
            run_started_at=NOW,
            jobs=tuple(
                Job(
                    id=f"greenhouse:zyx{index}:1",
                    source="greenhouse",
                    company=f"{LONG} {index}",
                    title_raw="Software Engineering Intern",
                    title_normalized="software engineering intern",
                    apply_url_raw=f"https://boards.example.test/{index}",
                    description="",
                    first_seen=NOW,
                    last_seen=NOW,
                )
                for index in range(count)
            ),
            quarantined=tuple(
                QuarantinedJob(
                    id=f"greenhouse:zyx{index}:q",
                    source="greenhouse",
                    company=f"{LONG} {index}",
                    title_raw="Senior Staff Engineer",
                    reason=RejectionReason.NOT_AN_INTERNSHIP,
                    first_seen=NOW,
                    last_seen=NOW,
                )
                for index in range(count)
            ),
        )
    )
    repo.close()
    return path


@pytest.mark.parametrize("width", [60, 100])
async def test_review_tables_fit_beside_their_scrollbar(tmp_path: Path, width: int) -> None:
    app = StageApp(_crowded(tmp_path / "crowded.db"), "")

    async with app.run_test(size=(width, 24)) as pilot:
        await _postings(app, pilot)
        await pilot.press("v")
        await _settled(app, pilot)
        employers = app.screen.query_one("#unregistered", DataTable)
        overflow = [(employers.row_count, employers.max_scroll_x)]
        await pilot.press("tab", "right")
        await _settled(app, pilot)
        rejected = app.screen.query_one("#quarantine", DataTable)
        overflow.append((rejected.row_count, rejected.max_scroll_x))

    assert overflow == [(40, 0), (40, 0)]


@pytest.mark.parametrize("keys", [("r",), ("l",), ("t",), ("v",), ("b",), ("s",), ("e", "e")])
async def test_a_database_that_fails_mid_session_is_said_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys: tuple[str, ...]
) -> None:
    import sqlite3

    from stage.storage.writer import DatabaseWriter

    async def malformed(*_: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setenv("STAGE_EXPORT_DIR", str(tmp_path))
    app = StageApp(_seed(tmp_path / "malformed.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        await _postings(app, pilot)
        await pilot.press("s")
        await _settled(app, pilot)
        monkeypatch.setattr(DatabaseWriter, "run", malformed)
        await pilot.press(*keys)
        await _settled(app, pilot)

    said = _said(app)[-1]
    assert "malformed.db is damaged or is not a Stage database" in said
    assert "Error" not in said and "image is malformed" not in said


async def test_invisible_characters_do_not_explain_a_search_as_itself(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "invisible.db"), "")

    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _search(app, pilot, "​Senior​")
        chips = _text(screen.query_one("#chips", Static))

    assert "searched as" not in chips and len(screen._jobs) == 3


async def test_the_network_check_does_not_freeze_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from stage.tui.screens.sync import SyncScreen

    on_the_loop: list[bool] = []

    def offline() -> bool:
        on_the_loop.append(threading.current_thread() is threading.main_thread())
        return False

    monkeypatch.setattr("stage.http.online", offline)
    app = StageApp(tmp_path / "offline.db", "")

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = SyncScreen()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("s")
        await _settled(app, pilot)
        chips = _text(screen.query_one("#chips", Static))

    assert on_the_loop == [False] and "No internet connection" in chips


async def test_a_picker_on_a_short_terminal_does_not_squeeze_the_screen(tmp_path: Path) -> None:
    app = StageApp(_seed(tmp_path / "short.db"), "")

    async with app.run_test(size=(60, 24)) as pilot:
        await _postings(app, pilot)
        await pilot.press("s", "down", "p", "down", "h")
        await _settled(app, pilot)
        await pilot.press("l")
        await _settled(app, pilot)
        await pilot.press("enter")
        await _settled(app, pilot)
        chips = app.screen.query_one("#chips", Static).region.height
        table = app.screen.query_one(DataTable)
        squeezed = (app.screen.show_vertical_scrollbar, table.max_scroll_x)

    assert chips > 2 and squeezed == (False, 0)
