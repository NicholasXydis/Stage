from collections import Counter
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from stage.tui.help import HelpOverlay
from stage.tui.safe import quoted

SAMPLE = 5000

if TYPE_CHECKING:
    from stage.domain import Job
    from stage.storage import AsyncRepository

BAR_WIDTH = 28
TOP_N = 12
TREND_DAYS = 30


def trend(jobs: "tuple[Job, ...]", days: int = TREND_DAYS) -> Counter[str]:
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=days)
    return Counter(
        job.first_seen.astimezone().strftime("%m-%d") for job in jobs if job.first_seen >= cutoff
    )


def bars(counts: Counter[str], width: int = BAR_WIDTH, top: int = TOP_N) -> str:
    if not counts:
        return "[dim]nothing recorded yet[/dim]"
    ranked = counts.most_common(top)
    highest = ranked[0][1] or 1
    label_width = max(len(name) for name, _ in ranked)
    return "\n".join(
        f"{quoted(name):<{label_width}}  [b]{'#' * round(width * value / highest)}[/b] {value}"
        for name, value in ranked
    )


class StatsScreen(HelpOverlay, Screen[None]):
    HELP_TEXT = """[b]Statistics[/b]
  r        reload from the database

[dim]? closes this   escape goes back[/dim]"""
    BINDINGS = [
        Binding("question_mark", "help", "keys"),
        Binding("escape", "back", "back"),
        Binding("r", "reload", "reload", show=False),
    ]

    @property
    def repository(self) -> "AsyncRepository | None":
        from stage.tui.app import StageApp

        app = self.app
        return app.repository if isinstance(app, StageApp) else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static("[dim]loading…[/dim]", id="stats-grid"))
        yield Static("", id="help")
        yield Footer()

    def on_mount(self) -> None:
        self._mark_loading()
        self.load()

    def _mark_loading(self) -> None:
        self.query_one("#stats-grid", Static).update("[dim]loading…[/dim]")

    @work(exclusive=True)
    async def load(self) -> None:
        from stage.domain import JobFilters
        from stage.services.query import list_jobs

        repo = self.repository
        if repo is None:
            return
        listing = await list_jobs(repo, JobFilters(limit=SAMPLE), window_days=None)
        reasons = await repo.quarantine_reason_counts()
        self.query_one("#stats-grid", Static).update(
            self._summary(listing.jobs, reasons, listing.total_matching)
        )

    def _summary(self, jobs: "tuple[Job, ...]", reasons: dict[str, int], total: int) -> str:
        if not jobs:
            return "[dim]No postings yet. Run stage sync first.[/dim]"
        blocks = (
            ("Postings by role", Counter(job.role.value for job in jobs)),
            ("Postings by location", Counter(job.location.value for job in jobs)),
            ("Postings by term", Counter(job.term for job in jobs)),
            ("Top employers", Counter(job.company for job in jobs)),
            ("Postings by source", Counter(job.source for job in jobs)),
            ("Quarantined by reason", Counter(reasons)),
        )
        seen = trend(jobs)
        scope = (
            f"[b]{total}[/b] postings\n"
            if len(jobs) >= total
            else f"[b]{total}[/b] postings, measured on the most recent {len(jobs)}\n"
        )
        parts = [scope]
        parts.extend(f"\n[b]{title}[/b]\n{bars(counts)}" for title, counts in blocks)
        if seen:
            ordered = Counter(dict(sorted(seen.items())))
            parts.append(
                f"\n[b]First seen over the last {TREND_DAYS} days[/b]\n"
                f"{bars(ordered, top=TREND_DAYS)}"
            )
        return "\n".join(parts)

    def action_reload(self) -> None:
        self.load()

    def action_back(self) -> None:
        if self.close_help():
            return
        self.dismiss(None)
