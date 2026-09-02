from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, ProgressBar, Static

from stage.tui.help import HelpOverlay
from stage.tui.safe import cell, quoted

if TYPE_CHECKING:
    from stage.domain import SyncEvent
    from stage.storage import AsyncRepository

MAX_WARNINGS = 40
BAR_WIDTH = 22


def source_bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    if total <= 0:
        return "-" * width
    filled = round(width * min(done, total) / total)
    return "#" * filled + "-" * (width - filled)


class SyncScreen(HelpOverlay, Screen[None]):
    HELP_TEXT = """[b]Sync[/b]
  s        start a sync
  x        cancel the run in progress

[dim]? closes this   escape goes back[/dim]"""
    BINDINGS = [
        Binding("s", "start", "start"),
        Binding("x", "cancel", "cancel"),
        Binding("question_mark", "help", "keys"),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, *, dry_run: bool = False) -> None:
        super().__init__()
        self.dry_run = dry_run
        self._sources: dict[str, tuple[int, int]] = {}
        self._warnings: list[str] = []
        self._done = 0
        self._planned = 0
        self._fetched = 0

    @property
    def repository(self) -> "AsyncRepository | None":
        from stage.tui.app import StageApp

        app = self.app
        return app.repository if isinstance(app, StageApp) else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="chips")
        yield ProgressBar(total=100, show_eta=False, id="overall")
        yield DataTable(id="sources", cursor_type="row")
        yield Static("", id="warnings")
        yield Static("", id="detail")
        yield Static("", id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sources", DataTable)
        table.add_columns("Source", "Progress", "Done")
        table.focus()
        self.query_one("#chips", Static).update(
            "[dim]Press s to start a sync, x to cancel, esc to go back.[/dim]"
        )
        self.query_one("#detail", Static).update(
            "[dim]A sync fetches every enabled board, classifies what it finds, and "
            "stores the postings that qualify.[/dim]"
        )

    @property
    def running(self) -> bool:
        from textual.worker import WorkerState

        return any(
            worker.name == "run_sync" and worker.state is WorkerState.RUNNING
            for worker in self.workers
        )

    def action_start(self) -> None:
        if self.running:
            self.notify("A sync is already running.", severity="warning")
            return
        self._sources.clear()
        self._warnings.clear()
        self._done = self._planned = self._fetched = 0
        self.run_sync()

    def action_cancel(self) -> None:
        if not self.running:
            self.notify("Nothing to cancel.", severity="warning")
            return
        self.workers.cancel_all()
        self.query_one("#chips", Static).update("[yellow]Sync cancelled.[/yellow]")

    def action_back(self) -> None:
        if self.close_help():
            return
        self.workers.cancel_all()
        self.dismiss(None)

    @work(exclusive=True)
    async def run_sync(self) -> None:
        from stage.companies import RegistryError, load_companies
        from stage.services.sync import sync

        repo = self.repository
        if repo is None:
            return
        try:
            companies = load_companies(None)
        except RegistryError as exc:
            self.query_one("#chips", Static).update(f"[red]{quoted(str(exc))}[/red]")
            return
        async for event in sync(repo, companies, dry_run=self.dry_run):
            self._absorb(event)

    def _absorb(self, event: "SyncEvent") -> None:
        from stage.domain import (
            CompanyDeferred,
            CompanyFailed,
            CompanyFinished,
            CompanyUnchanged,
            SourceBlocked,
            SourceFailed,
            SourceStarted,
            SyncFinished,
            SyncStarted,
        )

        if isinstance(event, SyncStarted):
            self._planned = event.companies
            self.query_one("#overall", ProgressBar).update(total=max(event.companies, 1))
        elif isinstance(event, SourceStarted):
            self._sources[event.source] = (0, event.companies)
        elif isinstance(
            event, CompanyFinished | CompanyUnchanged | CompanyFailed | CompanyDeferred
        ):
            done, total = self._sources.get(event.source, (0, 0))
            self._sources[event.source] = (done + 1, total)
            self._done += 1
            if isinstance(event, CompanyFinished):
                self._fetched += event.fetched
            elif isinstance(event, CompanyFailed):
                self._note(quoted(f"{event.source}/{event.company} - {event.error}"))
            self.query_one("#overall", ProgressBar).update(progress=self._done)
        elif isinstance(event, SourceBlocked | SourceFailed):
            self._note(quoted(f"{event.source} - {getattr(event, 'reason', 'failed')}"))
        elif isinstance(event, SyncFinished):
            self._finish(event)
            return
        self._paint()

    def _note(self, message: str) -> None:
        self._warnings.append(message)
        del self._warnings[:-MAX_WARNINGS]

    def _paint(self) -> None:
        table = self.query_one("#sources", DataTable)
        table.clear()
        for name, (done, total) in sorted(self._sources.items()):
            mark = " ok" if total and done >= total else ""
            table.add_row(cell(name, 20), source_bar(done, total), f"{done}/{total}{mark}")
        self.query_one("#chips", Static).update(
            f"[b]{self._done}[/b] of {self._planned} boards · "
            f"[b]{self._fetched}[/b] postings fetched"
        )
        if self._warnings:
            shown = "\n".join(f"  {line}" for line in self._warnings[-8:])
            self.query_one("#warnings", Static).update(
                f"\n[yellow]Warnings ({len(self._warnings)})[/yellow]\n{shown}"
            )

    def _finish(self, event: object) -> None:
        added = getattr(event, "added", 0)
        updated = getattr(event, "updated", 0)
        quarantined = getattr(event, "quarantined", 0)
        outcome = getattr(getattr(event, "outcome", None), "value", "finished")
        tone = {"success": "green", "partial": "yellow"}.get(outcome, "red")
        self._paint()
        self.query_one("#chips", Static).update(
            f"[{tone}]{outcome}[/{tone}] — [b]{added}[/b] added, {updated} updated, "
            f"{quarantined} quarantined"
        )
        self.query_one("#detail", Static).update(
            "[dim]Press escape to go back to the postings browser, or s to sync again.[/dim]"
        )
