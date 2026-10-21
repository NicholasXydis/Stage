import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, OptionList, Static

from stage.domain import Job, LocationBucket
from stage.domain.tracker import (
    ApplicationStatus,
    Snapshot,
    StatusChange,
    TrackedPosting,
)
from stage.services.tracker import tracker_path
from stage.storage.tracker import TrackerStore
from stage.tui.app import StageApp
from stage.tui.screens.applications import ApplicationsScreen
from stage.tui.screens.postings import PostingsScreen

NOW = datetime.now(UTC)
TODAY = NOW.astimezone().date()
SIZE = (120, 34)


def _id(name: str) -> str:
    return f"greenhouse:acme:{name}"


def _job(name: str, hours: int, url: str = "") -> Job:
    seen = NOW - timedelta(hours=hours)
    return Job(
        id=_id(name),
        source="greenhouse",
        company="Acme",
        title_raw=f"Intern {name}",
        title_normalized=f"intern {name}",
        apply_url_raw=url or f"https://example.com/{name}",
        description="",
        first_seen=seen,
        last_seen=seen,
        location_raw="Montreal, QC",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
    )


def _store(path: Path, jobs: list[Job] | None = None) -> Path:
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    rows = jobs or [_job(name, hours) for hours, name in enumerate("abcde", start=1)]
    repository = SqliteRepository.connect(path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(rows))
    )
    repository.close()
    return path


def _entry(
    name: str,
    status: ApplicationStatus,
    *,
    hours: int = 1,
    note: str = "",
    url: str = "",
    steps: tuple[ApplicationStatus, ...] = (),
) -> TrackedPosting:
    moment = NOW - timedelta(hours=hours)
    history = tuple(StatusChange(step, TODAY, moment) for step in (*steps, status))
    snapshot = Snapshot(
        "Acme",
        f"Intern {name}",
        "Montreal, QC",
        url or f"https://example.com/{name}",
        "greenhouse",
        "",
    )
    return TrackedPosting(_id(name), status, TODAY, moment, note, snapshot, history)


def _seed(*entries: TrackedPosting) -> None:
    with TrackerStore.connect(tracker_path()) as store:
        store.write(entries, [(e.posting_id, c) for e in entries for c in e.history], {})


def _entries() -> dict[str, TrackedPosting]:
    if not tracker_path().exists():
        return {}
    with TrackerStore.connect(tracker_path()) as store:
        return {entry.posting_id: entry for entry in store.entries()}


async def _settle(app: StageApp, pilot: Pilot[Any]) -> None:
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


def _messages(app: StageApp) -> list[str]:
    return [str(note.message) for note in app._notifications]


def _plain(widget: Static) -> str:
    return str(widget.render())


def _untracked(screen: PostingsScreen) -> bool:
    return screen.state.untracked


def _title(screen: PostingsScreen, row: int) -> str:
    table = screen.query_one("#results", DataTable)
    return str(table.get_row_at(row)[3])


async def _postings(app: StageApp, pilot: Pilot[Any]) -> PostingsScreen:
    await _settle(app, pilot)
    screen = app.screen
    assert isinstance(screen, PostingsScreen)
    return screen


