from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App
from textual.binding import Binding

if TYPE_CHECKING:
    from stage.storage import AsyncRepository

from stage.tui.screens.postings import PostingsScreen
from stage.tui.screens.splash import SplashScreen

THEME_PATH = Path(__file__).parent / "theme.tcss"


class StageApp(App[None]):
    CSS_PATH = THEME_PATH
    TITLE = "Stage"
    ALLOW_MAXIMIZE = False

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+t", "next_theme", "theme", show=False),
    ]

    def __init__(self, database: Path, summary: str = "") -> None:
        super().__init__()
        self.database = database
        self.summary = summary
        self._stack = AsyncExitStack()
        self.repository: AsyncRepository | None = None

    def action_next_theme(self) -> None:
        from stage.tui.safe import told
        from stage.tui.state import store_theme

        names = sorted(self.available_themes)
        index = names.index(self.theme) if self.theme in names else -1
        self.theme = names[(index + 1) % len(names)]
        store_theme(self.theme)
        told(self, f"Theme is now {self.theme}")

    async def on_mount(self) -> None:
        from stage.storage import open_repository
        from stage.tui.state import load_theme

        chosen = load_theme()
        if chosen in self.available_themes:
            self.theme = chosen
        self.repository = await self._stack.enter_async_context(open_repository(self.database))
        self.push_screen(PostingsScreen())
        if self.summary:
            self.push_screen(SplashScreen(self.summary))

    async def on_unmount(self) -> None:
        await self._stack.aclose()


async def summarize(database: Path) -> str:
    import sqlite3

    from stage.companies import RegistryError, load_companies
    from stage.domain import JobFilters
    from stage.storage import open_repository

    try:
        employers = len(load_companies(None))
    except RegistryError:
        employers = 0
    try:
        async with open_repository(database) as repo:
            postings = await repo.count_jobs(JobFilters(limit=1))
            last = await repo.last_sync_at()
    except (OSError, sqlite3.DatabaseError):
        return "no database yet — run stage sync"
    when = f"last sync {last.astimezone().strftime('%Y-%m-%d %H:%M')}" if last else "never synced"
    return f"{postings:,} postings · {employers:,} employers · {when}"


def launch(database: Path, summary: str = "") -> None:
    StageApp(database, summary).run()
