from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from stage.domain.text import sanitize
from stage.tui.help import HelpOverlay
from stage.tui.safe import cell, quoted, told

if TYPE_CHECKING:
    from stage.storage import AsyncRepository

COLUMNS = ("State", "Company", "Platform", "Postings", "Last success")


HELP_TEXT = """[b]Board health[/b]
  up down  move between employers
  e        enable this employer
  d        disable it, twice to confirm
  r        reload from the database

[dim]? closes this   escape goes back[/dim]"""


class BoardsScreen(HelpOverlay, Screen[None]):
    HELP_TEXT = HELP_TEXT
    BINDINGS = [
        Binding("e", "enable", "enable"),
        Binding("d", "disable", "disable"),
        Binding("question_mark", "help", "keys"),
        Binding("escape", "back", "back"),
        Binding("r", "reload", "reload", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._companies: tuple[str, ...] = ()
        self._arming_disable: str | None = None

    @property
    def repository(self) -> "AsyncRepository | None":
        from stage.tui.app import StageApp

        app = self.app
        return app.repository if isinstance(app, StageApp) else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        yield DataTable(id="results", cursor_type="row")
        yield Static("", id="detail")
        yield Static("", id="help")
        yield Footer()

    def on_mount(self) -> None:
        self._mark_loading()
        self.query_one("#results", DataTable).add_columns(*COLUMNS)
        self.load()

    def _mark_loading(self) -> None:
        self.query_one("#chips", Static).update("[dim]loading…[/dim]")

    def on_data_table_row_highlighted(self) -> None:
        self._arming_disable = None

    def selected_company(self) -> str | None:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if not self._companies or row < 0 or row >= len(self._companies):
            return None
        return self._companies[row]

    @work(exclusive=True)
    async def load(self) -> None:
        from stage.companies import RegistryError, load_companies
        from stage.services.coverage import coverage

        repo = self.repository
        if repo is None:
            return
        try:
            companies = load_companies(None)
        except RegistryError as exc:
            self.query_one("#detail", Static).update(f"[red]{quoted(str(exc))}[/red]")
            return
        report = await coverage(repo, companies)
        table = self.query_one("#results", DataTable)
        table.clear()
        names = []
        for row in report.rows:
            seen = (
                row.last_success_at.astimezone().strftime("%Y-%m-%d")
                if row.last_success_at
                else "—"
            )
            table.add_row(
                cell(row.state.value),
                cell(row.company, 26),
                cell(row.platform),
                cell(str(row.postings)),
                cell(seen),
            )
            names.append(row.company)
        listed = set(names)
        for company in companies:
            if company.enabled or company.name in listed:
                continue
            table.add_row(
                cell("disabled"),
                cell(company.name, 26),
                cell(company.platform.value),
                cell("0"),
                cell("-"),
            )
            names.append(company.name)
        self._companies = tuple(names)
        self.query_one("#chips", Static).update(
            f"[b]{report.enabled}[/b] enabled · [dim]{report.disabled} disabled · "
            f"{len(report.gaps)} producing nothing[/dim]"
        )
        self.query_one("#detail", Static).update(
            "[b]e[/b] enable  [b]d[/b] disable, twice to confirm  [b]?[/b] keys\n"
            "[dim]A board producing nothing is either genuinely empty or a parser has "
            "drifted. Check with stage canary before disabling.[/dim]"
        )

    def action_disable(self) -> None:
        company = self.selected_company()
        if company is None:
            told(self, "Highlight a board first.", "warning")
            return
        if self._arming_disable != company:
            self._arming_disable = company
            told(self, f"Press d again to disable {quoted(company)}.", "warning")
            return
        self._arming_disable = None
        self._set_enabled(False)

    def action_enable(self) -> None:
        self._arming_disable = None
        self._set_enabled(True)

    def _set_enabled(self, enabled: bool) -> None:
        from dataclasses import replace

        from stage.companies import RegistryError, update_registry
        from stage.domain import Company

        company = self.selected_company()
        if company is None:
            told(self, "Highlight a board first.", "warning")
            return

        def apply(rows: tuple[Company, ...]) -> tuple[list[Company], int]:
            changed = 0
            updated = []
            for row in rows:
                if row.name == company and row.enabled != enabled:
                    updated.append(replace(row, enabled=enabled))
                    changed += 1
                    continue
                updated.append(row)
            return updated, changed

        try:
            _, changed = update_registry(apply)
        except (RegistryError, OSError) as exc:
            told(self, f"Could not write the registry: {sanitize(str(exc))}", "error")
            return
        if not changed:
            state = "enabled" if enabled else "disabled"
            told(self, f"{sanitize(company)} is already {state}.", "warning")
            return
        told(self, f"{'Enabled' if enabled else 'Disabled'} {sanitize(company)}")
        self.load()

    def action_reload(self) -> None:
        self.load()

    def action_back(self) -> None:
        if self.close_help():
            return
        self.dismiss(None)
