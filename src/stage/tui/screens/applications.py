from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static

from stage.domain.tracker import ApplicationStatus, PostingState, TrackerError, parse_note
from stage.normalize.location import display_location
from stage.services.tracker import TrackerListing, TrackerRow
from stage.tui.context import repository_of
from stage.tui.help import HelpOverlay
from stage.tui.safe import cell, quoted, told

if TYPE_CHECKING:
    from stage.storage import AsyncRepository

HELP_TEXT = """[b]Your applications[/b]
  up down  move between postings
  enter    change the status
  n        write or edit the note
  o        open the posting
  f        show one status, or hidden
  x        stop tracking, press twice
  r        reload from the database

[dim]? closes this   escape goes back[/dim]"""

STATUSES = tuple(ApplicationStatus)
FIXED_WIDTH = 4 + 12 + 10 + 20 + 7 + 14


class ApplicationsScreen(HelpOverlay, Screen[bool]):
    HELP_TEXT = HELP_TEXT
    BINDINGS = [
        Binding("n", "note", "note"),
        Binding("o", "open", "open"),
        Binding("f", "filter", "filter"),
        Binding("question_mark", "help", "keys"),
        Binding("escape", "back", "back"),
        Binding("x", "untrack", "stop tracking", show=False),
        Binding("r", "reload", "reload", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: tuple[TrackerRow, ...] = ()
        self._listing: TrackerListing | None = None
        self._status: ApplicationStatus | None = None
        self._picking = ""
        self._choices: tuple[ApplicationStatus | None, ...] = ()
        self._arming: str | None = None
        self._changed = False

    @property
    def repository(self) -> "AsyncRepository | None":
        return repository_of(self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        yield OptionList(id="picker", classes="hidden")
        yield Horizontal(
            Input(placeholder="a short note; leave it empty to clear it", id="note"),
            id="note-bar",
        )
        yield DataTable(id="results", cursor_type="row")
        yield Static("", id="detail")
        yield Static("", id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_column("#", width=4)
        table.add_column("Status", width=12)
        table.add_column("On", width=10)
        table.add_column("Company", width=20)
        table.add_column("Title", width=self._title_width())
        table.add_column("Posting", width=7)
        self.query_one("#chips", Static).update("[dim]loading...[/dim]")
        table.focus()
        self.load()

    def _title_width(self) -> int:
        return max(24, self.app.size.width - FIXED_WIDTH)

    @work(exclusive=True)
    async def load(self, keep: str | None = None) -> None:
        from stage.services.tracker import TrackerProblemError, tracker_listing

        repo = self.repository
        if repo is None:
            return
        try:
            listing = await tracker_listing(repo, self._status)
        except TrackerProblemError as exc:
            self._show_problem(str(exc))
            return
        try:
            self._fill(listing, keep)
        except NoMatches:
            return

    def _show_problem(self, problem: str) -> None:
        try:
            self.query_one("#chips", Static).update("[dim]your tracker could not be used[/dim]")
            self.query_one("#detail", Static).update(f"[red]{quoted(problem)}[/red]")
        except NoMatches:
            return
        told(self, problem, "error")

    def _fill(self, listing: TrackerListing, keep: str | None) -> None:
        self._listing = listing
        self._rows = listing.rows
        table = self.query_one("#results", DataTable)
        table.clear()
        width = self._title_width()
        for position, row in enumerate(self._rows, start=1):
            entry = row.entry
            table.add_row(
                cell(str(position)),
                cell(entry.status.value),
                cell(entry.on.isoformat()),
                cell(entry.snapshot.company, 20),
                cell(entry.snapshot.title, width),
                cell(row.state.value),
            )
        wanted = next(
            (index for index, row in enumerate(self._rows) if row.entry.posting_id == keep), None
        )
        if wanted is not None:
            table.move_cursor(row=wanted)
        self._render_chips()
        self._show_detail()

    def _render_chips(self) -> None:
        listing = self._listing
        if listing is None:
            return
        stats = listing.stats
        parts = [
            f"[b]{stats.tracked}[/b] tracked",
            f"{stats.applied_last_7} applied this week",
            f"heard back from {stats.heard_back} of {stats.applications}",
        ]
        if listing.hidden:
            parts.append(f"{listing.hidden} hidden")
        shown = "   ".join(parts)
        if self._status is not None:
            shown = f"[reverse] {self._status.value} [/reverse]   {shown}"
        hint = "   [dim]enter changes the status[/dim]" if self._rows else ""
        self.query_one("#chips", Static).update(f"{shown}{hint}")

    def _selected(self) -> TrackerRow | None:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if not self._rows or row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def _show_detail(self) -> None:
        panel = self.query_one("#detail", Static)
        row = self._selected()
        if row is None:
            panel.update(self._empty_message())
            return
        entry = row.entry
        snapshot = entry.snapshot
        place = display_location(snapshot.location) or "-"
        posting = {
            PostingState.OPEN: "still open",
            PostingState.CLOSED: "closed",
            PostingState.GONE: "no longer stored; this is the copy you kept",
        }[row.state]
        steps = ", ".join(f"{c.status.value} {c.on.isoformat()}" for c in entry.history)
        steps = f"   [dim]{quoted(steps)}[/dim]" if len(entry.history) > 1 else ""
        note = f"note: {quoted(entry.note)}" if entry.note else "[dim]no note; n adds one[/dim]"
        panel.update(
            f"[b]{quoted(snapshot.title)}[/b]\n"
            f"[$blue]{quoted(snapshot.company)}[/$blue]   {quoted(place)}  [dim]|[/dim]  "
            f"{posting}\n\n"
            f"{entry.status.value} on {entry.on.isoformat()}{steps}\n"
            f"{note}"
        )

    def _empty_message(self) -> str:
        if self._status is not None:
            return (
                f"[dim]Nothing tracked as {self._status.value}. "
                "Press [b]f[/b] to show another status.[/dim]"
            )
        return (
            "[dim]Nothing tracked yet. On the postings screen, [b]s[/b] saves a posting, "
            "[b]p[/b] marks it applied and [b]h[/b] hides it.[/dim]"
        )

    def on_data_table_row_highlighted(self) -> None:
        self._arming = None
        self._show_detail()

    def on_data_table_row_selected(self) -> None:
        if self._selected() is None:
            return
        current = self._selected()
        self._open_picker(
            "status",
            STATUSES,
            [status.value for status in STATUSES],
            current.entry.status if current else None,
        )

    def action_filter(self) -> None:
        choices: tuple[ApplicationStatus | None, ...] = (None, *STATUSES)
        labels = ["everything but hidden", *(status.value for status in STATUSES)]
        self._open_picker("filter", choices, labels, self._status)

    def _open_picker(
        self,
        mode: str,
        choices: tuple[ApplicationStatus | None, ...],
        labels: list[str],
        current: ApplicationStatus | None,
    ) -> None:
        self.dismiss_overlays()
        self.close_help()
        self._picking = mode
        self._choices = choices
        panel = self.query_one("#picker", OptionList)
        panel.clear_options()
        for choice, label in zip(choices, labels, strict=True):
            panel.add_option(cell(f"{'[x]' if choice == current else '[ ]'} {label}"))
        panel.remove_class("hidden")
        panel.highlighted = choices.index(current) if current in choices else 0
        panel.focus()

    def _close_picker(self) -> bool:
        panel = self.query_one("#picker", OptionList)
        if panel.has_class("hidden"):
            return False
        panel.add_class("hidden")
        self._picking = ""
        self.query_one("#results", DataTable).focus()
        return True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        mode = self._picking
        choices = self._choices
        self._close_picker()
        if index < 0 or index >= len(choices):
            return
        choice = choices[index]
        if mode == "filter":
            self._status = choice
            self.load()
            return
        row = self._selected()
        if row is not None and choice is not None and choice is not row.entry.status:
            self._change(row, choice, None)

    def action_note(self) -> None:
        row = self._selected()
        if row is None:
            told(self, "Nothing selected.", "warning")
            return
        self.dismiss_overlays()
        self.close_help()
        field = self.query_one("#note", Input)
        field.value = row.entry.note
        self.query_one("#note-bar").add_class("visible")
        field.focus()

    def _close_note(self) -> bool:
        bar = self.query_one("#note-bar")
        if not bar.has_class("visible"):
            return False
        bar.remove_class("visible")
        self.query_one("#results", DataTable).focus()
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        row = self._selected()
        self._close_note()
        if row is None:
            return
        try:
            note = parse_note(event.value)
        except TrackerError as exc:
            told(self, str(exc), "error")
            return
        if note == row.entry.note:
            return
        self._change(row, None, note)

    @work(group="tracker")
    async def _change(
        self, row: TrackerRow, status: ApplicationStatus | None, note: str | None
    ) -> None:
        from stage.services.tracker import TrackerProblemError, record_status

        repo = self.repository
        if repo is None:
            return
        try:
            report = await record_status(repo, [row.entry.posting_id], status, note=note)
        except TrackerProblemError as exc:
            told(self, str(exc), "error")
            return
        if not report.changed:
            return
        self._changed = True
        entry = report.changed[0].entry
        title = entry.snapshot.title
        if status is not None:
            told(self, f"Marked {title} {status.value}.")
        else:
            told(self, f"Saved the note on {title}." if note else f"Cleared the note on {title}.")
        self.load(keep=entry.posting_id)

    def action_open(self) -> None:
        import webbrowser

        from stage.domain import web_url

        row = self._selected()
        if row is None:
            told(self, "Nothing selected.", "warning")
            return
        url = web_url(row.entry.snapshot.apply_url)
        if url is None:
            told(
                self,
                "This posting's link is not a plain http or https address, so it was not opened.",
                "error",
            )
            return
        if not webbrowser.open(url):
            told(self, "No browser available on this machine.", "warning")
            return
        told(self, f"Opened {row.entry.snapshot.company}")

    def action_untrack(self) -> None:
        row = self._selected()
        if row is None:
            told(self, "Nothing selected.", "warning")
            return
        if self._arming != row.entry.posting_id:
            self._arming = row.entry.posting_id
            told(
                self,
                f"Press x again to stop tracking {row.entry.snapshot.title}; "
                "its status, note and history go with it.",
                "warning",
            )
            return
        self._arming = None
        self._untrack(row)

    @work(group="tracker")
    async def _untrack(self, row: TrackerRow) -> None:
        from stage.services.tracker import TrackerProblemError, remove_tracked

        repo = self.repository
        if repo is None:
            return
        try:
            await remove_tracked(repo, [row.entry.posting_id])
        except TrackerProblemError as exc:
            told(self, str(exc), "error")
            return
        self._changed = True
        told(self, f"Stopped tracking {row.entry.snapshot.title}.")
        self.load()

    def action_reload(self) -> None:
        self._arming = None
        self.load()

    def dismiss_overlays(self) -> None:
        self._close_picker()
        self._close_note()

    def action_back(self) -> None:
        if self.close_help() or self._close_picker() or self._close_note():
            return
        self.dismiss(self._changed)


__all__ = ["HELP_TEXT", "ApplicationsScreen"]
