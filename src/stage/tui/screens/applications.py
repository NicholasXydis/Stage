import sqlite3

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static

from stage.domain.tracker import ApplicationStatus, PostingState, TrackerError, parse_note
from stage.normalize.location import display_location
from stage.services.tracker import TrackerListing, TrackerRow
from stage.tui.base import BACK, HELP, Columns, HelpPanel, StageScreen, laid_out, picked, refill
from stage.tui.safe import cell, headline, inline, quoted, told

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
STATUS_WIDTH, ON_WIDTH, POSTING_WIDTH = 12, 10, 7
POSTING_STATE = {
    PostingState.OPEN: "still open",
    PostingState.CLOSED: "closed",
    PostingState.GONE: "no longer stored (this is the copy you kept)",
}


class ApplicationsScreen(StageScreen[bool]):
    HELP_TEXT = HELP_TEXT
    OVER_HELP = frozenset({"note", "filter"})
    BINDINGS = [
        Binding("n", "note", "note"),
        Binding("o", "open", "open"),
        Binding("f", "filter", "filter"),
        HELP,
        BACK,
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]loading...[/dim]", id="chips")
        yield OptionList(id="picker", classes="hidden")
        yield Horizontal(
            Input(placeholder="a short note, or nothing to clear it", id="note"),
            id="note-bar",
        )
        yield DataTable(id="results", cursor_type="row")
        yield Static("", id="detail")
        yield HelpPanel(id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self._table()
        refill(table, self._columns(0), ())
        table.focus()
        self.load()

    def relayout(self) -> None:
        if self._listing is None or laid_out(self._table()) == self._columns(len(self._rows)):
            return
        selected = self._selected()
        self._fill(self._listing, selected.entry.posting_id if selected is not None else None)

    def outcome(self) -> bool:
        return self._changed

    def _columns(self, rows: int) -> Columns:
        width = self.app.size.width
        number = max(4, len(str(rows)) + 1)
        fixed: Columns = (("Status", STATUS_WIDTH),)
        if width >= 70:
            fixed += (("On", ON_WIDTH),)
        tail: Columns = (("Posting", POSTING_WIDTH),) if width >= 80 else ()
        used = number + sum(size for _, size in (*fixed, *tail)) + 2 * (len(fixed) + len(tail) + 3)
        room = max(24, width - used - 1)
        company = min(28, max(10, room // 3))
        return (("#", number), *fixed, ("Company", company), ("Title", room - company), *tail)

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
        except sqlite3.Error as exc:
            self.database_failed(exc)
            return
        try:
            self._fill(listing, keep)
        except NoMatches:
            return

    def _show_problem(self, problem: str) -> None:
        try:
            self.say("[dim]your tracker could not be used[/dim]")
            self.query_one("#detail", Static).update(f"[red]{quoted(problem)}[/red]")
        except NoMatches:
            return
        told(self, problem, "error")

    def _cells(self, position: int, row: TrackerRow, columns: Columns) -> list[Text]:
        entry = row.entry
        values = {
            "#": str(position),
            "Status": entry.status.value,
            "On": entry.on.isoformat(),
            "Company": entry.snapshot.company,
            "Title": entry.snapshot.title,
            "Posting": row.state.value,
        }
        return [cell(values[label], width) for label, width in columns]

    def _fill(self, listing: TrackerListing, keep: str | None) -> None:
        self._listing = listing
        self._rows = listing.rows
        columns = self._columns(len(self._rows))
        wanted = next(
            (index for index, row in enumerate(self._rows) if row.entry.posting_id == keep), None
        )
        rows = (self._cells(at, row, columns) for at, row in enumerate(self._rows, start=1))
        refill(self._table(), columns, rows, wanted)
        self._render_chips()
        self._show_detail()

    def _render_chips(self) -> None:
        listing = self._listing
        if listing is None:
            return
        stats = listing.stats
        parts = [
            f"[b]{stats.tracked:,}[/b] tracked",
            f"{stats.applied_last_7:,} applied in the last 7 days",
            f"heard back from {stats.heard_back:,} of {stats.applications:,}",
        ]
        if listing.hidden:
            parts.append(f"{listing.hidden:,} hidden")
        if self._status is not None:
            parts.insert(0, f"[reverse] {self._status.value} [/reverse]")
        self.say("   ".join(parts))

    def _selected(self) -> TrackerRow | None:
        return picked(self._table(), self._rows)

    def _show_detail(self) -> None:
        panel = self.query_one("#detail", Static)
        row = self._selected()
        if row is None:
            panel.update(f"[dim]{self._empty_message()}[/dim]")
            return
        entry = row.entry
        snapshot = entry.snapshot
        width = max(20, self.app.size.width - 5)
        place = display_location(snapshot.location) or "-"
        steps = ", ".join(f"{c.status.value} {c.on.isoformat()}" for c in entry.history)
        steps = f"   [dim]{quoted(steps)}[/dim]" if len(entry.history) > 1 else ""
        note = f"note: {quoted(entry.note)}" if entry.note else "[dim]no note, n adds one[/dim]"
        panel.update(
            f"[b]{headline(snapshot.title, width)}[/b]\n"
            f"[$primary]{inline(snapshot.company, width // 2)}[/$primary]   "
            f"{inline(place, width // 2)}  [dim]|[/dim]  {POSTING_STATE[row.state]}\n\n"
            f"{entry.status.value} on {entry.on.isoformat()}{steps}\n"
            f"{note}   [dim]enter changes the status[/dim]"
        )

    def _empty_message(self) -> str:
        if self._status is not None:
            return (
                f"Nothing tracked as {self._status.value}. Press [b]f[/b] to show another status."
            )
        return (
            "Nothing tracked yet. On the postings screen, [b]s[/b] saves a posting, "
            "[b]p[/b] marks it applied and [b]h[/b] hides it."
        )

    def nothing_highlighted(self) -> None:
        if self._status is not None:
            said = f"Nothing tracked as {self._status.value}. Press f to show another status."
        else:
            said = "Nothing tracked yet. Press escape, then s on a posting to save it."
        told(self, said, "warning")

    def on_data_table_row_highlighted(self) -> None:
        self._arming = None
        self._show_detail()

    def on_data_table_row_selected(self) -> None:
        current = self._selected()
        if current is None:
            return
        labels = [status.value for status in STATUSES]
        self._open_picker("status", STATUSES, labels, current.entry.status)

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
        self._picking = mode
        self._choices = choices
        panel = self.query_one("#picker", OptionList)
        panel.clear_options()
        panel.add_options(
            cell(f"{'[x]' if choice == current else '[ ]'} {label}")
            for choice, label in zip(choices, labels, strict=True)
        )
        panel.remove_class("hidden")
        panel.highlighted = choices.index(current) if current in choices else 0
        panel.focus()

    def _close_picker(self) -> bool:
        panel = self.query_one("#picker", OptionList)
        if panel.has_class("hidden"):
            return False
        panel.add_class("hidden")
        self._picking = ""
        self._table().focus()
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
            self.nothing_highlighted()
            return
        self.dismiss_overlays()
        field = self.query_one("#note", Input)
        field.value = row.entry.note
        self.query_one("#note-bar").add_class("visible")
        field.focus()

    def _close_note(self) -> bool:
        bar = self.query_one("#note-bar")
        if not bar.has_class("visible"):
            return False
        bar.remove_class("visible")
        self._table().focus()
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        row = self._selected()
        try:
            note = parse_note(event.value)
        except TrackerError as exc:
            told(self, f"{exc} Shorten it and press enter, or escape to keep the old one.", "error")
            return
        self._close_note()
        if row is not None and note != row.entry.note:
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
        except sqlite3.Error as exc:
            self.database_failed(exc, loading=False)
            return
        if not report.changed:
            return
        self._changed = True
        entry = report.changed[0].entry
        title = entry.snapshot.title
        if status is not None:
            said = f"Marked {title} {status.value}."
        else:
            said = f"Saved the note on {title}." if note else f"Cleared the note on {title}."
        told(self, said, subject=entry.posting_id)
        self.load(keep=entry.posting_id)

    def action_open(self) -> None:
        from stage.tui.liveness import open_postings

        row = self._selected()
        if row is None:
            self.nothing_highlighted()
            return
        snapshot = row.entry.snapshot
        open_postings(self, [(row.entry.posting_id, snapshot.apply_url, snapshot.company)])

    def action_untrack(self) -> None:
        row = self._selected()
        if row is None:
            self.nothing_highlighted()
            return
        if self._arming != row.entry.posting_id:
            self._arming = row.entry.posting_id
            told(
                self,
                f"Press x again to stop tracking {row.entry.snapshot.title}. "
                "Its status, note and history go with it.",
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
        except sqlite3.Error as exc:
            self.database_failed(exc, loading=False)
            return
        self._changed = True
        told(self, f"Stopped tracking {row.entry.snapshot.title}.", subject=row.entry.posting_id)
        self.load()

    def action_reload(self) -> None:
        self._arming = None
        self.load()

    def close_overlay(self) -> bool:
        picker = self._close_picker()
        return self._close_note() or picker


__all__ = ["HELP_TEXT", "ApplicationsScreen"]
