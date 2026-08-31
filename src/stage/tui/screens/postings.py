from datetime import UTC, datetime
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, Static

from stage.domain import ExportFormat
from stage.domain.text import sanitize
from stage.normalize.location import display_location
from stage.tui.safe import cell, quoted, told
from stage.tui.state import (
    DEBOUNCE_SECONDS,
    FILTER_FIELDS,
    PAGE_SIZE,
    FilterState,
    describe,
    load_saved,
    remember,
    store_saved,
)

if TYPE_CHECKING:
    from stage.domain import Job
    from stage.storage import AsyncRepository

COLUMNS = ("#", "Age", "Company", "Title", "Location")

HELP_TEXT = """[b]Find[/b]
  /          search titles and bodies
  f          show the filter panel
  1 2 3      cycle the filters
  c          clear filters and search
  r          reload from the database

[b]Act on a row[/b]
  up down    move between postings
  w          expand the full description
  o          open it in your browser
  e          export what you see
  E          change the export format

[b]Saved searches[/b]
  s          save the current search
  F1 - F9    recall a saved search

[b]Other screens[/b]
  y  sync         t  statistics
  v  review       b  board health
  A  about        q  quit

[dim]? closes this[/dim]"""
NARROW = 80


def age_label(first_seen: datetime, now: datetime) -> str:
    days = (now - first_seen).days
    return "new" if days <= 1 else f"{days}d"


