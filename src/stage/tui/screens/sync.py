import asyncio
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

from stage.domain.text import plain_error
from stage.tui.base import BACK, HELP, HelpPanel, StageScreen
from stage.tui.context import database_of
from stage.tui.safe import cell, counted, inline, quoted, sentence, told

if TYPE_CHECKING:
    from stage.domain import SyncEvent, SyncFinished

MAX_WARNINGS = 40
SHOWN_WARNINGS = 8
BAR_WIDTH = 22


def source_bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    if total <= 0:
        return "-" * width
    filled = round(width * min(done, total) / total)
    return "#" * filled + "-" * (width - filled)


def overall_bar(done: int, total: int, width: int = BAR_WIDTH + 10) -> str:
    share = round(100 * min(done, total) / total) if total > 0 else 0
    return f"{source_bar(done, total, width)} {share:>3}%"


class SyncScreen(StageScreen[None]):
    HELP_TEXT = """[b]Sync[/b]
  s        start a sync
  x        cancel the run in progress

[dim]? closes this   escape goes back[/dim]"""
    BINDINGS = [
        Binding("s", "start", "start"),
        Binding("x", "cancel", "cancel"),
        HELP,
        BACK,
    ]

    def __init__(self, *, dry_run: bool = False) -> None:
        super().__init__()
        self.dry_run = dry_run
        self._sources: dict[str, tuple[int, int]] = {}
        self._warnings: list[str] = []
        self._done = 0
        self._fetched = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]Press s to start a sync, x to cancel, esc to go back.[/dim]", id="chips")
        yield Static(overall_bar(0, 0), id="overall")
        yield DataTable(id="sources", cursor_type="row")
        yield Static("", id="warnings")
        yield Static(
            "[dim]A sync fetches every enabled board, classifies what it finds, and "
            "stores the postings that qualify.[/dim]",
            id="detail",
        )
        yield HelpPanel(id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sources", DataTable)
        table.add_columns("Source", "Progress", "Done")
        table.focus()

    def relayout(self) -> None:
        self._paint_warnings()

    @property
    def running(self) -> bool:
        from textual.worker import WorkerState

        return any(
            worker.node is self
            and worker.name == "run_sync"
            and worker.state is WorkerState.RUNNING
            for worker in self.workers
        )

    def action_start(self) -> None:
        if self.running:
            told(self, "A sync is already running. Press x to cancel it.", "warning")
            return
        self._sources.clear()
        self._warnings.clear()
        self._done = self._fetched = 0
        self.run_sync()

    def action_cancel(self) -> None:
        if not self.running:
            told(self, "Nothing to cancel. Press s to start a sync.", "warning")
            return
        self.workers.cancel_node(self)
        self.say("[yellow]Sync cancelled.[/yellow]")

    def action_back(self) -> None:
        if not self.close_help():
            self.workers.cancel_node(self)
            self.dismiss(None)

    @work(exclusive=True)
    async def run_sync(self) -> None:
        import sqlite3

        from stage.companies import RegistryError, load_boards
        from stage.paths import registry_override
        from stage.runlock import AnotherRunInProgressError, run_lock_path, single_run
        from stage.services.maintenance import (
            RECHECK_NOTICE,
            rescreen,
            upgrade_note,
            upgrade_pending,
        )
        from stage.services.sync import sync

        repo = self.repository
        database = database_of(self.app)
        if repo is None or database is None:
            return
        try:
            companies, personal = await asyncio.to_thread(load_boards, None)
        except RegistryError as exc:
            self.say(f"[red]{quoted(sentence(str(exc)))}[/red]")
            return
        if personal.problem:
            told(self, f"Your own boards were left out. {sentence(personal.problem)}", "warning")
        if self.dry_run:
            async for event in sync(repo, companies, dry_run=True):
                self._absorb(event)
            return
        from stage.http import online

        if not await asyncio.to_thread(online):
            self.say("[yellow]No internet connection, so the sync did not start.[/yellow]")
            return
        try:
            with single_run("sync", run_lock_path(database), busy="press s again when it finishes"):
                if await upgrade_pending(repo):
                    self.say(quoted(RECHECK_NOTICE))
                    self.say(quoted(upgrade_note(await rescreen(repo))))
                async for event in sync(
                    repo,
                    companies,
                    dry_run=False,
                    close_orphans=not personal.problem and registry_override() is None,
                ):
                    self._absorb(event)
        except AnotherRunInProgressError as exc:
            self.say(f"[yellow]This sync did not start. {quoted(str(exc))}[/yellow]")
        except sqlite3.Error as exc:
            self.say(f"[red]{quoted(self.database_trouble(exc))}[/red]")

    def _absorb(self, event: "SyncEvent") -> None:
        from stage.domain import (
            CompanyDeferred,
            CompanyFailed,
            CompanyFinished,
            CompanyUnchanged,
            SourceBlocked,
            SourceFailed,
            SourceFinished,
            SourceStarted,
            SyncFinished,
            SyncStarted,
        )

        if isinstance(event, SyncStarted):
            self.query_one("#overall", Static).update(overall_bar(0, 1))
        elif isinstance(event, SourceStarted):
            self._sources[event.source] = (0, 1 if event.feed else event.companies)
        elif isinstance(event, SourceFinished):
            done, _ = self._sources.get(event.source, (0, 0))
            self._sources[event.source] = (done, done)
        elif isinstance(
            event, CompanyFinished | CompanyUnchanged | CompanyFailed | CompanyDeferred
        ):
            done, total = self._sources.get(event.source, (0, 0))
            self._sources[event.source] = (done + 1, max(total, done + 1))
            self._done += 1
            if isinstance(event, CompanyFinished):
                self._fetched += event.fetched
            elif isinstance(event, CompanyFailed) and not (event.refused or event.gone):
                self._note(f"{event.source}/{event.company}: {plain_error(event.error)}")
        elif isinstance(event, SourceBlocked):
            until = event.blocked_until.astimezone().strftime("%H:%M")
            why = plain_error(event.reason) if event.reason.strip() else "no reason recorded"
            self._note(f"{event.source} blocked until {until}: {why}")
        elif isinstance(event, SourceFailed):
            self._note(f"{event.source}: {plain_error(event.error)}")
        elif isinstance(event, SyncFinished):
            self._finish(event)
            return
        self._paint()

    def _note(self, message: str) -> None:
        self._warnings.append(message)
        del self._warnings[:-MAX_WARNINGS]

    @property
    def planned(self) -> int:
        return sum(total for _, total in self._sources.values())

    def _paint(self) -> None:
        self.query_one("#overall", Static).update(overall_bar(self._done, max(self.planned, 1)))
        table = self.query_one("#sources", DataTable)
        table.clear()
        for name, (done, total) in sorted(self._sources.items()):
            mark = " ok" if total and done >= total else ""
            table.add_row(cell(name, 20), source_bar(done, total), f"{done}/{total}{mark}")
        self.say(
            f"[b]{self._done:,}[/b] of {self.planned:,} boards and feeds, "
            f"[b]{counted(self._fetched, 'posting')}[/b] fetched"
        )
        self._paint_warnings()

    def _paint_warnings(self) -> None:
        if not self._warnings:
            return
        room = max(2, min(SHOWN_WARNINGS, (self.app.size.height - 20) // 2))
        width = max(20, self.app.size.width - 4)
        shown = "\n".join(f"  {inline(line, width)}" for line in self._warnings[-room:])
        more = len(self._warnings) - room
        hidden = f" [dim]{more:,} earlier not shown[/dim]" if more > 0 else ""
        lead = "" if self.has_class("narrow") else "\n"
        self.query_one("#warnings", Static).update(
            f"{lead}[yellow]Warnings ({len(self._warnings):,})[/yellow]{hidden}\n{shown}"
        )

    def _finish(self, event: "SyncFinished") -> None:
        tone = {"success": "green", "partial": "yellow"}.get(event.outcome.value, "red")
        rejected = f", {event.quarantined:,} rejected" if event.quarantined else ""
        why = f" [dim]({quoted(event.partial_reason)})[/dim]" if event.partial_reason else ""
        self._paint()
        self.say(
            f"[{tone}]{event.outcome.value}[/{tone}]{why}: [b]{event.added:,}[/b] added, "
            f"{event.updated:,} updated, {event.closed:,} closed{rejected}"
        )
        kept = " Rejected postings are kept, and v there reviews them." if event.quarantined else ""
        self.query_one("#detail", Static).update(
            f"[dim]Press escape to go back to the postings, or s to sync again.{kept}[/dim]"
        )
