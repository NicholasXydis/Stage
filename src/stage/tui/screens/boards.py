import asyncio
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Static

from stage.domain import Platform
from stage.domain.coverage import CoverageState
from stage.domain.text import plain_error, sanitize
from stage.tui.base import BACK, HELP, Columns, HelpPanel, StageScreen, laid_out, picked, refill
from stage.tui.safe import cell, counted, inline, quoted, sentence, told

if TYPE_CHECKING:
    from stage.domain import Company

STATE_WIDTH = max(len(state.value) for state in CoverageState)
PLATFORM_WIDTH = max(len(platform.value) for platform in Platform)
POSTINGS_WIDTH, SEEN_WIDTH = len("Postings"), len("Last success")
COMPANY_MOST = 40

STATE_NOTES = {
    CoverageState.EMPTY.value: (
        "A board producing nothing is either empty or a parser has drifted. "
        "Check it with stage canary before you disable it."
    ),
    CoverageState.NEVER_REACHED.value: "No sync has reached this board yet.",
    "disabled": "Switched off. Press e to switch it back on.",
}

HELP_TEXT = """[b]Board health[/b]
  up down  move between employers
  e        enable this employer
  d        disable it, twice to confirm
  r        reload from the database

[dim]? closes this   escape goes back[/dim]"""


@dataclass(frozen=True, slots=True)
class BoardRow:
    state: str
    company: str
    platform: str
    board: str
    postings: int = 0
    seen: str = "-"
    error: str = ""
    failures: int = 0


def switch_board(company: str, board: str | None, enabled: bool) -> tuple[str, str, bool]:
    from dataclasses import replace
    from datetime import UTC, datetime

    from stage.companies import RegistryError, board_identity, load_companies, update_personal
    from stage.services.coverage import board_of

    def here(row: "Company") -> bool:
        return row.name == company and (board is None or board_of(row) == board)

    name = sanitize(company)
    try:
        shipped = [row for row in load_companies() if here(row)]
    except RegistryError as exc:
        return f"Could not read the registry. {sentence(str(exc))}", "error", False
    if enabled and any(not row.enabled for row in shipped):
        return (
            f"Stage switched {name} off on purpose, so it stays off. "
            "Its notes in the registry say why.",
            "warning",
            False,
        )
    targets = {board_identity(row) for row in shipped}

    def apply(rows: "tuple[Company, ...]") -> "tuple[list[Company], int]":
        if not targets:
            updated = [
                replace(row, enabled=enabled) if here(row) and row.enabled != enabled else row
                for row in rows
            ]
            return updated, sum(new is not old for new, old in zip(updated, rows, strict=True))
        held = [row for row in rows if board_identity(row) in targets and not row.enabled]
        kept = [row for row in rows if board_identity(row) not in targets or row.enabled]
        if enabled:
            return kept, len(held)
        if held:
            return list(rows), 0
        note = f"{datetime.now(UTC).date()}: switched off by you in stage tui"
        return [*kept, *(replace(row, enabled=False, notes=note) for row in shipped)], len(shipped)

    try:
        _, changed = update_personal(apply)
    except (RegistryError, OSError) as exc:
        return f"Could not write your board list. {sentence(str(exc))}", "error", False
    if not changed:
        return f"{name} is already {'enabled' if enabled else 'disabled'}.", "warning", False
    if enabled:
        return f"Enabled {name}", "information", True
    return f"Disabled {name}. Press e to switch it back on.", "information", True