class PostingsScreen(Screen[None]):
    BINDINGS = [
        ("slash", "search", "search"),
        ("f", "filters", "filters"),
        ("1", "cycle('role')", "role"),
        ("2", "cycle('location')", "location"),
        ("3", "cycle('language')", "language"),
        ("o", "open", "open"),
        ("e", "export", "export"),
        ("E", "cycle_format", "export format"),
        ("c", "clear", "clear"),
        ("r", "reload", "reload"),
        ("m", "more", "load more"),
        ("s", "save", "save"),
        ("f1", "recall(0)", "recall 1"),
        ("f2", "recall(1)", "recall 2"),
        ("f3", "recall(2)", "recall 3"),
        ("f4", "recall(3)", "recall 4"),
        ("f5", "recall(4)", "recall 5"),
        ("f6", "recall(5)", "recall 6"),
        ("f7", "recall(6)", "recall 7"),
        ("f8", "recall(7)", "recall 8"),
        ("f9", "recall(8)", "recall 9"),
        ("t", "stats", "stats"),
        ("v", "review", "review"),
        ("b", "boards", "boards"),
        ("y", "sync", "sync"),
        ("w", "expand", "full description"),
        ("question_mark", "help", "help"),
        ("A", "about", "about"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state = FilterState()
        self.saved = load_saved()
        self._jobs: tuple[Job, ...] = ()
        self._export_format = "csv"
        self._expanded = False
        self._total = 0
        self._pending: Timer | None = None

    @property
    def repository(self) -> "AsyncRepository | None":
        from stage.tui.app import StageApp

        app = self.app
        return app.repository if isinstance(app, StageApp) else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        yield Static("", id="filters", classes="hidden")
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
        parts = [f"[reverse] {quoted(value)} [/reverse]" for _, value in self.state.active]
        if self.state.query:
            parts.insert(0, f'[reverse] "{quoted(self.state.query)}" [/reverse]')
        shown = " ".join(parts) if parts else "[dim]all postings[/dim]"
        count = (
            f"[b]{len(self._jobs)}[/b] of [b]{self._total}[/b]"
            if len(self._jobs) < self._total
            else f"[b]{self._total}[/b] posting(s)"
        )
        clear = "   [dim]c to clear[/dim]" if self.state.active or self.state.query else ""
        self.query_one("#chips", Static).update(f"{shown}   {count}{clear}")

    def _render_filters(self) -> None:
        lines = []
        for name, label, options in FILTER_FIELDS:
            chosen = self.state.values.get(name)
            rendered = "  ".join(
                f"[reverse] {option} [/reverse]" if option == chosen else option
                for option in options
                if option != "unknown"
            )
            lines.append(f"[b]{label:<9}[/b] {rendered}")
        self.query_one("#filters", Static).update("\n".join(lines))

    @work(exclusive=True)
    async def refresh_results(self) -> None:
        from stage.services.query import list_jobs, search_jobs

        repo = self.repository
        if repo is None:
            return
        self._set_busy(True)
        filters = self.state.as_filters()
        if self.state.query.strip():
            listing = await search_jobs(repo, self.state.query, filters)
        else:
            listing = await list_jobs(repo, filters, window_days=None)
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
            table.add_row(
                cell(str(position)),
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
        self.query_one("#filters").toggle_class("hidden")

    def action_cycle(self, name: str) -> None:
        options = next((values for field, _, values in FILTER_FIELDS if field == name), ())
        usable = [value for value in options if value != "unknown"]
        if not usable:
            return
        current = self.state.values.get(name)
        if current is None:
            self.state.values[name] = usable[0]
        else:
            index = usable.index(current) + 1 if current in usable else len(usable)
            if index < len(usable):
                self.state.values[name] = usable[index]
            else:
                self.state.values.pop(name, None)
        self.state.limit = PAGE_SIZE
        self._render_filters()
        self.refresh_results()

    def action_clear(self) -> None:
        self.state.clear()
        self.query_one("#search", Input).value = ""
        self._render_filters()
        self.refresh_results()

    def action_reload(self) -> None:
        self.refresh_results()

    def action_more(self) -> None:
        if len(self._jobs) >= self._total:
            told(self, "Every match is already listed.", "warning")
            return
        self.state.limit += PAGE_SIZE
        self.refresh_results()

    def action_open(self) -> None:
        import webbrowser

        from stage.domain import web_url

        job = self._selected()
        if job is None:
            told(self, "Nothing selected.", "warning")
            return
        url = web_url(job.apply_url_raw)
        if url is None:
            self.notify(
                "That posting's apply link is not a plain http or https address, "
                "so it was not opened.",
                severity="error",
            )
            return
        if not webbrowser.open(url):
            told(self, "No browser available on this machine.", "warning")
            return
        told(self, f"Opened {sanitize(job.company)}")

    def action_save(self) -> None:
        name = describe(self.state)
        self.saved = remember(self.saved, name, self.state)
        if store_saved(self.saved):
            told(self, f"Saved {sanitize(name)} - recall with F1-F9")
            return
        told(self, "Could not write the saved searches file.", "warning")

    def action_recall(self, index: int) -> None:
        if index >= len(self.saved):
            told(self, "No saved search in that slot.", "warning")
            return
        entry = self.saved[index]
        self.state.restore(entry.payload)
        self.query_one("#search", Input).value = self.state.query
        self._render_filters()
        self.refresh_results()
        told(self, f"Recalled {sanitize(entry.name)}")

    def action_cycle_format(self) -> None:
        order = [item.value for item in ExportFormat]
        index = (order.index(self._export_format) + 1) % len(order)
        self._export_format = order[index]
        told(self, f"Export format is now {self._export_format}")

    @work(exclusive=True)
    async def action_export(self) -> None:
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        from stage.services.export import ExportError, export_jobs

        repo = self.repository
        if repo is None or not self._jobs:
            told(self, "Nothing to export.", "warning")
            return
        fmt = ExportFormat(self._export_format)
        override = os.environ.get("STAGE_EXPORT_DIR", "").strip()
        root = Path(override).expanduser() if override else Path.cwd()
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        try:
            result = await export_jobs(
                repo,
                self.state.as_filters(),
                fmt=fmt,
                destination=root / f"stage-{stamp}.{fmt.value}",
                force=True,
                window_days=None,
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

    def action_help(self) -> None:
        panel = self.query_one("#help", Static)
        showing = not panel.has_class("visible")
        panel.set_class(showing, "visible")
        if showing:
            panel.update(HELP_TEXT)
