from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from stage.cli.render import (
    Cell,
    Column,
    cut,
    cut_often,
    grid,
    link,
    place,
    plain,
    quoted,
    sanitize,
    truncate,
)
from stage.domain import DEFAULT_WINDOW_DAYS, UNKNOWN_TERM, Job
from stage.domain.text import counted, posted_day
from stage.domain.text import sanitize as _sanitize
from stage.domain.tracker import TrackedPosting

if TYPE_CHECKING:
    from stage.domain.preferences import Preferences
    from stage.services.export import ExportResult
    from stage.services.query import JobListing, PostingDetail

QUERY_SHOWN = 60


def age_style(first_seen: datetime, now: datetime) -> tuple[str, str]:
    age_days = (now - first_seen).days
    if age_days <= 1:
        return "bold green", "new"
    if age_days <= 3:
        return "green", "recent"
    if age_days <= 7:
        return "default", "week"
    return "dim", "older"


def render_jobs(
    console: Console,
    jobs: Sequence[Job],
    *,
    total_matching: int,
    window_days: int | None,
    last_sync_at: datetime | None,
    now: datetime | None = None,
    numbered: int | None = None,
    hint: str = "",
    marks: dict[str, str] | None = None,
) -> None:
    moment = now or datetime.now(UTC)
    if not jobs:
        console.print(_empty_state(window_days, last_sync_at, moment, hint))
        return
    marked = marks or {}
    addressable = len(jobs) if numbered is None else numbered

    row_width = max(2, len(str(len(jobs))))
    age_width = 6
    narrow = console.width < row_width + age_width + 10 + 18 + 18 + 16 + 2 * 5
    seen_width = 0 if narrow else 10
    location_width = 0 if narrow else 18
    count = 4 if narrow else 6
    spare = console.width - row_width - age_width - seen_width - location_width - 2 * count
    company_width = max(4, min(14, spare - 8)) if narrow else 18
    title_width = max(4 if narrow else 16, spare - company_width)

    columns = [Column("#", row_width, right=True, crop=False), Column("Age", age_width, crop=False)]
    if not narrow:
        columns.append(Column("Seen", seen_width, crop=False))
    columns += [Column("Company", company_width), Column("Title", title_width)]
    if not narrow:
        columns.append(Column("Location", location_width))

    def row(position: int, job: Job) -> list[Cell]:
        style, label = age_style(job.first_seen, moment)
        mark = marked.get(job.id)
        title = f"{mark} {job.title_raw}" if mark else job.title_raw
        cells = [Cell(str(position) if position <= addressable else "", "dim"), Cell(label, style)]
        if not narrow:
            cells.append(Cell(job.first_seen.astimezone().strftime("%Y-%m-%d")))
        cells.append(Cell(cut_often(job.company, company_width)))
        cells.append(Cell(cut(title, title_width), style, job.apply_url_raw))
        if not narrow:
            cells.append(Cell(cut_often(place(job.location_raw) or "-", location_width)))
        return cells

    grid(console, columns, (row(position, job) for position, job in enumerate(jobs, start=1)))
    shown = len(jobs)
    suffix = f" of {total_matching:,}" if total_matching > shown else ""
    capped = f"  Only the first {addressable:,} are numbered." if addressable < shown else ""
    last = min(5, addressable)
    opened = "stage open 1" if last == 1 else f"stage open 1-{last}"
    console.print(
        f"\n[dim]{counted(shown, 'posting')}{suffix}.  "
        f"stage show 1 or {opened} acts on numbered rows.{capped}[/dim]"
    )