class BoardsScreen(StageScreen[None]):
    HELP_TEXT = HELP_TEXT
    NOTHING = "No board is listed. Press r to reload."
    BINDINGS = [
        Binding("e", "enable", "enable"),
        Binding("d", "disable", "disable"),
        HELP,
        BACK,
        Binding("r", "reload", "reload", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: tuple[BoardRow, ...] = ()
        self._arming_disable: str | None = None

    @property
    def _companies(self) -> tuple[str, ...]:
        return tuple(row.company for row in self._rows)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]loading...[/dim]", id="chips")
        yield DataTable(id="results", cursor_type="row")
        yield Static("", id="detail")
        yield HelpPanel(id="help")
        yield Footer()

    def on_mount(self) -> None:
        refill(self._table(), self._columns(), ())
        self.load()

    def relayout(self) -> None:
        if laid_out(self._table()) != self._columns():
            self._fill(self.selected_board())

    def _columns(self) -> Columns:
        width = self.app.size.width
        platform: Columns = (("Platform", PLATFORM_WIDTH),) if width >= 80 else ()
        fixed = STATE_WIDTH + POSTINGS_WIDTH + SEEN_WIDTH + sum(size for _, size in platform)
        company = min(COMPANY_MOST, max(10, width - fixed - 2 * (4 + len(platform)) - 1))
        return (
            ("State", STATE_WIDTH),
            ("Company", company),
            *platform,
            ("Postings", POSTINGS_WIDTH),
            ("Last success", SEEN_WIDTH),
        )

    def on_data_table_row_highlighted(self) -> None:
        self._arming_disable = None
        self._show_detail()

    def _selected(self) -> BoardRow | None:
        return picked(self._table(), self._rows)

    def selected_company(self) -> str | None:
        row = self._selected()
        return row.company if row is not None else None

    def selected_board(self) -> str | None:
        row = self._selected()
        return row.board if row is not None else None

    @work(exclusive=True)
    async def load(self) -> None:
        from stage.companies import RegistryError, load_boards
        from stage.services.coverage import board_of, coverage

        repo = self.repository
        if repo is None:
            return
        try:
            companies, personal = await asyncio.to_thread(load_boards, None)
        except RegistryError as exc:
            self.say("[dim]the registry could not be read[/dim]")
            self.query_one("#detail", Static).update(f"[red]{quoted(sentence(str(exc)))}[/red]")
            return
        if personal.problem:
            told(self, f"Your own boards were left out. {sentence(personal.problem)}", "warning")
        try:
            report = await coverage(repo, companies)
        except sqlite3.Error as exc:
            self.database_failed(exc)
            return
        rows = [
            BoardRow(
                state=entry.state.value,
                company=entry.company,
                platform=entry.platform,
                board=entry.board,
                postings=entry.postings,
                seen=(
                    entry.last_success_at.astimezone().strftime("%Y-%m-%d")
                    if entry.last_success_at
                    else "-"
                ),
                error=entry.last_error,
                failures=entry.consecutive_failures,
            )
            for entry in report.rows
        ]
        listed = {row.board for row in rows}
        rows.extend(
            BoardRow("disabled", company.name, company.platform.value, board_of(company))
            for company in companies
            if not company.enabled and board_of(company) not in listed
        )
        try:
            keep = self.selected_board()
            self._rows = tuple(rows)
            self._fill(keep)
            self.say(
                f"[b]{report.enabled:,}[/b] enabled, [dim]{report.disabled:,} disabled, "
                f"{len(report.gaps):,} producing nothing[/dim]"
            )
        except NoMatches:
            return

    def _fill(self, keep: str | None) -> None:
        table = self._table()
        boards = [row.board for row in self._rows]
        cursor = boards.index(keep) if keep in boards else table.cursor_row
        columns = self._columns()
        values = (
            {
                "State": row.state,
                "Company": row.company,
                "Platform": row.platform,
                "Postings": f"{row.postings:,}",
                "Last success": row.seen,
            }
            for row in self._rows
        )
        refill(
            table, columns, ([cell(v[label], w) for label, w in columns] for v in values), cursor
        )
        self._show_detail()

    def _show_detail(self) -> None:
        try:
            panel = self.query_one("#detail", Static)
        except NoMatches:
            return
        row = self._selected()
        keys = "[b]e[/b] enable  [b]d[/b] disable, twice to confirm  [b]?[/b] keys"
        if row is None:
            panel.update(f"{keys}\n[dim]{self.NOTHING}[/dim]")
            return
        width = max(20, self.app.size.width - 5)
        facts = "  [dim]|[/dim]  ".join(
            inline(part, width // 2)
            for part in (row.platform, row.state, counted(row.postings, "posting"))
        )
        if row.error:
            failed = f" ({counted(row.failures, 'failure')} in a row)" if row.failures else ""
            why = f"[red]{inline(f'Last error{failed}: {plain_error(row.error)}', 2 * width)}[/red]"
        else:
            why = f"[dim]{STATE_NOTES.get(row.state, 'No error recorded.')}[/dim]"
        panel.update(
            f"[$primary]{inline(row.company, width // 2)}[/$primary]   {facts}\n{why}\n{keys}"
        )

    def action_disable(self) -> None:
        company = self.selected_company()
        if company is None:
            self.nothing_highlighted()
            return
        if self._arming_disable != company:
            self._arming_disable = company
            told(self, f"Press d again to disable {sanitize(company)}.", "warning")
            return
        self._arming_disable = None
        self._set_enabled(False)

    def action_enable(self) -> None:
        self._arming_disable = None
        self._set_enabled(True)

    def _set_enabled(self, enabled: bool) -> None:
        company = self.selected_company()
        if company is None:
            self.nothing_highlighted()
            return
        self._switch(company, self.selected_board(), enabled)

    @work(group="boards")
    async def _switch(self, company: str, board: str | None, enabled: bool) -> None:
        said, severity, changed = await asyncio.to_thread(switch_board, company, board, enabled)
        told(self, said, severity, subject=(board, enabled))
        if changed:
            self.load()

    def action_reload(self) -> None:
        self.load()
