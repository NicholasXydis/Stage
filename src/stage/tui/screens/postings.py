from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static

from stage.domain import DEFAULT_WINDOW_DAYS, ExportFormat
from stage.domain.text import sanitize
from stage.normalize.location import display_location
from stage.tui.help import HelpOverlay
from stage.tui.safe import cell, quoted, told
from stage.tui.state import (
    DEBOUNCE_SECONDS,
    PAGE_SIZE,
    FilterState,
    filter_rows,
)

if TYPE_CHECKING:
    from stage.domain import Job
    from stage.storage import AsyncRepository

COLUMNS = ("#", "Age", "Company", "Title", "Location")

HELP_TEXT = """[b]Find[/b]
  /        search titles and bodies
  f        filters, arrows then enter
  c        clear filters and search
  r        reload from the database

[b]Act on a row[/b]
  up down  move between postings
  space    mark a row
  w        expand the description
  o        open marked rows, or this one
  e        export what you see
  g        change the export format

[b]How many rows[/b]
  m        show 200 more
  M        show 200 fewer

[b]Other screens[/b]
  y        sync
  t        statistics
  v        review
  b        board health
  A        about
  ctrl+t   change the theme
  q        quit

[dim]? closes this[/dim]"""
NARROW = 80


def age_label(first_seen: datetime, now: datetime) -> str:
    days = (now - first_seen).days
    return "new" if days <= 1 else f"{days}d"


