import asyncio
import sqlite3
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from stage.domain.text import sanitize
from stage.tui.base import BACK, HELP, Columns, HelpPanel, StageScreen, laid_out, picked, refill
from stage.tui.safe import cell, headline, inline, quoted, sentence, told

if TYPE_CHECKING:
    from stage.domain import QuarantinedJob, UnregisteredCompany
    from stage.services.coverage import CoverageReport

PAGE = 200
COUNTS = ("Postings", "Rejected", "Internships")
COUNT_WIDTHS = tuple(len(label) for label in COUNTS)

DISPOSITIONS: tuple[tuple[str, str, str], ...] = (
    ("a", "adapter-candidate", "worth a dedicated adapter"),
    ("j", "custom-json-candidate", "reachable with a custom board"),
    ("k", "feed-only", "covered well enough through a feed"),
    ("u", "unavailable", "no public board to poll"),
    ("d", "deferred", "revisit later"),
)


HELP_TEXT = """[b]Review[/b]
  up down  move between rows
  tab      then left right switches tabs

[b]File an employer[/b]
  a        worth a dedicated adapter
  j        reachable with custom json
  k        covered through a feed
  u        no public board to poll
  d        revisit later
  x        undo the last call

[b]Other[/b]
  m        load more
  r        reload from the database

[dim]? closes this   escape goes back[/dim]"""


