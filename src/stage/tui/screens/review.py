from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from stage.domain.text import sanitize
from stage.tui.help import HelpOverlay
from stage.tui.safe import cell, quoted, told

if TYPE_CHECKING:
    from stage.storage import AsyncRepository

PAGE = 200
UNREGISTERED_COLUMNS = ("Employer", "Postings", "Quarantined", "Internships", "Sources")
QUARANTINE_COLUMNS = ("Reason", "Company", "Title", "Source")

DISPOSITIONS: tuple[tuple[str, str, str], ...] = (
    ("a", "adapter-candidate", "worth a dedicated adapter"),
    ("j", "custom-json-candidate", "reachable with a custom board"),
    ("k", "feed-only", "covered well enough through a feed"),
    ("u", "unavailable", "no public board to poll"),
    ("d", "deferred", "revisit later"),
)


HELP_TEXT = """[b]Review[/b]
  up down  move between rows
  tab      switch tabs

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


class ReviewScreen(HelpOverlay, Screen[None]):
    HELP_TEXT = HELP_TEXT
    BINDINGS = [
        Binding("a", "classify('adapter-candidate')", "adapter"),
        Binding("j", "classify('custom-json-candidate')", "custom json"),
        Binding("k", "classify('feed-only')", "feed only"),
        Binding("x", "unclassify", "undo"),
        Binding("question_mark", "help", "keys"),
        Binding("escape", "back", "back"),
        Binding("u", "classify('unavailable')", "unavailable", show=False),
        Binding("d", "classify('deferred')", "defer", show=False),
        Binding("r", "reload", "reload", show=False),
        Binding("m", "more", "load more", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._limit = PAGE
        self._employers: tuple[str, ...] = ()

    @property
    def repository(self) -> "AsyncRepository | None":
        from stage.tui.app import StageApp

        app = self.app
        return app.repository if isinstance(app, StageApp) else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        with TabbedContent(id="tabs"):
            with TabPane("Unregistered", id="tab-unregistered"):
                yield DataTable(id="unregistered", cursor_type="row")
            with TabPane("Quarantine", id="tab-quarantine"):
                yield DataTable(id="quarantine", cursor_type="row")
        yield Static("", id="detail")
        yield Static("", id="help")
        yield Footer()

    def on_mount(self) -> None:
        self._mark_loading()
        self.query_one("#unregistered", DataTable).add_columns(*UNREGISTERED_COLUMNS)
        self.query_one("#quarantine", DataTable).add_columns(*QUARANTINE_COLUMNS)
        self.load()

    def _mark_loading(self) -> None:
        self.query_one("#chips", Static).update("[dim]loading…[/dim]")

    def selected_employer(self) -> str | None:
        table = self.query_one("#unregistered", DataTable)
        row = table.cursor_row
        if not self._employers or row < 0 or row >= len(self._employers):
            return None
        return self._employers[row]

    @work(exclusive=True)
    async def load(self) -> None:
        from stage.companies import RegistryError, load_companies
        from stage.domain import QuarantineFilters
        from stage.services.coverage import coverage
        from stage.services.quarantine import list_quarantined

        repo = self.repository
        if repo is None:
            return
        try:
            companies = load_companies(None)
        except RegistryError as exc:
            self.query_one("#detail", Static).update(f"[red]{quoted(str(exc))}[/red]")
            return
        report = await coverage(repo, companies, unregistered=True)
        listing = await list_quarantined(repo, QuarantineFilters(limit=self._limit))

        rows = report.unregistered[: self._limit]
        self._employers = tuple(entry.company for entry in rows)
        unregistered = self.query_one("#unregistered", DataTable)
        unregistered.clear()
        for entry in rows:
            unregistered.add_row(
                cell(entry.company, 34),
                cell(str(entry.postings)),
                cell(str(entry.quarantined)),
                cell("yes" if entry.posts_internships else "-"),
                cell(", ".join(entry.sources) or "-", 24),
            )

        quarantine = self.query_one("#quarantine", DataTable)
        quarantine.clear()
        for row in listing.entries:
            quarantine.add_row(
                cell(row.reason.value, 22),
                cell(row.company, 22),
                cell(row.title_raw, 38),
                cell(row.source, 14),
            )

        self.query_one("#chips", Static).update(
            f"[b]{len(report.unregistered)}[/b] unregistered · "
            f"[b]{listing.total_matching}[/b] quarantined "
            f"[dim](showing {len(listing.entries)}, {len(report.classifications)} reviewed)[/dim]"
        )
        self._render_help()

    def _render_help(self) -> None:
        keys = "  ".join(f"[b]{key}[/b] {label}" for key, label, _ in DISPOSITIONS)
        self.query_one("#detail", Static).update(
            f"{keys}  [b]x[/b] undo\n[dim]Acts on the highlighted employer. "
            "m loads more quarantined rows.[/dim]"
        )

    @work(exclusive=True)
    async def action_classify(self, disposition: str) -> None:
        from datetime import UTC, datetime

        from stage.domain import CoverageClassification, CoverageDisposition

        repo = self.repository
        company = self.selected_employer()
        if repo is None or company is None:
            told(self, "Highlight an unregistered employer first.", "warning")
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
        replaced = await repo.record_coverage_classification(entry)
        verb = "Updated" if replaced else "Recorded"
        told(self, f"{verb} {sanitize(company)} as {disposition}")
        self.load()

    @work(exclusive=True)
    async def action_unclassify(self) -> None:
        repo = self.repository
        company = self.selected_employer()
        if repo is None or company is None:
            told(self, "Highlight an employer first.", "warning")
            return
        removed = await repo.clear_coverage_classification(company)
        if not removed:
            told(self, f"{sanitize(company)} had no recorded decision.", "warning")
            return
        told(self, f"Cleared the decision for {sanitize(company)}")
        self.load()

    def action_more(self) -> None:
        self._limit += PAGE
        self.load()

    def action_reload(self) -> None:
        self._limit = PAGE
        self.load()

    def action_back(self) -> None:
        if self.close_help():
            return
        self.dismiss(None)
