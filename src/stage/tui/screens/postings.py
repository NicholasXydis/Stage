import asyncio
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.events import Key
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static

from stage.domain import DEFAULT_WINDOW_DAYS, MAX_OPEN_AT_ONCE, ExportFormat, JobFilters
from stage.domain.text import paragraphs, sanitize
from stage.domain.tracker import MARKERS, ApplicationStatus
from stage.normalize.location import display_location
from stage.services.tracker import Change, ChangeReport, TrackerView
from stage.tui.base import HELP, Columns, HelpPanel, StageScreen, laid_out, picked, refill
from stage.tui.safe import cell, counted, headline, inline, quoted, sentence, told
from stage.tui.state import DEBOUNCE_SECONDS, PAGE_SIZE, FilterState, filter_rows

if TYPE_CHECKING:
    from stage.domain import Job
    from stage.domain.preferences import Preferences
    from stage.services.query import JobListing
    from stage.storage import AsyncRepository

AGE_WIDTH = 5

HELP_TEXT = """[b]Find[/b]
  /        search title, employer, text
  f        filters, arrows then enter
  c        clear filters and search
  r        reload from the database

[b]Act on a row[/b]
  up down  move between postings
  space    mark a row
  w        read the whole description
  o        open marked rows, or this one
  e        export every match
  g        change the export format
  m M      show 200 more, or 200 fewer

[b]Track[/b]
  s p h    save, mark applied, or hide
  z        undo the last s, p or h

[b]Other screens[/b]
  l        your applications
  y t v b  sync, stats, review, boards
  A        about
  ctrl+t   change the theme
  q        quit

[dim]? closes this[/dim]"""


def _tracked_notice(status: ApplicationStatus, report: ChangeReport) -> str:
    count = len(report.changed)
    if not count:
        kept = report.unchanged[0].status.value if report.unchanged else status.value
        return f"Nothing to change. Already marked {kept}, and l opens your applications."
    noun = counted(count, "posting")
    if status is ApplicationStatus.SAVED:
        line = f"Saved {noun}."
    elif status is ApplicationStatus.APPLIED:
        line = f"Marked {noun} applied."
    else:
        line = f"Hid {noun}."
    skipped = len(report.unchanged)
    if skipped:
        line += f" {skipped:,} already further along stayed as they were."
    return f"{line} z undoes it."


def _mine_notice(preferences: "Preferences", filters: JobFilters, left_out: int | None) -> str:
    notice = preferences.describe(filters)
    if left_out:
        notice += (
            f". {left_out:,} with no location given are left out. "
            "Turn off Only my preferences (f) to see them."
        )
    return notice


def age_label(first_seen: datetime, now: datetime) -> str:
    days = (now - first_seen).days
    return "new" if days <= 1 else f"{days}d"