def render_search(
    console: Console,
    listing: "JobListing",
    *,
    now: datetime | None = None,
    numbered: int | None = None,
    hint: str = "",
    marks: dict[str, str] | None = None,
) -> None:
    if not listing.terms:
        console.print(
            f"[yellow]Nothing searchable in {quoted(listing.query, 40)}.[/yellow] "
            "Search matches the start of words. Letters and digits, accents optional."
        )
        return
    matched = truncate(" ".join(listing.terms), QUERY_SHOWN)
    if not listing.jobs:
        why = hint or (
            "Terms are combined with AND and matched as prefixes, so drop a word "
            "to widen it, or relax a filter."
        )
        console.print(f'[yellow]No posting matches "{matched}".[/yellow] {why}')
        return
    render_jobs(
        console,
        listing.jobs,
        total_matching=listing.total_matching,
        window_days=listing.window_days,
        last_sync_at=listing.last_sync_at,
        now=now,
        numbered=numbered,
        hint=hint,
        marks=marks,
    )
    console.print(f"[dim]Matched {matched} as prefixes, newest first.[/dim]")
    from stage.services.query import searched_as

    note = searched_as(listing.query, listing.terms, QUERY_SHOWN)
    if note:
        console.print(f"[yellow]{escape(note)}[/yellow]")


def field(label: str, value: str) -> Text:
    return Text.assemble((f"{label:<14}", "bold"), value)


def render_posting(
    console: Console,
    detail: "PostingDetail",
    now: datetime | None = None,
    tracked: TrackedPosting | None = None,
) -> None:
    job = detail.job
    moment = now or datetime.now(UTC)
    style, age = age_style(job.first_seen, moment)

    title = plain(job.title_raw, style=f"bold {style}")
    link(title, job.apply_url_raw)
    console.print(title)
    console.print(plain(job.company, style="cyan"))
    console.print()

    remote = f" ({job.remote_scope.value})" if job.remote_scope else ""
    where = f"{_sanitize(job.location_raw.strip()) or '-'} [{job.location.value}]{remote}"

    def state(label: str, value: str) -> None:
        if value and value != UNKNOWN_TERM:
            console.print(field(label, value))

    console.print(field("status", job.status.value))
    console.print(field("location", where))
    state("term", job.term)
    state("role", job.role.value)
    state("language", job.language.value)
    state("degree", job.degree_requirement.value)
    if job.work_auth_flag:
        console.print(field("work auth", "restricted: the posting states an eligibility limit"))
    if job.compensation:
        console.print(field("compensation", _sanitize(job.compensation)))
    console.print(field("first seen", f"{job.first_seen.astimezone():%Y-%m-%d} ({age})"))
    console.print(field("last seen", f"{job.last_seen.astimezone():%Y-%m-%d}"))
    if job.source_posted_at:
        console.print(field("source date", posted_day(job.source_posted_at).isoformat()))
    console.print(field("source", f"{job.source} / {job.board_key}"))
    if tracked is not None:
        tracked_fields(console, tracked)
    console.print()
    console.print(plain(_sanitize(job.apply_url_raw) or "-", style="blue"), soft_wrap=True)
    console.print(plain(job.id, style="dim"), soft_wrap=True)

    if detail.canonical is not None:
        console.print()
        console.print(
            f"[yellow]Linked as a duplicate of[/yellow] {sanitize(detail.canonical.id)} "
            f"({detail.canonical.source}), the row [bold]stage list[/bold] shows."
        )
    if detail.duplicates:
        console.print()
        console.print(f"[bold]Also published as[/bold] ({len(detail.duplicates)})")
        for other in detail.duplicates:
            console.print(
                f"  {other.source:<16} {truncate(other.title_raw, 48):<48} {sanitize(other.id)}",
                soft_wrap=True,
            )

    console.print()
    if job.description.strip():
        console.print("[bold]Description[/bold]")
        console.print(plain(job.description.strip()))
    else:
        console.print(
            "[dim]No description stored. Feeds publish none, and some boards only carry "
            "one on the posting page.[/dim]"
        )


def tracked_fields(console: Console, entry: TrackedPosting) -> None:
    console.print(field("tracked", f"{entry.status.value} on {entry.on.isoformat()}"))
    if entry.note:
        console.print(field("note", entry.note))
    if len(entry.history) > 1:
        steps = ", ".join(
            f"{change.status.value} {change.on.isoformat()}" for change in entry.history
        )
        console.print(field("history", steps))