async def test_s_saves_the_row_under_the_cursor_and_keeps_it(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("down", "s")
        await _settle(app, pilot)

        assert _entries()[_id("b")].status is ApplicationStatus.SAVED
        assert _title(screen, 1) == "[S] Intern b"
        assert screen.query_one("#results", DataTable).cursor_row == 1
        assert "Saved 1 posting. z undoes it." in _messages(app)


async def test_p_marks_every_marked_row_applied(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("space", "down", "space", "p")
        await _settle(app, pilot)

        entries = _entries()
        assert {entries[_id(n)].status for n in "ab"} == {ApplicationStatus.APPLIED}
        assert screen._marked == set()
        assert "Marked 2 postings applied. z undoes it." in _messages(app)


async def test_h_hides_counts_and_leaves_the_cursor_in_place(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("down", "h")
        await _settle(app, pilot)

        assert [job.id for job in screen._jobs] == [_id(n) for n in "acde"]
        assert screen._selected() is not None and screen._selected().id == _id("c")  # type: ignore[union-attr]
        assert "1 hidden" in _plain(screen.query_one("#chips", Static))


async def test_z_puts_everything_back_exactly(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.APPLIED, note="referral", steps=(ApplicationStatus.SAVED,)))
    before = _entries()
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("h")
        await _settle(app, pilot)
        assert _entries()[_id("a")].status is ApplicationStatus.HIDDEN
        await pilot.press("z")
        await _settle(app, pilot)

        assert _entries() == before
        assert screen._selected() is not None and screen._selected().id == _id("a")  # type: ignore[union-attr]
        assert "Undone: 1 posting back as before." in _messages(app)


async def test_z_after_a_first_save_untracks_and_then_has_nothing_left(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        await _postings(app, pilot)
        await pilot.press("s")
        await _settle(app, pilot)
        await pilot.press("z")
        await _settle(app, pilot)
        await pilot.press("z")
        await _settle(app, pilot)

        assert _entries() == {}
        assert _messages(app)[-1] == "Nothing to undo."


async def test_quick_keys_never_move_a_status_back(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.INTERVIEWING))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("s")
        await _settle(app, pilot)

        assert _entries()[_id("a")].status is ApplicationStatus.INTERVIEWING
        assert _title(screen, 0) == "[I] Intern a"
        assert _messages(app)[-1].startswith("Nothing to change; already marked interviewing.")


async def test_the_detail_names_the_status(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)

        assert "saved" in _plain(screen.query_one("#detail", Static))


async def test_the_not_acted_on_filter_hides_tracked_and_clears(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED), _entry("b", ApplicationStatus.HIDDEN))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("f")
        await pilot.pause()
        screen.query_one("#filters", OptionList).highlighted = 1
        await pilot.press("enter", "escape")
        await _settle(app, pilot)
        await type(screen).refresh_results.__wrapped__(screen)  # type: ignore[attr-defined]

        assert _untracked(screen)
        assert [job.id for job in screen._jobs] == [_id(n) for n in "cde"]
        assert "not acted on" in _plain(screen.query_one("#chips", Static))
        await pilot.press("c")
        await _settle(app, pilot)
        assert not _untracked(screen)
        assert len(screen._jobs) == 4


async def test_l_opens_your_applications_and_back_updates_the_markers(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _postings(app, pilot)
        await pilot.press("l")
        await _settle(app, pilot)
        applications = app.screen
        assert isinstance(applications, ApplicationsScreen)
        await pilot.press("enter")
        await pilot.pause()
        applications.query_one("#picker", OptionList).highlighted = 3
        await pilot.press("enter")
        await _settle(app, pilot)
        await pilot.press("escape")
        await _settle(app, pilot)

        assert app.screen is screen
        assert _title(screen, 0) == "[O] Intern a"


async def _applications(app: StageApp, pilot: Pilot[Any]) -> ApplicationsScreen:
    await _postings(app, pilot)
    await pilot.press("l")
    await _settle(app, pilot)
    screen = app.screen
    assert isinstance(screen, ApplicationsScreen)
    return screen


async def test_your_applications_lists_the_latest_change_first(db_path: Path) -> None:
    _seed(
        _entry("a", ApplicationStatus.APPLIED, hours=5),
        _entry("b", ApplicationStatus.INTERVIEWING, hours=1, steps=(ApplicationStatus.APPLIED,)),
        _entry("c", ApplicationStatus.HIDDEN),
    )
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        chips = _plain(screen.query_one("#chips", Static))

        assert [row.entry.posting_id for row in screen._rows] == [_id("b"), _id("a")]
        assert "2 tracked" in chips and "heard back from 1 of 2" in chips and "1 hidden" in chips
        assert f"applied {TODAY}, interviewing {TODAY}" in _plain(
            screen.query_one("#detail", Static)
        )


async def test_enter_changes_the_status_from_the_picker(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        picker = screen.query_one("#picker", OptionList)
        labels = [str(picker.get_option_at_index(i).prompt) for i in range(picker.option_count)]
        assert labels[0] == "[x] saved" and labels[-1] == "[ ] hidden"
        picker.highlighted = 2
        await pilot.press("enter")
        await _settle(app, pilot)

        assert _entries()[_id("a")].status is ApplicationStatus.INTERVIEWING
        assert "Marked Intern a interviewing." in _messages(app)
        assert picker.has_class("hidden")


async def test_f_shows_one_status_including_hidden(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED), _entry("c", ApplicationStatus.HIDDEN))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        await pilot.press("f")
        await pilot.pause()
        screen.query_one("#picker", OptionList).highlighted = 7
        await pilot.press("enter")
        await _settle(app, pilot)

        assert [row.entry.posting_id for row in screen._rows] == [_id("c")]
        assert "hidden" in _plain(screen.query_one("#chips", Static))


async def test_n_saves_and_clears_a_note_printed_literally(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        await pilot.press("n")
        await pilot.pause()
        assert screen.query_one("#note-bar").has_class("visible")
        screen.query_one("#note", Input).value = "[/bold]call Tuesday"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _entries()[_id("a")].note == "[/bold]call Tuesday"
        assert "[/bold]call Tuesday" in _plain(screen.query_one("#detail", Static))

        await pilot.press("n")
        await pilot.pause()
        screen.query_one("#note", Input).value = ""
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _entries()[_id("a")].note == ""
        assert "Cleared the note on Intern a." in _messages(app)


async def test_x_needs_two_presses_and_moving_forgets_it(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED, hours=2), _entry("b", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        await _applications(app, pilot)
        await pilot.press("x", "down", "up", "x")
        await _settle(app, pilot)
        assert set(_entries()) == {_id("a"), _id("b")}
        await pilot.press("x")
        await _settle(app, pilot)

        assert set(_entries()) == {_id("a")}
        assert "Stopped tracking Intern b." in _messages(app)


async def test_escape_closes_each_overlay_before_leaving(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        for key, target in (("enter", "#picker"), ("n", "#note-bar"), ("question_mark", "#help")):
            await pilot.press(key)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen, target
        assert screen.query_one("#picker", OptionList).has_class("hidden")
        await pilot.press("escape")
        await _settle(app, pilot)

        assert isinstance(app.screen, PostingsScreen)


async def test_your_applications_explains_an_empty_list(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        empty = _plain(screen.query_one("#detail", Static))
        await pilot.press("f")
        await pilot.pause()
        screen.query_one("#picker", OptionList).highlighted = 4
        await pilot.press("enter")
        await _settle(app, pilot)

        assert empty.startswith("Nothing tracked yet.")
        assert "Nothing tracked as offer." in _plain(screen.query_one("#detail", Static))
        assert not tracker_path().exists()


async def test_your_applications_reports_a_damaged_tracker(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    tracker_path().write_bytes(b"garbage" * 200)
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)

        assert "could not use your tracker" in _plain(screen.query_one("#detail", Static))
        assert any("could not use your tracker" in message for message in _messages(app))


async def test_o_opens_only_a_safe_link(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import webbrowser

    opened: list[str] = []

    def record(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    _seed(
        _entry("a", ApplicationStatus.SAVED, hours=2, url="javascript:alert(1)"),
        _entry("b", ApplicationStatus.SAVED, url="https://acme.test/b"),
    )
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        await _applications(app, pilot)
        await pilot.press("o", "down", "o")
        await _settle(app, pilot)

        assert opened == ["https://acme.test/b"]
        assert any("not a plain http or https address" in m for m in _messages(app))


async def test_keys_with_nothing_to_act_on_say_so(db_path: Path) -> None:
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        await _applications(app, pilot)
        await pilot.press("n", "o", "x")
        await pilot.pause()

        assert _messages(app).count("Nothing selected.") == 3


async def test_a_note_that_is_too_long_is_refused(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        await pilot.press("n")
        await pilot.pause()
        screen.query_one("#note", Input).value = "x" * 1_001
        await pilot.press("enter")
        await _settle(app, pilot)

        assert _entries()[_id("a")].note == ""
        assert any("at most 1,000 characters" in m for m in _messages(app))


async def test_no_browser_is_reported(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda _: False)
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        await _applications(app, pilot)
        await pilot.press("o")
        await pilot.pause()

        assert "No browser available on this machine." in _messages(app)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
async def test_a_tracker_that_turns_read_only_is_reported_everywhere(db_path: Path) -> None:
    _seed(_entry("a", ApplicationStatus.SAVED))
    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        screen = await _applications(app, pilot)
        tracker_path().chmod(0o400)
        try:
            await pilot.press("enter")
            await pilot.pause()
            screen.query_one("#picker", OptionList).highlighted = 1
            await pilot.press("enter")
            await _settle(app, pilot)
            await pilot.press("x", "x")
            await _settle(app, pilot)
            await pilot.press("escape")
            await _settle(app, pilot)
            await pilot.press("down", "s")
            await _settle(app, pilot)
        finally:
            tracker_path().chmod(0o600)

        failures = [m for m in _messages(app) if "could not use your tracker" in m]
        assert len(failures) == 3
        assert _entries()[_id("a")].status is ApplicationStatus.SAVED


async def test_a_posting_removed_since_the_listing_is_reported(db_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    app = StageApp(_store(db_path), "")
    async with app.run_test(size=SIZE) as pilot:
        await _postings(app, pilot)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (_id("a"),))
            conn.commit()
        await pilot.press("s")
        await _settle(app, pilot)

        assert "That posting is no longer stored; press r to reload." in _messages(app)
        assert _entries() == {}


async def test_the_quick_keys_need_a_posting(tmp_path: Path) -> None:
    app = StageApp(tmp_path / "empty.db", "")
    async with app.run_test(size=SIZE) as pilot:
        await _postings(app, pilot)
        await pilot.press("s", "p", "h")
        await pilot.pause()

        assert _messages(app).count("Nothing selected.") == 3
