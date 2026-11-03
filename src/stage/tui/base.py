import sqlite3
from collections.abc import Iterable, Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Static

from stage.storage import AsyncRepository
from stage.tui.context import database_of, repository_of
from stage.tui.safe import quoted, told

SHORT = 30
COMPACT_BELOW = 80
ALWAYS = frozenset({"help", "back"})
HELP = Binding("question_mark", "help", "keys")
BACK = Binding("escape", "back", "back")
Columns = tuple[tuple[str, int], ...]


class HelpPanel(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Static("", id="help-text")


def laid_out(table: DataTable[object]) -> Columns:
    return tuple((str(column.label), column.width) for column in table.columns.values())


def refill(
    table: DataTable[object],
    columns: Columns,
    rows: Iterable[Sequence[object]],
    cursor: int | None = None,
) -> None:
    if laid_out(table) == columns:
        table.clear()
    else:
        table.clear(columns=True)
        for label, width in columns:
            table.add_column(label, width=width)
    for row in rows:
        table.add_row(*row)
    if cursor is not None and table.row_count:
        table.move_cursor(row=min(max(cursor, 0), table.row_count - 1))


def picked[T](table: DataTable[object], items: Sequence[T]) -> T | None:
    row = table.cursor_row
    return items[row] if 0 <= row < len(items) else None


class StageScreen[T](Screen[T]):
    HELP_TEXT: ClassVar[str] = ""
    OVER_HELP: ClassVar[frozenset[str]] = frozenset()
    NOTHING: ClassVar[str] = "Nothing is highlighted."
    _before_help: Widget | None = None
    _help_open = False

    @property
    def repository(self) -> AsyncRepository | None:
        return repository_of(self.app)

    def on_mount(self) -> None:
        self.fit()

    def on_resize(self) -> None:
        self.fit()
        self.relayout()

    def relayout(self) -> None:
        return

    def fit(self) -> None:
        size = self.app.size
        self.set_class(size.height < SHORT, "narrow")
        for footer in self.query(Footer):
            footer.compact = size.width < COMPACT_BELOW

    def say(self, text: str) -> None:
        try:
            self.query_one("#chips", Static).update(text)
        except NoMatches:
            return

    def nothing_highlighted(self) -> None:
        told(self, self.NOTHING, "warning")

    def database_trouble(self, exc: sqlite3.Error) -> str:
        from stage.paths import database_path
        from stage.storage.problems import database_problem

        return database_problem(exc, database_of(self.app) or database_path())

    def database_failed(self, exc: sqlite3.Error, *, loading: bool = True) -> None:
        said = self.database_trouble(exc)
        if loading:
            self.say(f"[red]{quoted(said)}[/red]")
        told(self, said, "error")

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        return not self._help_open or action in ALWAYS or action in self.OVER_HELP

    def _table(self) -> DataTable[object]:
        return self.query_one("#results", DataTable)

    def dismiss_overlays(self) -> None:
        self.close_help()
        self.close_overlay()

    def close_overlay(self) -> bool:
        return False

    def outcome(self) -> T | None:
        return None

    def action_back(self) -> None:
        if not (self.close_help() or self.close_overlay()):
            self.dismiss(self.outcome())

    def action_help(self) -> None:
        if self.close_help():
            return
        self.dismiss_overlays()
        panel = self.query_one("#help", HelpPanel)
        panel.query_one("#help-text", Static).update(self.HELP_TEXT)
        panel.add_class("visible")
        panel.scroll_home(animate=False)
        self._before_help = self.focused
        self._help_open = True
        self.refresh_bindings()
        panel.focus()

    def close_help(self) -> bool:
        panel = self.query_one("#help", HelpPanel)
        if not panel.has_class("visible"):
            return False
        panel.remove_class("visible")
        self._help_open = False
        self.refresh_bindings()
        before, self._before_help = self._before_help, None
        tables = self.query(DataTable)
        if before is not None and _shown(before):
            before.focus()
        elif tables:
            tables.first().focus()
        else:
            panel.blur()
        return True


def _shown(widget: Widget) -> bool:
    return widget.is_attached and all(
        node.display for node in widget.ancestors_with_self if isinstance(node, Widget)
    )


__all__ = [
    "ALWAYS",
    "BACK",
    "HELP",
    "SHORT",
    "Columns",
    "HelpPanel",
    "StageScreen",
    "laid_out",
    "picked",
    "refill",
]