def render_snapshot(console: Console, entry: TrackedPosting) -> None:
    snapshot = entry.snapshot
    title = plain(snapshot.title, style="bold")
    link(title, snapshot.apply_url)
    console.print(title)
    console.print(plain(snapshot.company, style="cyan"))
    console.print()
    console.print(field("location", _sanitize(place(snapshot.location)) or "-"))
    if snapshot.term and snapshot.term != UNKNOWN_TERM:
        console.print(field("term", snapshot.term))
    console.print(field("source", snapshot.source))
    tracked_fields(console, entry)
    console.print()
    console.print(plain(_sanitize(snapshot.apply_url) or "-", style="blue"), soft_wrap=True)
    console.print(plain(entry.posting_id, style="dim"), soft_wrap=True)
    console.print()
    console.print(
        "[dim]Stage no longer stores this posting, so this is the copy kept when you "
        "tracked it.[/dim]"
    )


def render_preferences(
    console: Console,
    preferences: "Preferences",
    *,
    saved: bool,
    counts: dict[str, int] | None = None,
) -> None:
    from stage.domain.preferences import COMPANY_SEPARATOR

    def listed(values: Sequence[str], separator: str = ", ") -> str:
        return separator.join(values) if values else "-"

    def with_open(name: str, folded: str) -> str:
        return name if counts is None else f"{name} ({counts.get(folded, 0):,} open)"

    console.print(plain("Saved your preferences." if saved else "Your preferences", "bold"))
    console.print(field("Locations", listed([v.value for v in preferences.locations])))
    console.print(field("Roles", listed([v.value for v in preferences.roles])))
    console.print(field("Terms", listed(list(preferences.terms))))
    follow = [with_open(e.name, e.folded) for e in preferences.follow]
    avoid = [with_open(e.name, e.folded) for e in preferences.avoid]
    console.print(field("Following", listed(follow, COMPANY_SEPARATOR)))
    console.print(field("Leaving out", listed(avoid, COMPANY_SEPARATOR)))
    about = f"{len(preferences.about):,} characters" if preferences.about else "-"
    console.print(field("About you", about))
    console.print()
    if preferences.narrows:
        console.print(
            "[dim][bold]stage list --mine[/bold] shows postings that match, newest first.[/dim]"
        )
        console.print(
            "[dim]Followed employers show from any place. Postings with no location are left "
            "out.[/dim]"
        )
        console.print(
            "[dim]Postings with no term given still count, since most boards publish none.[/dim]"
        )
    else:
        console.print(
            "[dim]Nothing chosen yet. Run [bold]stage prefs[/bold] in a terminal, or pass "
            "--location, --role and --term.[/dim]"
        )


def render_export(console: Console, result: "ExportResult") -> None:
    console.print(export_summary(result), soft_wrap=True)
    for note in result.notes:
        console.print(f"  [yellow]{sanitize(note)}[/yellow]")


def export_summary(result: "ExportResult") -> str:
    left = result.total_matching - result.count
    truncated = (
        f" [yellow]{left:,} more match the filters. Raise --limit or add --all to include "
        "them.[/yellow]"
        if left > 0
        else ""
    )
    return (
        f"Exported {counted(result.count, 'posting')} as {result.fmt.value} to "
        f"[bold]{sanitize(str(result.path))}[/bold].{truncated}"
    )


def _empty_state(
    window_days: int | None,
    last_sync_at: datetime | None,
    now: datetime,
    hint: str = "",
) -> str:
    if last_sync_at is None:
        return (
            "[yellow]No postings yet.[/yellow] The database is empty. Run [bold]stage sync[/bold] "
            "to fetch from the registry."
        )
    age = max((now - last_sync_at).days, 0)
    when = "today" if age == 0 else f"{counted(age, 'day')} ago"
    window = f" in the last {window_days} days" if window_days is not None else ""
    if hint:
        return f"[yellow]No matching postings{window}.[/yellow] {hint}"
    widen = (
        "Widen the window with [bold]--last[/bold], relax a filter"
        if window_days is not None and window_days < DEFAULT_WINDOW_DAYS
        else "Relax a filter"
    )
    return (
        f"[yellow]No matching postings{window}.[/yellow] {widen}, or run "
        f"[bold]stage sync[/bold] (last sync: {when})."
    )