class PostingsScreen(HelpOverlay, Screen[None]):
    HELP_TEXT = HELP_TEXT
    BINDINGS = [
        Binding("slash", "search", "search"),
        Binding("f", "filters", "filter"),
        Binding("space", "mark", "mark", show=False),
        Binding("o", "open", "open"),
        Binding("w", "expand", "read", show=False),
        Binding("e", "export", "export"),
        Binding("question_mark", "help", "keys"),
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
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state = FilterState()
        self._jobs: tuple[Job, ...] = ()
        self._export_format = "csv"
        self._expanded = False
        self._total = 0
        self._pending: Timer | None = None
        self._marked: set[str] = set()
        self._arming_export = False
        self._rows: tuple[tuple[str, str, str, bool], ...] = ()

    @property
    def repository(self) -> "AsyncRepository | None":
        from stage.tui.app import StageApp

        app = self.app
        return app.repository if isinstance(app, StageApp) else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        yield OptionList(id="filters", classes="hidden")
        yield Horizontal(
            Input(placeholder="search titles, employers, descriptions", id="search"),
            id="search-bar",
        )
        yield DataTable(id="results", cursor_type="row", zebra_stripes=False)
        yield Static("", id="detail")
        yield Static("", id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_column("#", width=4)
        table.add_column("Age", width=5)
        table.add_column("Company", width=22)
        table.add_column("Title", width=self._title_width())
        table.add_column("Location", width=18)
        self._render_filters()
        table.focus()
        self.refresh_results()

    def _title_width(self) -> int:
        fixed = 4 + 5 + 22 + 18 + 12
        return max(24, self.app.size.width - fixed)

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#chips", Static).set_class(busy, "busy")
        if busy:
            self.query_one("#chips", Static).update("[dim]searching…[/dim]")

    def _render_chips(self) -> None:
        if self._arming_export:
            self.query_one("#chips", Static).update(
                f"Export every match as [b]{self._export_format}[/b]?   "
                "[dim]e to confirm, g for another format, escape to cancel[/dim]"
            )
            return
        parts = [f"[reverse] {quoted(value)} [/reverse]" for _, value in self.state.active]
        if self.state.query:
            parts.insert(0, f'[reverse] "{quoted(self.state.query)}" [/reverse]')
        if self.state.only_new:
            parts.append("[reverse] new [/reverse]")
        if self.state.last_days != DEFAULT_WINDOW_DAYS:
            window = "any date" if self.state.last_days == 0 else f"{self.state.last_days}d"
            parts.append(f"[reverse] {window} [/reverse]")
        shown = " ".join(parts) if parts else "[dim]all postings[/dim]"
        count = (
            f"[b]{len(self._jobs)}[/b] of [b]{self._total}[/b]"
            if len(self._jobs) < self._total
            else f"[b]{self._total}[/b] posting(s)"
        )
        marked = f"   [b]{len(self._marked)}[/b] marked" if self._marked else ""
        clear = (
            "   [dim]c to clear[/dim]"
            if self.state.active or self.state.query or self._is_narrowed()
            else ""
        )
        self.query_one("#chips", Static).update(f"{shown}   {count}{marked}{clear}")

    def _is_narrowed(self) -> bool:
        return self.state.only_new or self.state.last_days != DEFAULT_WINDOW_DAYS

    def _render_filters(self) -> None:
        panel = self.query_one("#filters", OptionList)
        highlighted = panel.highlighted
        job = self._selected()
        self._rows = filter_rows(self.state, job.company if job else None)
        panel.clear_options()
        for _, _, label, chosen in self._rows:
            panel.add_option(cell(f"{'[x]' if chosen else '[ ]'} {label}"))
        if highlighted is not None and self._rows:
            panel.highlighted = min(highlighted, len(self._rows) - 1)

    @work(exclusive=True)
    async def refresh_results(self) -> None:
        from stage.services.query import list_jobs, search_jobs

        repo = self.repository
        if repo is None:
            return
        self._set_busy(True)
        filters = self.state.as_filters()
        if self.state.only_new:
            since = await repo.previous_sync_at()
            if since is not None:
                filters = replace(filters, first_seen_after=since)
        if self.state.query.strip():
            listing = await search_jobs(
                repo, self.state.query, filters, window_days=self.state.window_days
            )
        else:
            listing = await list_jobs(repo, filters, window_days=self.state.window_days)
        self._jobs = listing.jobs
        self._total = listing.total_matching
        self._fill(listing.jobs)
        self._set_busy(False)

    def _fill(self, jobs: "tuple[Job, ...]") -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        now = datetime.now(UTC)
        width = self._title_width()
        for position, job in enumerate(jobs, start=1):
            marker = "*" if job.id in self._marked else " "
            table.add_row(
                cell(f"{marker}{position}"),
                cell(age_label(job.first_seen, now)),
                cell(job.company, 20),
                cell(job.title_raw, width),
                cell(display_location(job.location_raw) or "-", 18),
            )
        self._render_chips()
        self._show_detail()

    def _selected(self) -> "Job | None":
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if not self._jobs or row < 0 or row >= len(self._jobs):
            return None
        return self._jobs[row]

    def _show_detail(self) -> None:
        from stage.domain.text import summary

        job = self._selected()
        panel = self.query_one("#detail", Static)
        if job is None:
            panel.update(
                "[dim]Nothing selected. Press [b]/[/b] to search, "
                "[b]f[/b] for filters, [b]?[/b] for help.[/dim]"
            )
            return
        facts = "  [dim]|[/dim]  ".join(
            quoted(part)
            for part in (
                display_location(job.location_raw) or None,
                job.role.value if job.role.value != "unknown" else None,
                job.term if job.term != "unknown" else None,
                job.language.value.upper() if job.language.value != "unknown" else None,
                age_label(job.first_seen, datetime.now(UTC)),
            )
            if part
        )
        body = quoted(summary(job.description, len(job.description) if self._expanded else 360))
        if not body:
            body = "[dim]This board publishes no description. Press o to open it.[/dim]"
        elif not self._expanded and len(job.description) > 360:
            body = f"{body}[dim] … press w for the full description[/dim]"
        panel.update(
            f"[b]{quoted(job.title_raw)}[/b]\n"
            f"[$blue]{quoted(job.company)}[/$blue]   {facts}\n\n{body}"
        )

    def on_data_table_row_highlighted(self) -> None:
        self._show_detail()

    def on_input_changed(self, event: Input.Changed) -> None:
        timer = self._pending
        if timer is not None:
            timer.stop()
        self.state.query = event.value
        self.state.limit = PAGE_SIZE
        self._pending = self.set_timer(DEBOUNCE_SECONDS, self.refresh_results)

    def on_input_submitted(self) -> None:
        self._leave_search()

    def on_key(self, event: object) -> None:
        if getattr(event, "key", "") != "escape":
            return
        if self.close_help():
            stop = getattr(event, "stop", None)
            if callable(stop):
                stop()
            return
        if self._arming_export:
            self._arming_export = False
            self._render_chips()
            stop = getattr(event, "stop", None)
            if callable(stop):
                stop()
            return
        if not self.query_one("#filters", OptionList).has_class("hidden"):
            self._leave_filters()
            stop = getattr(event, "stop", None)
            if callable(stop):
                stop()
            return
        if not self.query_one("#search-bar").has_class("visible"):
            return
        self._leave_search()
        stop = getattr(event, "stop", None)
        if callable(stop):
            stop()

    def _leave_search(self) -> None:
        self.query_one("#search-bar").remove_class("visible")
        self.query_one("#results", DataTable).focus()

    def action_search(self) -> None:
        self.query_one("#search-bar").add_class("visible")
        self.query_one("#search", Input).focus()

    def action_expand(self) -> None:
        self._expanded = not self._expanded
        self.query_one("#detail").set_class(self._expanded, "expanded")
        self._show_detail()

    def action_filters(self) -> None:
        panel = self.query_one("#filters", OptionList)
        if panel.has_class("hidden"):
            self.close_help()
            self._render_filters()
            panel.remove_class("hidden")
            if panel.option_count and panel.highlighted is None:
                panel.highlighted = 0
            panel.focus()
            return
        self._leave_filters()

    def _leave_filters(self) -> None:
        self.query_one("#filters", OptionList).add_class("hidden")
        self.query_one("#results", DataTable).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if index < 0 or index >= len(self._rows):
            return
        name, value, _, _ = self._rows[index]
        self.state.choose(name, value)
        self._render_filters()
        self.refresh_results()

    def action_clear(self) -> None:
        self.state.clear()
        self._marked.clear()
        self.query_one("#search", Input).value = ""
        self._render_filters()
        self.refresh_results()

    def action_reload(self) -> None:
        self.refresh_results()

    def action_more(self) -> None:
        if len(self._jobs) >= self._total:
            told(self, "Every match is already listed.", "warning")
            return
        self.state.widen()
        self.refresh_results()

    def action_fewer(self) -> None:
        if self.state.show_all:
            self.state.show_all = False
            self.state.limit = PAGE_SIZE
            self.refresh_results()
            return
        if not self.state.narrow():
            return
        self.refresh_results()

    def action_mark(self) -> None:
        job = self._selected()
        if job is None:
            told(self, "Nothing selected.", "warning")
            return
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if job.id in self._marked:
            self._marked.discard(job.id)
        else:
            self._marked.add(job.id)
        self._fill(self._jobs)
        if 0 <= row < len(self._jobs):
            table.move_cursor(row=row)

    def _open_targets(self) -> "tuple[Job, ...]":
        if self._marked:
            return tuple(job for job in self._jobs if job.id in self._marked)
        job = self._selected()
        return (job,) if job is not None else ()

    def action_open(self) -> None:
        import webbrowser

        from stage.domain import web_url

        targets = self._open_targets()
        if not targets:
            told(self, "Nothing selected.", "warning")
            return
        opened = 0
        refused = 0
        for job in targets:
            url = web_url(job.apply_url_raw)
            if url is None:
                refused += 1
                continue
            if not webbrowser.open(url):
                told(self, "No browser available on this machine.", "warning")
                return
            opened += 1
        if refused:
            self.notify(
                f"{refused} posting(s) had an apply link that is not a plain "
                "http or https address, so they were not opened.",
                severity="error",
            )
        if not opened:
            return
        if opened == 1 and len(targets) == 1:
            told(self, f"Opened {sanitize(targets[0].company)}")
            return
        told(self, f"Opened {opened} posting(s)")

    def action_cycle_format(self) -> None:
        order = [item.value for item in ExportFormat]
        index = (order.index(self._export_format) + 1) % len(order)
        self._export_format = order[index]
        if self._arming_export:
            self._render_chips()
            return
        told(self, f"Export format is now {self._export_format}")

    def action_export(self) -> None:
        if not self._jobs:
            told(self, "Nothing to export.", "warning")
            return
        if not self._arming_export:
            self.close_help()
            self._arming_export = True
            self._render_chips()
            return
        self._arming_export = False
        self._render_chips()
        self._write_export()

    @work(exclusive=True)
    async def _write_export(self) -> None:
        from datetime import UTC, datetime

        from stage.services.export import ExportError, export_jobs, export_root

        repo = self.repository
        if repo is None or not self._jobs:
            told(self, "Nothing to export.", "warning")
            return
        fmt = ExportFormat(self._export_format)
        root = export_root()
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        try:
            result = await export_jobs(
                repo,
                self.state.as_filters(),
                fmt=fmt,
                destination=root / f"stage-{stamp}.{fmt.value}",
                force=True,
                window_days=self.state.window_days,
                query=self.state.query,
            )
        except (ExportError, OSError) as exc:
            told(self, f"Could not write the export: {sanitize(str(exc))}", "error")
            return
        told(self, f"Exported {result.count} posting(s) to {sanitize(str(result.path))}")

    def action_stats(self) -> None:
        from stage.tui.screens.stats import StatsScreen

        self.app.push_screen(StatsScreen())

    def action_review(self) -> None:
        from stage.tui.screens.review import ReviewScreen

        self.app.push_screen(ReviewScreen())

    def action_boards(self) -> None:
        from stage.tui.screens.boards import BoardsScreen

        self.app.push_screen(BoardsScreen())

    def action_sync(self) -> None:
        from stage.tui.screens.sync import SyncScreen

        self.app.push_screen(SyncScreen())

    def action_about(self) -> None:
        from stage.tui.screens.splash import SplashScreen

        app = self.app
        summary = getattr(app, "summary", "")
        app.push_screen(SplashScreen(summary or "Stage", dismiss_after=None))

    def dismiss_overlays(self) -> None:
        if not self.query_one("#filters", OptionList).has_class("hidden"):
            self._leave_filters()
        if self.query_one("#search-bar").has_class("visible"):
            self._leave_search()
        if self._arming_export:
            self._arming_export = False
            self._render_chips()
