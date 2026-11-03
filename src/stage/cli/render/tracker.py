from typing import TYPE_CHECKING

from rich.console import Console

from stage.cli.render import Cell, Column, cut, cut_often, grid, plain
from stage.cli.render.postings import field
from stage.domain.text import counted
from stage.domain.text import truncate as _truncate
from stage.domain.tracker import ApplicationStatus, PostingState, TrackedPosting, TrackerStats

if TYPE_CHECKING:
    from stage.services.tracker import ChangeReport, RemoveReport, TrackerListing

MAX_CHANGES_LISTED = 20
_REPLY_WORDS = {
    ApplicationStatus.INTERVIEWING: "interview",
    ApplicationStatus.OFFER: "offer",
    ApplicationStatus.REJECTED: "rejection",
}
_STATUS_STYLES = {
    ApplicationStatus.OFFER: "bold green",
    ApplicationStatus.INTERVIEWING: "green",
    ApplicationStatus.APPLIED: "cyan",
    ApplicationStatus.REJECTED: "dim",
    ApplicationStatus.WITHDRAWN: "dim",
}


def render_changes(console: Console, headline: str, report: "ChangeReport") -> None:
    console.print(plain(headline))
    for change in report.changed[:MAX_CHANGES_LISTED]:
        entry = change.entry
        was = f" (was {change.before.value})" if change.before not in (None, entry.status) else ""
        console.print(plain(f"  {_posting_line(entry)}{was}"))
    _and_more(console, len(report.changed))
    for entry in report.unchanged:
        line = f"  Already marked {entry.status.value}: {_posting_line(entry, mark=False)}"
        console.print(plain(line, "dim"))


def render_removed(console: Console, report: "RemoveReport") -> None:
    if report.removed:
        console.print(plain(f"Stopped tracking {counted(len(report.removed), 'posting')}."))
        for entry in report.removed[:MAX_CHANGES_LISTED]:
            console.print(plain(f"  {_posting_line(entry, mark=False)} (was {entry.status.value})"))
        _and_more(console, len(report.removed))
    for reference in report.not_tracked:
        console.print(plain(f"{reference} was not tracked.", "dim"))


def _and_more(console: Console, count: int) -> None:
    if count > MAX_CHANGES_LISTED:
        console.print(plain(f"  ...and {count - MAX_CHANGES_LISTED:,} more", "dim"))


def _posting_line(entry: TrackedPosting, *, mark: bool = True) -> str:
    snapshot = entry.snapshot
    head = f"{entry.marker or entry.status.value} " if mark else ""
    return f"{head}{_truncate(snapshot.title, 60)} - {_truncate(snapshot.company, 30)}"


def render_tracker(
    console: Console,
    listing: "TrackerListing",
    *,
    status: ApplicationStatus | None,
    numbered: int,
) -> None:
    if not listing.rows:
        if status is not None:
            console.print(plain(f"Nothing tracked as {status.value}."))
        else:
            console.print(
                "Nothing tracked yet. Run [bold]stage list[/bold], then [bold]stage save 1[/bold] "
                "or [bold]stage applied 1[/bold]."
            )
        _hidden_line(console, listing.hidden, status)
        return
    row_width = max(2, len(str(len(listing.rows))))
    status_width = 12
    state_width = 7
    narrow = console.width < row_width + status_width + 10 + 18 + state_width + 16 + 2 * 5
    on_width = 0 if narrow else 10
    count = 5 if narrow else 6
    spare = console.width - row_width - status_width - on_width - state_width - 2 * count
    company_width = max(4, min(14, spare - 8)) if narrow else 18
    title_width = max(4 if narrow else 16, spare - company_width)

    columns = [
        Column("#", row_width, right=True, crop=False),
        Column("Status", status_width, crop=False),
    ]
    if not narrow:
        columns.append(Column("On", on_width, crop=False))
    columns += [
        Column("Company", company_width),
        Column("Title", title_width),
        Column("Posting", state_width, crop=False),
    ]

    def row(position: int, entry: TrackedPosting, state: PostingState) -> list[Cell]:
        cells = [
            Cell(str(position) if position <= numbered else "", "dim"),
            Cell(entry.status.value, _STATUS_STYLES.get(entry.status, "")),
        ]
        if not narrow:
            cells.append(Cell(entry.on.isoformat()))
        cells.append(Cell(cut_often(entry.snapshot.company, company_width)))
        cells.append(Cell(cut(entry.snapshot.title, title_width), url=entry.snapshot.apply_url))
        cells.append(Cell(state.value, "" if state is PostingState.OPEN else "dim"))
        return cells

    grid(
        console,
        columns,
        (row(position, item.entry, item.state) for position, item in enumerate(listing.rows, 1)),
    )
    console.print(
        f"\n[dim]{len(listing.rows):,} tracked.  stage show 1 or "
        "stage track 1 --status interviewing acts on numbered rows.[/dim]"
    )
    _hidden_line(console, listing.hidden, status)


def _hidden_line(console: Console, hidden: int, status: ApplicationStatus | None) -> None:
    if hidden and status is None:
        console.print(plain(f"{hidden:,} hidden: stage tracker --status hidden", "dim"))


def render_tracker_stats(console: Console, stats: TrackerStats) -> None:
    counts = " | ".join(
        f"{status.value} {count}"
        for status, count in stats.by_status.items()
        if count and status is not ApplicationStatus.HIDDEN
    )
    console.print(field("Tracked", f"{stats.tracked:,}" + (f"  ({counts})" if counts else "")))
    console.print(
        field(
            "Applied",
            f"{stats.applied_last_7} in the last 7 days, {stats.applied_last_30} in the last 30",
        )
    )
    console.print(field("Heard back", _heard_back(stats)))
    hidden = stats.by_status.get(ApplicationStatus.HIDDEN, 0)
    if hidden:
        console.print(field("Hidden", f"{hidden:,}"))


def _heard_back(stats: TrackerStats) -> str:
    line = f"{stats.heard_back} of {counted(stats.applications, 'application')}"
    if stats.applications:
        line += f" ({stats.heard_back * 100 // stats.applications}%)"
    answers = [
        counted(count, _REPLY_WORDS[status]) for status, count in stats.replies.items() if count
    ]
    return f"{line}: {', '.join(answers)}" if answers else line
