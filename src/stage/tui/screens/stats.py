import sqlite3
from collections import Counter
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Static

from stage.tui.base import BACK, HELP, HelpPanel, StageScreen
from stage.tui.safe import counted, quoted

if TYPE_CHECKING:
    from stage.domain import Job

SAMPLE = 5000
BAR_WIDTH = 28
TOP_N = 12
LABEL_WIDTH = 32
TREND_DAYS = 30


def trend(jobs: "tuple[Job, ...]", days: int = TREND_DAYS) -> Counter[str]:
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=days)
    seen = sorted(job.first_seen.astimezone().date() for job in jobs if job.first_seen >= cutoff)
    return Counter(day.strftime("%m-%d") for day in seen)


def bars(
    counts: Counter[str], width: int = BAR_WIDTH, top: int = TOP_N, *, ranked: bool = True
) -> str:
    if not counts:
        return "[dim]nothing recorded yet[/dim]"
    from rich.cells import cell_len

    from stage.domain.text import truncate

    chosen = counts.most_common(top) if ranked else list(counts.items())[-top:]
    highest = max(value for _, value in chosen) or 1
    labels = [truncate(name, LABEL_WIDTH) for name, _ in chosen]
    label_width = max(cell_len(label) for label in labels)
    return "\n".join(
        f"{quoted(label)}{' ' * (label_width - cell_len(label))}  "
        f"[b]{'#' * round(width * value / highest)}[/b] {value:,}"
        for label, (_, value) in zip(labels, chosen, strict=True)
    )


class StatsScreen(StageScreen[None]):
    HELP_TEXT = """[b]Statistics[/b]
  r        reload from the database

[dim]? closes this   escape goes back[/dim]"""
    BINDINGS = [
        HELP,
        BACK,
        Binding("r", "reload", "reload", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._measured: tuple[tuple[Job, ...], dict[str, int], int] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static("[dim]loading...[/dim]", id="stats-grid"))
        yield HelpPanel(id="help")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def relayout(self) -> None:
        self._paint()

    def say(self, text: str) -> None:
        try:
            self.query_one("#stats-grid", Static).update(text)
        except NoMatches:
            return

    @work(exclusive=True)
    async def load(self) -> None:
        from stage.domain import JobFilters
        from stage.services.query import list_jobs

        repo = self.repository
        if repo is None:
            return
        try:
            listing = await list_jobs(repo, JobFilters(limit=SAMPLE), window_days=None)
            reasons = await repo.quarantine_reason_counts()
        except sqlite3.Error as exc:
            self.database_failed(exc)
            return
        self._measured = (listing.jobs, reasons, listing.total_matching)
        self._paint()

    def _paint(self) -> None:
        if self._measured is None:
            return
        try:
            self.query_one("#stats-grid", Static).update(self._summary(*self._measured))
        except NoMatches:
            return

    def _summary(self, jobs: "tuple[Job, ...]", reasons: dict[str, int], total: int) -> str:
        if not jobs:
            return "[dim]No postings yet. Press escape, then y to sync.[/dim]"
        width = min(BAR_WIDTH, max(8, self.app.size.width - LABEL_WIDTH - 14))
        blocks = (
            ("Postings by role", Counter(job.role.value for job in jobs)),
            ("Postings by location", Counter(job.location.value for job in jobs)),
            ("Postings by term", Counter(job.term for job in jobs)),
            ("Top employers", Counter(job.company for job in jobs)),
            ("Postings by source", Counter(job.source for job in jobs)),
            ("Rejected by reason", Counter(reasons)),
        )
        seen = trend(jobs)
        scope = (
            f"[b]{counted(total, 'posting')}[/b]\n"
            if len(jobs) >= total
            else f"[b]{total:,}[/b] postings, measured on the most recent {len(jobs):,}\n"
        )
        parts = [scope]
        parts.extend(f"\n[b]{title}[/b]\n{bars(counts, width)}" for title, counts in blocks)
        if seen:
            parts.append(
                f"\n[b]First seen over the last {TREND_DAYS} days[/b]\n"
                f"{bars(seen, width, top=TREND_DAYS, ranked=False)}"
            )
        return "\n".join(parts)

    def action_reload(self) -> None:
        self.load()