class ReviewScreen(StageScreen[None]):
    HELP_TEXT = HELP_TEXT
    NOTHING = "No unregistered employer is listed. Press r to reload."
    BINDINGS = [
        Binding("a", "classify('adapter-candidate')", "adapter"),
        Binding("j", "classify('custom-json-candidate')", "json"),
        Binding("k", "classify('feed-only')", "feed"),
        Binding("x", "unclassify", "undo"),
        HELP,
        BACK,
        Binding("u", "classify('unavailable')", "unavailable", show=False),
        Binding("d", "classify('deferred')", "defer", show=False),
        Binding("r", "reload", "reload", show=False),
        Binding("m", "more", "load more", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._limit = PAGE
        self._report: CoverageReport | None = None
        self._unregistered: tuple[UnregisteredCompany, ...] = ()
        self._quarantined: tuple[QuarantinedJob, ...] = ()
        self._total = 0
        self._filed: str | None = None
        self._problem = ""

    @property
    def _employers(self) -> tuple[str, ...]:
        return tuple(entry.company for entry in self._unregistered)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]loading...[/dim]", id="chips")
        with TabbedContent(id="tabs"):
            with TabPane("Unregistered", id="tab-unregistered"):
                yield DataTable(id="unregistered", cursor_type="row")
            with TabPane("Rejected", id="tab-quarantine"):
                yield DataTable(id="quarantine", cursor_type="row")
        yield Static("", id="detail")
        yield HelpPanel(id="help")
        yield Footer()

    def on_mount(self) -> None:
        self._fill_unregistered(None)
        self._fill_quarantine()
        self.query_one("#unregistered", DataTable).focus()
        self.load()

    def relayout(self) -> None:
        if laid_out(self.query_one("#unregistered", DataTable)) != self._columns()[0]:
            self._fill_unregistered(self.selected_employer())
            self._fill_quarantine()

    def _columns(self) -> tuple[Columns, Columns]:
        width = self.app.size.width
        room = max(20, width - sum(COUNT_WIDTHS) - 11)
        employer = min(34, max(10, room * 3 // 5))
        counts = tuple(zip(COUNTS, COUNT_WIDTHS, strict=True))
        space = max(36, width - 9)
        reason = min(21, space // 3)
        source = min(14, max(10, space // 8))
        company = min(26, (space - reason - source) // 3)
        return (
            (("Employer", employer), *counts, ("Sources", min(40, room - employer))),
            (
                ("Reason", reason),
                ("Company", company),
                ("Title", space - reason - source - company),
                ("Source", source),
            ),
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if not super().check_action(action, parameters):
            return False
        if action != "classify":
            return True
        try:
            return self.query_one("#tabs", TabbedContent).active == "tab-unregistered"
        except NoMatches:
            return False

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()
        if event.pane is not None:
            event.pane.query_one(DataTable).focus()
        self._render_detail()

    def on_data_table_row_highlighted(self) -> None:
        self._render_detail()

    def selected_employer(self) -> str | None:
        return picked(self.query_one("#unregistered", DataTable), self._employers)

    @work(exclusive=True)
    async def load(self, keep: str | None = None, *, fresh: bool = True) -> None:
        from stage.companies import RegistryError, load_boards
        from stage.domain import QuarantineFilters
        from stage.services.coverage import coverage
        from stage.services.quarantine import list_quarantined

        repo = self.repository
        if repo is None:
            return
        if fresh or self._report is None:
            try:
                companies, _ = await asyncio.to_thread(load_boards, None)
            except RegistryError as exc:
                self._problem = sentence(str(exc))
                self.say("[dim]the registry could not be read[/dim]")
                self._render_detail()
                return
            self._problem = ""
            try:
                self._report = await coverage(repo, companies, unregistered=True)
            except sqlite3.Error as exc:
                self.database_failed(exc)
                return
        self._unregistered = tuple(self._report.unregistered[: self._limit])
        try:
            self._fill_unregistered(keep)
            self._render_chips(loading=True)
        except NoMatches:
            return
        try:
            listing = await list_quarantined(repo, QuarantineFilters(limit=self._limit))
        except sqlite3.Error as exc:
            self.database_failed(exc)
            return
        self._quarantined = tuple(listing.entries)
        self._total = listing.total_matching
        try:
            self._fill_quarantine()
            self._render_chips()
        except NoMatches:
            return

    def _render_chips(self, *, loading: bool = False) -> None:
        report = self._report
        if report is None:
            return
        reviewed = f"{len(report.classifications):,} reviewed"
        if loading:
            rejected = "[dim]loading rejected postings...[/dim]"
        else:
            rejected = (
                f"[b]{self._total:,}[/b] rejected "
                f"[dim]({len(self._quarantined):,} shown, {reviewed})[/dim]"
            )
        self.say(f"[b]{len(report.unregistered):,}[/b] unregistered, {rejected}")

    def _fill_unregistered(self, keep: str | None) -> None:
        table = self.query_one("#unregistered", DataTable)
        columns = self._columns()[0]
        (_, employer), *_, (_, sources) = columns
        employers = self._employers
        cursor = employers.index(keep) if keep in employers else table.cursor_row
        rows = (
            (
                cell(entry.company, employer),
                cell(f"{entry.postings:,}"),
                cell(f"{entry.quarantined:,}"),
                cell("yes" if entry.posts_internships else "-"),
                cell(", ".join(entry.sources) or "-", sources),
            )
            for entry in self._unregistered
        )
        refill(table, columns, rows, cursor)
        self._render_detail()

    def _fill_quarantine(self) -> None:
        table = self.query_one("#quarantine", DataTable)
        columns = self._columns()[1]
        (_, reason), (_, company), (_, title), (_, source) = columns
        rows = (
            (
                cell(row.reason.value, reason),
                cell(row.company, company),
                cell(row.title_raw, title),
                cell(row.source, source),
            )
            for row in self._quarantined
        )
        refill(table, columns, rows, table.cursor_row)
        self._render_detail()

    def _render_detail(self) -> None:
        try:
            active = self.query_one("#tabs", TabbedContent).active
            detail = self.query_one("#detail", Static)
            table = self.query_one("#quarantine", DataTable)
        except NoMatches:
            return
        if self._problem:
            detail.update(f"[red]{quoted(self._problem)}[/red]")
            return
        if active != "tab-quarantine":
            keys = [f"[b]{key}[/b] {label}" for key, label, _ in DISPOSITIONS]
            detail.update(
                f"{'  '.join(keys[:3])}\n{'  '.join(keys[3:])}  [b]x[/b] undo\n"
                "[dim]Acts on the highlighted employer. m loads more rows.[/dim]"
            )
            return
        row = picked(table, self._quarantined)
        if row is None:
            detail.update("[dim]No rejected postings are listed. Press r to reload.[/dim]")
            return
        width = max(20, self.app.size.width - 5)
        facts = "  [dim]|[/dim]  ".join(
            inline(part, width // 2)
            for part in (row.reason.value, row.location_raw, row.source)
            if part
        )
        detail.update(
            f"[b]{headline(row.title_raw, width)}[/b]\n"
            f"[$primary]{inline(row.company, width // 2)}[/$primary]   {facts}\n"
            "[dim]Stage rejected this posting when it screened it. Filing keys work on the "
            "Unregistered tab, and m loads more rows.[/dim]"
        )

    @work(exclusive=True, group="filing")
    async def action_classify(self, disposition: str) -> None:
        from datetime import UTC, datetime

        from stage.domain import CoverageClassification, CoverageDisposition

        repo = self.repository
        company = self.selected_employer()
        if repo is None or company is None:
            self.nothing_highlighted()
            return
        note = next(
            (why for _, label, why in DISPOSITIONS if label == disposition),
            "reviewed from the interactive browser",
        )
        entry = CoverageClassification(
            company=company,
            disposition=CoverageDisposition(disposition),
            note=note,
            checked_on=datetime.now(UTC),
        )
        try:
            replaced = await repo.record_coverage_classification(entry)
        except sqlite3.Error as exc:
            said = self.database_trouble(exc)
            told(self, f"Could not record {sanitize(company)}. {said}", "error")
            return
        self._filed = company
        verb = "Updated" if replaced else "Recorded"
        told(self, f"{verb} {sanitize(company)} as {disposition}. x undoes it.")
        self.load()

    @work(exclusive=True, group="filing")
    async def action_unclassify(self) -> None:
        repo = self.repository
        company = self._filed
        if repo is None or company is None:
            told(self, "Nothing to undo. File an employer first with a, j, k, u or d.", "warning")
            return
        try:
            removed = await repo.clear_coverage_classification(company)
        except sqlite3.Error as exc:
            said = self.database_trouble(exc)
            told(self, f"Could not clear {sanitize(company)}. {said}", "error")
            return
        self._filed = None
        if not removed:
            told(self, f"{sanitize(company)} had no recorded decision.", "warning")
            return
        told(self, f"Cleared the decision for {sanitize(company)}")
        self.load(keep=company)

    def action_more(self) -> None:
        self._limit += PAGE
        self.load(self.selected_employer(), fresh=False)

    def action_reload(self) -> None:
        self._limit = PAGE
        self.load()