class PostingsScreen(StageScreen[None]):
    HELP_TEXT = HELP_TEXT
    OVER_HELP = frozenset(
        {
            "search",
            "filters",
            "export",
            "expand",
            "applications",
            "stats",
            "review",
            "boards",
            "sync",
            "about",
        }
    )
    BINDINGS = [
        Binding("slash", "search", "search"),
        Binding("f", "filters", "filter"),
        Binding("o", "open", "open"),
        Binding("e", "export", "export"),
        Binding("l", "applications", "tracked"),
        HELP,
        Binding("space", "mark", "mark", show=False),
        Binding("w", "expand", "read", show=False),
        Binding("c", "clear", "clear", show=False),
        Binding("r", "reload", "reload", show=False),
        Binding("m", "more", "load more", show=False),
        Binding("M", "fewer", "load fewer", show=False),
        Binding("g", "cycle_format", "export format", show=False),
        Binding("t", "stats", "stats", show=False),
        Binding("v", "review", "review", show=False),
        Binding("b", "boards", "boards", show=False),
        Binding("y", "sync", "sync", show=False),
        Binding("A", "about", "about", show=False),
        Binding("s", "save", "save", show=False),
        Binding("p", "applied", "applied", show=False),
        Binding("h", "hide", "hide", show=False),
        Binding("z", "undo", "undo", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state = FilterState()
        self._jobs: tuple[Job, ...] = ()
        self._export_format = "csv"
        self._expanded = False
        self._never_synced = False
        self._total = 0
        self._searched: tuple[str, ...] = ()
        self._pending: Timer | None = None
        self._marked: set[str] = set()
        self._arming_export = False
        self._rows: tuple[tuple[str, str, str, bool], ...] = ()
        self._preferences: Preferences | None = None
        self._announce_mine = False
        self._tracker_warned = False
        self._statuses: Mapping[str, ApplicationStatus] = {}
        self._hidden = 0
        self._undo: tuple[Change, ...] = ()
        self._keep: tuple[str, int] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        yield OptionList(id="filters", classes="hidden")
        yield Horizontal(
            Input(placeholder="search titles, employers, descriptions", id="search"),
            id="search-bar",
        )
        yield DataTable(id="results", cursor_type="row", zebra_stripes=False)
        yield VerticalScroll(Static("", id="detail"), id="reader", can_focus=False)
        yield HelpPanel(id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self._table()
        refill(table, self._columns(0), ())
        self._load_preferences()
        self._render_filters()
        table.focus()
        self.refresh_results()

    def relayout(self) -> None:
        table = self._table()
        if not laid_out(table) or laid_out(table) == self._columns(len(self._jobs)):
            return
        self._hold()
        self._fill(self._jobs)

    def _columns(self, rows: int) -> Columns:
        number = max(4, len(str(rows)) + 1)
        room = max(40, self.app.size.width - number - AGE_WIDTH - 11)
        company = min(30, max(12, room // 4))
        location = min(26, max(12, room // 4))
        return (
            ("#", number),
            ("Age", AGE_WIDTH),
            ("Company", company),
            ("Title", room - company - location),
            ("Location", location),
        )

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#chips", Static).set_class(busy, "busy")
        if busy:
            self.say("[dim]searching...[/dim]")

    def _render_chips(self) -> None:
        if self._arming_export:
            self.say(
                f"Export every match as [b]{self._export_format}[/b]?   "
                "[dim]e to confirm, g for another format, escape to cancel[/dim]"
            )
            return
        query = self.state.query.strip()
        parts = [f"[reverse] {inline(value, 40)} [/reverse]" for _, value in self.state.active]
        if query:
            parts.insert(0, f'[reverse] "{inline(query, 40)}" [/reverse]')
        if self.state.only_new:
            parts.append("[reverse] new [/reverse]")
        days = self.state.window_days
        if days != (None if query else DEFAULT_WINDOW_DAYS):
            window = "any date" if days is None else f"{days}d"
            parts.append(f"[reverse] {window} [/reverse]")
        if self.state.mine:
            parts.append("[reverse] mine [/reverse]")
        if self.state.untracked:
            parts.append("[reverse] not acted on [/reverse]")
        shown = " ".join(parts) if parts else "[dim]all postings[/dim]"
        count = (
            f"[b]{len(self._jobs):,}[/b] of [b]{self._total:,}[/b]"
            if len(self._jobs) < self._total
            else f"[b]{counted(self._total, 'posting')}[/b]"
        )
        marked = f"   [b]{len(self._marked):,}[/b] marked" if self._marked else ""
        marked += f"   [dim]{self._hidden:,} hidden[/dim]" if self._hidden else ""
        clear = "   [dim]c to clear[/dim]" if self.state.active or query or self._narrowed() else ""
        note = self._search_note()
        if note and self._jobs:
            clear += f"\n[dim]{quoted(note)}[/dim]"
        self.say(f"{shown}   {count}{marked}{clear}")

    def _search_note(self) -> str:
        from stage.services.query import searched_as

        query = self.state.query.strip()
        return searched_as(query, self._searched, 40) if query else ""

    def _narrowed(self) -> bool:
        return (
            self.state.only_new
            or self.state.mine
            or self.state.untracked
            or self.state.last_days != DEFAULT_WINDOW_DAYS
        )

    def _load_preferences(self) -> None:
        from stage.services.preferences import load_preferences

        loaded = load_preferences()
        self._preferences = loaded.preferences
        for problem in loaded.problems:
            told(self, problem, "warning")

    def _has_preferences(self) -> bool:
        return self._preferences is not None and self._preferences.narrows

    def _render_filters(self) -> None:
        panel = self.query_one("#filters", OptionList)
        highlighted = panel.highlighted
        job = self._selected()
        self._rows = filter_rows(
            self.state, job.company if job else None, saved=self._has_preferences()
        )
        panel.clear_options()
        panel.add_options(
            cell(f"{'[x]' if chosen else '[ ]'} {label}") for _, _, label, chosen in self._rows
        )
        if highlighted is not None and self._rows:
            panel.highlighted = min(highlighted, len(self._rows) - 1)

    @work(exclusive=True)
    async def refresh_results(self) -> None:
        from stage.services.query import count_hidden, list_jobs, search_jobs

        repo = self.repository
        if repo is None:
            return
        try:
            self._set_busy(True)
        except NoMatches:
            return
        announce = self._announce_mine and self.state.mine
        self._announce_mine = False
        preferences = self._preferences
        query = self.state.query.strip()
        window = self.state.window_days
        try:
            filters, view = await self._scoped(repo, self.state.as_filters(preferences))
            listing: JobListing
            if query:
                listing = await search_jobs(repo, query, filters, window_days=window)
            else:
                listing = await list_jobs(repo, filters, window_days=window)
            hidden = await count_hidden(repo, filters, view.hidden, window_days=window, query=query)
        except asyncio.CancelledError:
            self._announce_mine = self._announce_mine or announce
            raise
        except sqlite3.Error as exc:
            self.database_failed(exc)
            return
        self._jobs = listing.jobs
        self._total = listing.total_matching
        self._never_synced = listing.last_sync_at is None
        self._searched = listing.terms
        self._statuses = view.statuses
        self._hidden = hidden
        try:
            self._fill(listing.jobs)
            self._set_busy(False)
        except NoMatches:
            return
        if announce and preferences is not None:
            told(self, _mine_notice(preferences, filters, listing.no_location))

    async def _scoped(
        self, repo: "AsyncRepository", filters: JobFilters
    ) -> tuple[JobFilters, TrackerView]:
        from stage.services.tracker import tracker_view

        view = await tracker_view(repo)
        if view.problem and not self._tracker_warned:
            self._tracker_warned = True
            told(self, view.problem, "warning")
        excluded = view.tracked if self.state.untracked else view.hidden
        filters = replace(filters, excluded_ids=filters.excluded_ids | excluded)
        if self.state.only_new:
            since = await repo.previous_sync_at()
            if since is not None:
                filters = replace(filters, first_seen_after=since)
        return filters, view

    def _number(self, position: int, job: "Job") -> Text:
        return cell(f"{'*' if job.id in self._marked else ' '}{position}")

    def _cells(self, position: int, job: "Job", columns: Columns, now: datetime) -> list[Text]:
        _, _, (_, company), (_, title), (_, location) = columns
        status = self._statuses.get(job.id)
        tag = MARKERS.get(status) if status is not None else None
        return [
            self._number(position, job),
            cell(age_label(job.first_seen, now)),
            cell(job.company, company),
            cell(f"{tag} {job.title_raw}" if tag else job.title_raw, title),
            cell(display_location(job.location_raw) or "-", location),
        ]

    def _fill(self, jobs: "tuple[Job, ...]") -> None:
        self._marked &= {job.id for job in jobs}
        columns = self._columns(len(jobs))
        now = datetime.now(UTC)
        keep, self._keep = self._keep, None
        cursor = None
        if keep is not None:
            cursor = next((at for at, job in enumerate(jobs) if job.id == keep[0]), keep[1])
        rows = (self._cells(at, job, columns, now) for at, job in enumerate(jobs, start=1))
        refill(self._table(), columns, rows, cursor)
        self._render_chips()
        self._show_detail()

    def _selected(self) -> "Job | None":
        return picked(self._table(), self._jobs)

    def _empty_detail(self) -> str:
        query = self.state.query.strip()
        if query and not self._searched:
            note = self._search_note() or f'Nothing in "{query}" can be searched.'
            said = f"{quoted(note)} Type letters or digits, or press [b]c[/b] to clear it."
        elif self._never_synced:
            said = "No postings yet. Run [b]stage sync[/b] in a terminal, or press [b]y[/b]."
        elif query:
            said = (
                f'No posting matches "{inline(query, 40)}". Every word has to match the '
                "start of a word, so drop one, change the filters with [b]f[/b], or press "
                "[b]c[/b] to clear."
            )
        elif self.state.active or self._narrowed():
            said = (
                "No posting matches these filters. Press [b]f[/b] to change them or "
                "[b]c[/b] to clear them."
            )
        else:
            said = (
                f"No posting was first seen in the last {DEFAULT_WINDOW_DAYS} days. Press "
                "[b]f[/b] for a longer window, or [b]y[/b] to sync."
            )
        return f"[dim]{said}[/dim]"

    def _show_detail(self) -> None:
        from stage.domain.text import first_line, graphemes

        job = self._selected()
        panel = self.query_one("#detail", Static)
        if job is None:
            panel.update(self._empty_detail())
            return
        status = self._statuses.get(job.id)
        width = max(20, self.app.size.width - 5)
        facts = "  [dim]|[/dim]  ".join(
            inline(part, width // 2)
            for part in (
                status.value if status is not None else None,
                display_location(job.location_raw) or None,
                job.role.value if job.role.value != "unknown" else None,
                job.term if job.term != "unknown" else None,
                job.language.value.upper() if job.language.value != "unknown" else None,
                age_label(job.first_seen, datetime.now(UTC)),
            )
            if part
        )
        whole = paragraphs(job.description)
        shown = whole if self._expanded else first_line(whole)
        clusters = graphemes(shown)
        cut = not self._expanded and len(clusters) > 360
        body = quoted("".join(clusters[:360] if cut else clusters))
        if cut:
            body = f"{body}[dim]...[/dim]"
        if not body:
            body = "[dim]This board publishes no description. Press o to open it.[/dim]"
        if self._expanded:
            facts = f"{facts}  [dim]|  w or escape goes back[/dim]"
        elif shown != whole:
            facts = f"{facts}  [dim]|  w reads it all[/dim]"
        title = inline(job.title_raw) if self._expanded else headline(job.title_raw, width)
        panel.update(
            f"[b]{title}[/b]\n"
            f"[$primary]{inline(job.company, width // 2)}[/$primary]   {facts}\n\n{body}"
        )

    def on_data_table_row_highlighted(self) -> None:
        self._show_detail()
        self.query_one("#reader", VerticalScroll).scroll_home(animate=False)

    def on_unmount(self) -> None:
        if self._pending is not None:
            self._pending.stop()

    def _refresh_soon(self) -> None:
        timer = self._pending
        if timer is not None:
            timer.stop()
        self._pending = self.set_timer(DEBOUNCE_SECONDS, self.refresh_results)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.state.query = event.value
        self.state.limit = PAGE_SIZE
        self._refresh_soon()

    def on_input_submitted(self) -> None:
        self._leave_search()

    def on_key(self, event: Key) -> None:
        if event.key == "escape" and self._escape():
            event.stop()

    def _escape(self) -> bool:
        if self.close_help() or self.close_overlay():
            return True
        if not self._expanded:
            return False
        self.action_expand()
        return True

    def _leave_search(self) -> None:
        self.query_one("#search-bar").remove_class("visible")
        self._table().focus()

    def action_search(self) -> None:
        self.dismiss_overlays()
        self.query_one("#search-bar").add_class("visible")
        self.query_one("#search", Input).focus()

    def action_expand(self) -> None:
        job = self._selected()
        if not self._expanded and (job is None or not paragraphs(job.description)):
            told(self, "This board publishes no description. Press o to open it.")
            return
        if not self._expanded:
            self.dismiss_overlays()
        self._expanded = not self._expanded
        reader = self.query_one("#reader", VerticalScroll)
        reader.set_class(self._expanded, "expanded")
        reader.can_focus = self._expanded
        self._show_detail()
        reader.scroll_home(animate=False)
        table = self._table()
        if self._expanded:
            reader.focus()
        else:
            table.focus()
        if self._jobs:
            table.call_after_refresh(table.move_cursor, row=table.cursor_row)

    def action_filters(self) -> None:
        panel = self.query_one("#filters", OptionList)
        if not panel.has_class("hidden"):
            self._leave_filters()
            return
        self.dismiss_overlays()
        self._render_filters()
        panel.remove_class("hidden")
        if panel.option_count and panel.highlighted is None:
            panel.highlighted = 0
        panel.focus()

    def _leave_filters(self) -> None:
        self.query_one("#filters", OptionList).add_class("hidden")
        self._table().focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if index < 0 or index >= len(self._rows):
            return
        name, value, _, _ = self._rows[index]
        if name == "mine" and not self.state.mine:
            self._load_preferences()
            if not self._has_preferences():
                told(self, "No preferences saved yet. Run stage prefs in a terminal.", "warning")
                self._render_filters()
                return
        self.state.choose(name, value)
        self._announce_mine = name == "mine" and self.state.mine
        self._render_filters()
        self._refresh_soon()

    def action_clear(self) -> None:
        self.state.clear()
        self._marked.clear()
        self.query_one("#search", Input).value = ""
        self._render_filters()
        self.refresh_results()

    def action_reload(self) -> None:
        self._reload()

    def _reload(self, _: object = None) -> None:
        self._hold()
        self.refresh_results()

    def action_more(self) -> None:
        if len(self._jobs) >= self._total:
            told(self, "Every match is already listed.", "warning")
            return
        self.state.widen()
        self._reload()

    def action_fewer(self) -> None:
        if self.state.show_all:
            self.state.show_all = False
            self.state.limit = PAGE_SIZE
        elif not self.state.narrow():
            told(self, "Already down to one page. Press m to show more.", "warning")
            return
        self._reload()

    def _hold(self) -> None:
        selected = self._selected()
        if selected is not None:
            self._keep = (selected.id, max(self._table().cursor_row, 0))

    def nothing_highlighted(self) -> None:
        if self._never_synced:
            said = "No postings yet. Press y to sync."
        elif self.state.query.strip() or self.state.active or self._narrowed():
            said = "No posting matches. Press c to clear the search and filters."
        else:
            said = (
                f"No posting from the last {DEFAULT_WINDOW_DAYS} days. Press f for a longer window."
            )
        told(self, said, "warning")

    def action_mark(self) -> None:
        job = self._selected()
        if job is None:
            self.nothing_highlighted()
            return
        self._marked ^= {job.id}
        table = self._table()
        row = table.cursor_row
        table.update_cell_at(Coordinate(row, 0), self._number(row + 1, job))
        self._render_chips()

    def _open_targets(self) -> "tuple[Job, ...]":
        if self._marked:
            return tuple(job for job in self._jobs if job.id in self._marked)
        job = self._selected()
        return (job,) if job is not None else ()

    def action_open(self) -> None:
        from stage.tui.liveness import open_postings

        targets = self._open_targets()
        if not targets:
            self.nothing_highlighted()
            return
        if len(targets) > MAX_OPEN_AT_ONCE:
            told(
                self,
                f"{len(targets):,} rows are marked, and Stage opens at most {MAX_OPEN_AT_ONCE} "
                "tabs at once. Press space on a marked row to unmark it.",
                "warning",
            )
            return
        open_postings(
            self, [(job.id, job.apply_url_raw, job.company) for job in targets], self._reload
        )

    def action_cycle_format(self) -> None:
        order = [item.value for item in ExportFormat]
        self._export_format = order[(order.index(self._export_format) + 1) % len(order)]
        if self._arming_export:
            self._render_chips()
            return
        told(self, f"Export format is now {self._export_format}")

    def action_export(self) -> None:
        if not self._jobs:
            self.nothing_highlighted()
            return
        if not self._arming_export:
            self.dismiss_overlays()
            self._arming_export = True
            self._render_chips()
            return
        self._arming_export = False
        self._render_chips()
        self._write_export()

    @work(exclusive=True, group="export")
    async def _write_export(self) -> None:
        from stage.services.export import ExportError, export_jobs, export_root

        repo = self.repository
        if repo is None or not self._jobs:
            self.nothing_highlighted()
            return
        fmt = ExportFormat(self._export_format)
        root = export_root()
        if not root.is_dir():
            told(
                self,
                f"The export folder {sanitize(str(root))} does not exist. Create it, or set "
                "STAGE_EXPORT_DIR to a folder that does.",
                "error",
            )
            return
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        try:
            every = replace(self.state.as_filters(self._preferences), limit=None)
            filters, _ = await self._scoped(repo, every)
            result = await export_jobs(
                repo,
                filters,
                fmt=fmt,
                destination=root / f"stage-{stamp}.{fmt.value}",
                force=True,
                window_days=self.state.window_days,
                query=self.state.query,
            )
        except ExportError as exc:
            told(self, sentence(str(exc)), "error")
            return
        except sqlite3.Error as exc:
            self.database_failed(exc, loading=False)
            return
        except OSError as exc:
            told(
                self,
                f"Could not write the export to {sanitize(str(root))} ({exc.strerror or exc}). "
                "Check the folder is writable and has room.",
                "error",
            )
            return
        told(self, f"Exported {counted(result.count, 'posting')} to {sanitize(str(result.path))}")

    def _push[R](self, screen: Screen[R], after: Callable[[R | None], None] | None = None) -> None:
        self.dismiss_overlays()
        self.app.push_screen(screen, after)

    def action_stats(self) -> None:
        from stage.tui.screens.stats import StatsScreen

        self._push(StatsScreen())

    def action_review(self) -> None:
        from stage.tui.screens.review import ReviewScreen

        self._push(ReviewScreen())

    def action_boards(self) -> None:
        from stage.tui.screens.boards import BoardsScreen

        self._push(BoardsScreen())

    def action_sync(self) -> None:
        from stage.tui.screens.sync import SyncScreen

        self._push(SyncScreen(), self._reload)

    def action_applications(self) -> None:
        from stage.tui.screens.applications import ApplicationsScreen

        self._push(ApplicationsScreen(), self._after_applications)

    def _after_applications(self, changed: bool | None) -> None:
        if changed:
            self._undo = ()
            self.refresh_results()

    def action_save(self) -> None:
        self._track(ApplicationStatus.SAVED)

    def action_applied(self) -> None:
        self._track(ApplicationStatus.APPLIED)

    def action_hide(self) -> None:
        self._track(ApplicationStatus.HIDDEN)

    def _track(self, status: ApplicationStatus) -> None:
        targets = self._open_targets()
        if not targets:
            self.nothing_highlighted()
            return
        self._hold()
        self._record(status, tuple(job.id for job in targets))

    @work(group="tracker")
    async def _record(self, status: ApplicationStatus, ids: tuple[str, ...]) -> None:
        from stage.services.tracker import TrackerProblemError, record_status

        repo = self.repository
        if repo is None:
            return
        try:
            report = await record_status(
                repo, ids, status, forward_only=status is not ApplicationStatus.HIDDEN
            )
        except TrackerProblemError as exc:
            told(self, str(exc), "error")
            return
        except sqlite3.Error as exc:
            self.database_failed(exc, loading=False)
            return
        if report.missing:
            told(self, "That posting is no longer stored. Press r to reload.", "warning")
            return
        if report.changed:
            self._undo = report.changed
        self._marked.clear()
        told(self, _tracked_notice(status, report), subject=ids)
        self.refresh_results()

    def action_undo(self) -> None:
        if not self._undo:
            told(self, "Nothing to undo.", "warning")
            return
        self._keep = (self._undo[0].entry.posting_id, max(self._table().cursor_row, 0))
        self._revert(self._undo)

    @work(group="tracker")
    async def _revert(self, changes: tuple[Change, ...]) -> None:
        from stage.services.tracker import TrackerProblemError, restore_unless_changed

        try:
            restored, kept = await asyncio.to_thread(restore_unless_changed, changes)
        except TrackerProblemError as exc:
            told(self, str(exc), "error")
            return
        self._undo = ()
        said = f"Undone: {counted(len(restored), 'posting')} back as before."
        if kept:
            left = "it as it is" if len(kept) == 1 else "them as they are"
            said = f"{said} {len(kept):,} changed again since, so Stage left {left}."
        subject = tuple(change.entry.posting_id for change in changes)
        told(self, said, "warning" if kept else "information", subject=subject)
        self.refresh_results()

    def action_about(self) -> None:
        from stage.tui.screens.splash import SplashScreen

        summary = getattr(self.app, "summary", "")
        self.app.push_screen(SplashScreen(summary or "Stage", dismiss_after=None))

    def close_overlay(self) -> bool:
        closed = False
        if not self.query_one("#filters", OptionList).has_class("hidden"):
            self._leave_filters()
            closed = True
        if self.query_one("#search-bar").has_class("visible"):
            self._leave_search()
            closed = True
        if self._arming_export:
            self._arming_export = False
            self._render_chips()
            closed = True
        return closed
