import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, TextIO

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from stage.domain import WorkdayCrawl
    from stage.services.canary import CanaryReport
    from stage.services.coverage import CoverageReport
    from stage.services.export import ExportResult
    from stage.services.health import DoctorReport, SourceHealth, StatsReport
    from stage.services.query import JobListing, PostingDetail

from stage.domain import (
    UNKNOWN_TERM,
    BucketPlan,
    CandidateSkipped,
    CompanyDeferred,
    CompanyFailed,
    CompanyFinished,
    CompanyUnchanged,
    CoverageState,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    IntegrityRepair,
    Job,
    PlannedRequest,
    PlatformCandidate,
    PlatformProbed,
    ProbeVerdict,
    QuarantinedJob,
    RateState,
    RequestLogged,
    SourceBlocked,
    SourceCapped,
    SourceFailed,
    SourceFinished,
    SourceFresh,
    SourceResting,
    SourceRotated,
    SourceStarted,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    SyncStarted,
    UnroutableCompanies,
    UrlResolved,
    UrlUnrecognized,
    VisitState,
    VolumeVerdict,
    web_url,
)
from stage.domain.text import first_line as _first_line
from stage.domain.text import sanitize as _sanitize
from stage.domain.text import summary as _summary
from stage.domain.text import truncate as _truncate


def terminal() -> Console:
    return Console(emoji=False)


def sanitize(value: str) -> str:
    return escape(_sanitize(value))


def truncate(value: str, width: int) -> str:
    return escape(_truncate(value, width))


def first_line(value: str) -> str:
    return escape(_first_line(value))


def summary(value: str, width: int) -> str:
    return escape(_summary(value, width))


def plain(value: str, style: str = "") -> Text:
    return Text(_sanitize(value), style=style)


def clipped(value: str, width: int, style: str = "") -> Text:
    return Text(_truncate(value, width), style=style)


def failure(exc: BaseException) -> Text:
    return Text(_sanitize(str(exc)), style="red")


def place(raw: str) -> str:
    from stage.normalize.location import display_location

    return display_location(raw)


def rule() -> Text:
    return Text("-" * 60, style="dim")


def quoted(value: str, width: int) -> str:
    return f"'{truncate(value, width)}'"


def _link(text: Text, raw: str) -> None:
    url = web_url(raw)
    if url is not None:
        text.stylize(f"link {url}")


def _duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def render_rate_state(
    console: Console,
    states: Sequence[RateState],
    now: datetime,
    *,
    verbose: bool = False,
) -> None:
    if not states:
        console.print("[dim]No rate state stored. Nothing has been throttled or blocked.[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("bucket")
    table.add_column("status")
    table.add_column("interval", justify="right")
    table.add_column("fails", justify="right")
    table.add_column("rotation")
    table.add_column("reason")

    for state in sorted(states, key=lambda item: item.bucket):
        if state.is_blocked(now):
            status = f"[red]blocked {_duration(state.blocks_remaining_s(now))}[/red]"
        elif state.blocked_until is not None:
            status = "[yellow]block expired[/yellow]"
        elif state.min_interval_override is not None:
            status = "[yellow]tightened[/yellow]"
        else:
            status = "[green]clear[/green]"
        override = (
            f"{state.min_interval_override:.2f}s"
            if state.min_interval_override is not None
            else "[dim]default[/dim]"
        )
        table.add_row(
            truncate(state.bucket, 34),
            status,
            override,
            str(state.consecutive_failures),
            truncate(state.rotation_cursor, 24) or "[dim]—[/dim]",
            summary(state.reason, 4000 if verbose else 40) or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(
        "[dim]Tightening decays on each clean run. "
        "[bold]stage sources --reset-rate-limit <bucket>[/bold] resets a block now.[/dim]"
    )


NARROW_COLUMNS = 70
CACHE_RATIO_MINIMUM = 20


def _age_style(first_seen: datetime, now: datetime) -> tuple[str, str]:
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
) -> None:
    moment = now or datetime.now(UTC)
    if not jobs:
        console.print(_empty_state(window_days, last_sync_at, moment, hint))
        return
    addressable = len(jobs) if numbered is None else numbered

    narrow = console.width < NARROW_COLUMNS
    row_width = max(2, len(str(len(jobs))))
    age_width = 6
    seen_width = 0 if narrow else 10
    company_width = 14 if narrow else 18
    location_width = 0 if narrow else 18
    used = row_width + age_width + seen_width + company_width + location_width
    columns = 4 if narrow else 6
    title_width = max(16, console.width - used - 2 * columns)

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("#", width=row_width, justify="right", no_wrap=True)
    table.add_column("Age", width=age_width, no_wrap=True)
    if not narrow:
        table.add_column("Seen", width=seen_width, no_wrap=True)
    table.add_column("Company", width=company_width, no_wrap=True, overflow="ellipsis")
    table.add_column("Title", width=title_width, no_wrap=True, overflow="ellipsis")
    if not narrow:
        table.add_column("Location", width=location_width, no_wrap=True, overflow="ellipsis")

    for position, job in enumerate(jobs, start=1):
        style, label = _age_style(job.first_seen, moment)
        title = clipped(job.title_raw, title_width, style=style)
        _link(title, job.apply_url_raw)
        number = str(position) if position <= addressable else ""
        cells = [Text(number, style="dim"), Text(label, style=style)]
        if not narrow:
            cells.append(Text(job.first_seen.astimezone().strftime("%Y-%m-%d")))
        cells.append(Text(truncate(job.company, 22)))
        cells.append(title)
        if not narrow:
            cells.append(Text(truncate(place(job.location_raw) or "—", 22)))
        table.add_row(*cells)

    console.print(table)
    shown = len(jobs)
    suffix = f" of {total_matching}" if total_matching > shown else ""
    capped = f"  Only the first {addressable} are numbered." if addressable < shown else ""
    console.print(
        f"\n[dim]{shown} posting(s){suffix}.  "
        f"stage show 1 or stage open 1 acts on a numbered row.{capped}[/dim]"
    )


def _dropped_punctuation(query: str) -> str:
    import unicodedata

    def carries_punctuation(word: str) -> bool:
        return any(
            unicodedata.category(character)[0] in "PS" and character != "_" for character in word
        )

    return ", ".join(word for word in query.split() if carries_punctuation(word))


def render_search(
    console: Console,
    listing: "JobListing",
    *,
    now: datetime | None = None,
    numbered: int | None = None,
    hint: str = "",
) -> None:
    if not listing.terms:
        console.print(
            f"[yellow]Nothing searchable in {quoted(listing.query, 40)}.[/yellow] "
            "Search matches whole words. Letters and digits, accents optional."
        )
        return
    if not listing.jobs:
        matched = " ".join(listing.terms)
        why = hint or (
            "Terms are combined with AND and matched as prefixes, so drop a word "
            "to widen it, or relax a filter."
        )
        console.print(f"[yellow]No posting matches {matched!r}.[/yellow] {why}")
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
    )
    console.print(f"[dim]Matched {' '.join(listing.terms)} as prefixes, ranked by relevance.[/dim]")
    dropped = _dropped_punctuation(listing.query)
    if dropped:
        console.print(
            f"[yellow]Punctuation is ignored, so {sanitize(dropped)} was searched as "
            f"{' '.join(listing.terms)}.[/yellow]"
        )


def _field(label: str, value: str) -> Text:
    return Text.assemble((f"{label:<14}", "bold"), value)


def render_posting(console: Console, detail: "PostingDetail", now: datetime | None = None) -> None:
    job = detail.job
    moment = now or datetime.now(UTC)
    style, age = _age_style(job.first_seen, moment)

    title = plain(job.title_raw, style=f"bold {style}")
    _link(title, job.apply_url_raw)
    console.print(title)
    console.print(plain(job.company, style="cyan"))
    console.print()

    remote = f" ({job.remote_scope.value})" if job.remote_scope else ""
    where = f"{_sanitize(place(job.location_raw)) or '—'} [{job.location.value}]{remote}"

    def state(label: str, value: str) -> None:
        if value and value != UNKNOWN_TERM:
            console.print(_field(label, value))

    console.print(_field("status", job.status.value))
    console.print(_field("location", where))
    state("term", job.term)
    state("role", job.role.value)
    state("language", job.language.value)
    state("degree", job.degree_requirement.value)
    if job.work_auth_flag:
        console.print(_field("work auth", "restricted: the posting states an eligibility limit"))
    if job.compensation:
        console.print(_field("compensation", _sanitize(job.compensation)))
    console.print(_field("first seen", f"{job.first_seen.astimezone():%Y-%m-%d} ({age})"))
    console.print(_field("last seen", f"{job.last_seen.astimezone():%Y-%m-%d}"))
    if job.source_posted_at:
        console.print(_field("source date", f"{job.source_posted_at.astimezone():%Y-%m-%d}"))
    console.print(_field("source", f"{job.source} / {job.board_key}"))
    console.print()
    console.print(plain(_sanitize(job.apply_url_raw) or "—", style="blue"))
    console.print(plain(job.id, style="dim"))

    if detail.canonical is not None:
        console.print()
        console.print(
            f"[yellow]Linked as a duplicate of[/yellow] {detail.canonical.id} "
            f"({detail.canonical.source}) — that row is the one [bold]stage list[/bold] shows."
        )
    if detail.duplicates:
        console.print()
        console.print(f"[bold]Also published as[/bold] ({len(detail.duplicates)})")
        for other in detail.duplicates:
            console.print(f"  {other.source:<16} {truncate(other.title_raw, 48):<48} {other.id}")

    console.print()
    if job.description.strip():
        console.print("[bold]Description[/bold]")
        console.print(plain(job.description.strip()))
    else:
        console.print(
            "[dim]No description stored. Feeds publish none, and some boards only carry "
            "one on the posting page.[/dim]"
        )


def render_export(console: Console, result: "ExportResult") -> None:
    console.print(export_summary(result))
    for note in result.notes:
        console.print(f"  [yellow]{sanitize(note)}[/yellow]")
    if result.notes:
        console.print(
            "  [dim]Those characters are absent from the embedded font and were dropped from "
            "the PDF only. Export json or csv to keep them.[/dim]"
        )


def export_summary(result: "ExportResult") -> str:
    truncated = (
        f" [yellow]{result.total_matching - result.count} more match the filters — raise "
        "--limit to include them.[/yellow]"
        if result.total_matching > result.count
        else ""
    )
    return (
        f"Exported {result.count} posting(s) as {result.fmt.value} to "
        f"[bold]{sanitize(str(result.path))}[/bold].{truncated}"
    )


_COVERAGE_STYLE = {
    CoverageState.PRODUCING: "green",
    CoverageState.EMPTY: "yellow",
    CoverageState.FAILING: "red",
    CoverageState.STALE: "yellow",
    CoverageState.NEVER_REACHED: "dim",
    CoverageState.UNROUTABLE: "red",
}


def render_contradictions(console: Console, report: "CoverageReport") -> None:
    if not report.contradictions:
        console.print("[dim]No review verdict contradicts the registry or has aged out.[/dim]")
        return
    table = Table(
        title="Review verdicts to re-derive", box=None, pad_edge=False, header_style="bold"
    )
    table.add_column("Company")
    table.add_column("Verdict")
    table.add_column("Why it no longer holds")
    for record, reason in report.contradictions:
        table.add_row(
            sanitize(record.company), sanitize(record.disposition.value), sanitize(reason)
        )
    console.print(table)


ROW_PREVIEW = 30
NAME_PREVIEW = 8
BOARD_PREVIEW = 10


def render_coverage(
    console: Console,
    report: "CoverageReport",
    now: datetime,
    *,
    include_classified: bool = False,
    include_contradictions: bool = False,
    limit: int | None = ROW_PREVIEW,
) -> None:
    if include_contradictions:
        render_contradictions(console, report)
        return
    counts: dict[CoverageState, int] = {}
    for row in report.rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    breakdown = ", ".join(
        f"[{_COVERAGE_STYLE[state]}]{counts[state]} {state.value}[/{_COVERAGE_STYLE[state]}]"
        for state in CoverageState
        if state in counts
    )
    console.print(f"[bold]{report.enabled}[/bold] enabled row(s): {breakdown or 'none'}")
    console.print(f"[dim]{report.disabled} disabled row(s) are not expected to produce.[/dim]")

    notes = (
        (CoverageState.FAILING, "have never succeeded", "a fetch problem; see stage doctor"),
        (CoverageState.STALE, "have not succeeded lately", "stale rather than empty"),
        (CoverageState.UNROUTABLE, "have no adapter", "enabled rows nothing will ever fetch"),
        (CoverageState.NEVER_REACHED, "have not been polled yet", "no evidence either way"),
    )
    for state, label, note in notes:
        _render_coverage_note(console, report, state, label, note, limit=limit)

    gaps = report.gaps
    if gaps:
        console.print()
        console.print(
            f"[bold]No internships open right now[/bold] ({len(gaps)}) "
            "[dim]— these boards answered, they just had nothing matching[/dim]"
        )
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("company")
        table.add_column("board")
        table.add_column("last success", justify="right")
        shown = gaps if limit is None else gaps[:limit]
        for row in shown:
            table.add_row(
                truncate(row.company, 28), truncate(row.board, 38), _ago(row.last_success_at, now)
            )
        console.print(table)
        if len(gaps) > len(shown):
            console.print(
                f"  [dim]… and {len(gaps) - len(shown)} more; --all lists every row[/dim]"
            )
        console.print("[dim]Expected most of the year. Internship postings are seasonal.[/dim]")

    if report.unregistered:
        console.print()
        console.print(
            f"[bold]Seen in a feed, absent from the registry[/bold] ({len(report.unregistered)})"
        )
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("company")
        table.add_column("postings", justify="right")
        table.add_column("sources")
        listed = report.unregistered if limit is None else report.unregistered[:limit]
        for unknown in listed:
            table.add_row(
                truncate(unknown.company, 34),
                str(unknown.postings),
                ", ".join(unknown.sources),
            )
        console.print(table)
        if len(report.unregistered) > 30:
            console.print(f"  [dim]… and {len(report.unregistered) - 30} more[/dim]")
        console.print(
            '[dim]After researching an employer, record it with [bold]stage classify "Company" '
            '--status feed-only --note "why"[/bold]. To identify a career-board URL without '
            "fetching it, use [bold]stage discover --url URL[/bold].[/dim]"
        )

    if include_classified:
        console.print()
        if report.classifications:
            console.print(f"[bold]Reviewed feed employers[/bold] ({len(report.classifications)})")
            table = Table(box=None, pad_edge=False, header_style="bold")
            table.add_column("company")
            table.add_column("status")
            table.add_column("checked", justify="right")
            table.add_column("note")
            for entry in report.classifications:
                table.add_row(
                    truncate(entry.company, 26),
                    entry.disposition.value,
                    entry.checked_on.date().isoformat(),
                    truncate(entry.note, 56),
                )
            console.print(table)
        else:
            console.print("[dim]No feed employers have been reviewed yet.[/dim]")


def _render_coverage_note(
    console: Console,
    report: "CoverageReport",
    state: CoverageState,
    label: str,
    note: str,
    *,
    limit: int | None = NAME_PREVIEW,
) -> None:
    rows = [row for row in report.rows if row.state is state]
    if not rows:
        return
    cap = len(rows) if limit is None else NAME_PREVIEW
    names = ", ".join(truncate(row.company, 24) for row in rows[:cap])
    more = f" and {len(rows) - cap} more" if len(rows) > cap else ""
    console.print()
    console.print(f"[bold]{len(rows)} row(s) {label}[/bold] — {note}")
    console.print(f"  [dim]{names}{more}[/dim]")


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "[red]never[/red]"
    return _duration((now - when).total_seconds()) + " ago"


def _ratio(value: float | None) -> str:
    return "[dim]—[/dim]" if value is None else f"{value:.0%}"


def render_source_health(
    console: Console, sources: Sequence["SourceHealth"], stale_after_days: int
) -> None:
    if not sources:
        console.print("[dim]No sync has run yet. Run stage sync.[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("source", no_wrap=True)
    table.add_column("open", justify="right")
    table.add_column("volume", no_wrap=True)
    table.add_column("ok", justify="right")
    table.add_column("cache", justify="right")
    table.add_column("mid", justify="right")
    table.add_column("slow", justify="right")
    table.add_column("calls", justify="right")
    table.add_column("boards")

    for source in sources:
        verdict = source.volume.verdict
        if verdict is VolumeVerdict.COLLAPSED:
            volume = "[red]collapsed[/red]"
        elif verdict is VolumeVerdict.DROPPED:
            volume = "[red]dropped[/red]"
        elif verdict is VolumeVerdict.UNPROVEN:
            volume = "[dim]unproven[/dim]"
        else:
            volume = "[green]steady[/green]"

        failing, stale = len(source.failing_boards), len(source.stale_boards)
        if failing:
            boards = f"[red]{failing} failing[/red]"
            if stale:
                boards += f", [yellow]{stale} stale[/yellow]"
        elif stale:
            boards = f"[yellow]{stale} stale[/yellow]"
        elif source.boards:
            boards = f"[green]{len(source.boards)} ok[/green]"
        else:
            boards = "[dim]—[/dim]"

        rate = source.success_rate
        if rate is None:
            success = "[dim]—[/dim]"
        elif rate < 1.0:
            success = f"[yellow]{rate:.0%}[/yellow]"
        else:
            success = f"[green]{rate:.0%}[/green]"

        table.add_row(
            source.source,
            str(source.stored),
            volume,
            success,
            _ratio(source.cache_hit_ratio),
            f"{source.latency_p50_ms:.0f}ms",
            f"{source.latency_p95_ms:.0f}ms",
            str(source.requests),
            boards,
        )

    console.print(table)
    console.print(
        "[dim]open: postings held now.  ok: successful fetches.  cache: served "
        "unchanged.\nmid and slow: typical and worst response times.  calls: requests "
        f"made.\nA board is stale after {stale_after_days} days without a success, and "
        "failing when it has never succeeded.[/dim]"
    )


def render_workday_crawl_progress(console: Console, crawls: Sequence["WorkdayCrawl"]) -> None:
    console.print("[bold]Workday crawl progress[/bold]")
    if not crawls:
        console.print("[dim]No incomplete Workday crawl is retained.[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("board")
    table.add_column("next offset", justify="right")
    table.add_column("reported total", justify="right")
    for crawl in crawls:
        table.add_row(
            truncate(crawl.board, 48),
            str(crawl.next_offset),
            str(crawl.total) if crawl.total is not None else "[dim]unknown[/dim]",
        )
    console.print(table)
    console.print(
        "[dim]These boards are part-way through a paged crawl and resume next sync. "
        "Their postings stay open until a full pass finishes.[/dim]"
    )


def render_board_health(
    console: Console,
    sources: Sequence["SourceHealth"],
    now: datetime,
    *,
    verbose: bool = False,
) -> None:
    rows = [
        board
        for source in sources
        for board in source.boards
        if board.state is not VisitState.HEALTHY
    ]
    if not rows:
        console.print("[green]Every board that rotation has reached succeeded recently.[/green]")
        return

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("board")
    table.add_column("source")
    table.add_column("state")
    table.add_column("last success", justify="right")
    table.add_column("fails", justify="right")
    table.add_column("error")

    for board in rows:
        colour = "red" if board.state is VisitState.FAILING else "yellow"
        table.add_row(
            truncate(board.label, 30),
            board.source,
            f"[{colour}]{board.state.value}[/{colour}]",
            _ago(board.last_success_at, now),
            str(board.consecutive_failures),
            summary(board.last_error, 4000 if verbose else 40) or "[dim]—[/dim]",
        )

    console.print(table)
    hint = (
        ""
        if verbose or not any(len(board.last_error or "") > 40 for board in rows)
        else "  --verbose prints each error in full."
    )
    console.print(f"[dim]Boards not listed have not been reached yet.{hint}[/dim]")


def render_canary(
    console: Console,
    report: "CanaryReport",
    *,
    verbose: bool = False,
) -> None:
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("source")
    table.add_column("board")
    table.add_column("result")
    table.add_column("postings", justify="right")
    table.add_column("note")

    for probe in report.probes:
        if probe.is_failure:
            result = "[red]failed[/red]"
        elif probe.is_unreachable:
            result = "[yellow]unreachable[/yellow]"
        elif probe.is_empty:
            result = "[red]no postings[/red]"
        elif probe.unchanged:
            result = "[dim]unchanged[/dim]"
        else:
            result = "[green]ok[/green]"
        note = probe.error or probe.degraded
        table.add_row(
            probe.source,
            truncate(probe.company, 28),
            result,
            "[dim]—[/dim]" if probe.unchanged else str(probe.fetched),
            summary(note, 4000 if verbose else 44) or "[dim]—[/dim]",
        )

    console.print(table)
    if report.skipped_platforms:
        console.print(
            f"[dim]Skipped {', '.join(report.skipped_platforms)} : bot-protected, so never "
            "probed on a schedule.[/dim]"
        )
    console.print()
    if report.unreachable:
        console.print(
            f"[yellow]{len(report.unreachable)} board(s) refused or dropped the request.[/yellow] "
            "That is their server, not the parser. [bold]stage doctor[/bold] tracks "
            "repeat failures."
        )
    if report.passed:
        console.print(
            f"[green]{len(report.probes) - len(report.unreachable)} board(s) still answer "
            "the shape we parse.[/green]"
        )
    else:
        console.print(
            f"[red]{len(report.failures)} failed, {len(report.empties)} returned "
            "nothing.[/red] Rebuild the fixture from the captured payload."
        )


def render_repairs(console: "Console", repairs: Sequence[IntegrityRepair]) -> None:
    if not repairs:
        return
    console.print("[bold]Repaired[/bold]")
    for entry in repairs:
        console.print(f"  {entry.repaired} × {sanitize(entry.check)} — {sanitize(entry.detail)}")
    console.print()


def render_doctor(
    console: Console,
    report: "DoctorReport",
    now: datetime,
    *,
    limit: int | None = BOARD_PREVIEW,
    verbose: bool = False,
) -> None:
    console.print(f"[bold]schema[/bold] v{report.schema_version}")
    if report.never_synced:
        console.print(
            "[yellow]No sync has ever run here[/yellow], so integrity is clean by "
            "default. Run stage sync."
        )
    else:
        console.print(f"[bold]last sync[/bold] {_ago(report.last_sync_at, now)}")
    console.print()

    problems = report.integrity_problems
    if problems:
        console.print("[bold red]Integrity[/bold red]")
        for finding in problems:
            console.print(f"  [red]{finding.count}[/red] {finding.check} — {finding.detail}")
    else:
        console.print(
            f"[bold green]Integrity[/bold green] all {len(report.integrity)} checks clean"
        )
    console.print()

    console.print("[bold]Sources[/bold]")
    render_source_health(console, report.sources, report.stale_after_days)
    console.print()
    render_workday_crawl_progress(console, report.workday_crawls)

    alerts = report.volume_alerts
    if alerts:
        console.print()
        console.print("[bold red]Volume[/bold red]")
        for source in alerts:
            console.print(f"  [red]{source.source}[/red] {source.volume.detail}")

    if report.blocks:
        console.print()
        console.print("[bold red]Blocked buckets[/bold red]")
        for state in report.blocks:
            console.print(
                f"  [red]{state.bucket}[/red] for another "
                f"{_duration(state.blocks_remaining_s(now))} — "
                f"{first_line(state.reason) or 'no reason recorded'}"
            )
        console.print("  [dim]Clear one with stage sources --clear <bucket>.[/dim]")

    failing = report.failing_boards
    if failing:
        console.print()
        console.print(f"[bold yellow]Boards needing a look[/bold yellow] ({len(failing)})")
        shown_boards = failing if limit is None else failing[:limit]
        for board in shown_boards:
            console.print(
                f"  [yellow]{truncate(board.label, 32)}[/yellow] "
                f"({board.source}) {board.consecutive_failures} consecutive failure(s) — "
                f"{summary(board.last_error, 4000 if verbose else 48) or 'no error recorded'}"
            )
        if len(failing) > len(shown_boards):
            console.print(
                f"  [dim]… and {len(failing) - len(shown_boards)} more; --all lists every row[/dim]"
            )
        console.print(
            "[dim]These are registry rows to fix or switch off."
            + ("" if verbose else "  --verbose prints each error in full.")
            + "[/dim]"
        )

    due = report.due_for_recheck
    if due:
        console.print()
        console.print(f"[bold yellow]Registry rows due for re-check[/bold yellow] ({len(due)})")
        shown_due = due if limit is None else due[:limit]
        for entry in shown_due:
            console.print(f"  [yellow]{truncate(entry, 60)}[/yellow]")
        if len(due) > len(shown_due):
            console.print(
                f"  [dim]… and {len(due) - len(shown_due)} more; --all lists every row[/dim]"
            )
        console.print("[dim]Read each note and re-check before deciding.[/dim]")

    console.print()
    if not report.is_healthy:
        console.print("[red]Problems above need attention.[/red]")
    elif report.warnings:
        console.print(f"[yellow]No errors, {report.warnings} warning(s).[/yellow]")
    else:
        console.print("[green]Healthy.[/green]")


def render_stats(
    console: Console,
    report: "StatsReport",
    now: datetime,
    *,
    limit: int | None = BOARD_PREVIEW,
) -> None:
    console.print(
        f"[bold]{report.total_jobs}[/bold] canonical posting(s), "
        f"{report.duplicates} linked as duplicate(s), "
        f"[bold]{sum(report.quarantined.values())}[/bold] quarantined, "
        f"{report.tombstones} tombstone(s), {report.cached_urls} cached validator(s), "
        f"schema v{report.schema_version}"
    )
    console.print()

    if not report.runs:
        console.print("[dim]No sync runs recorded yet — run stage sync.[/dim]")
    else:
        table = Table(box=None, pad_edge=False, header_style="bold", title="Recent syncs")
        table.title_justify = "left"
        table.add_column("when")
        table.add_column("outcome")
        table.add_column("elapsed", justify="right")
        table.add_column("added", justify="right")
        table.add_column("closed", justify="right")
        table.add_column("quarantined", justify="right")
        table.add_column("requests", justify="right")
        table.add_column("cache", justify="right")
        for run in report.runs:
            requests = sum(stats.requests for stats in run.sources)
            cached = sum(stats.not_modified for stats in run.sources)
            elapsed = (run.finished_at - run.started_at).total_seconds()
            colour = {"success": "green", "partial": "yellow"}.get(run.outcome.value, "red")
            table.add_row(
                _ago(run.finished_at, now),
                f"[{colour}]{run.outcome.value}[/{colour}]",
                f"{elapsed:.1f}s",
                str(sum(stats.added for stats in run.sources)),
                str(sum(stats.closed for stats in run.sources)),
                str(sum(stats.quarantined for stats in run.sources)),
                str(requests),
                _ratio(cached / requests if requests else None),
            )
        console.print(table)

    for column, counts in report.composition.items():
        if not counts:
            continue
        console.print()
        console.print(f"[bold]{column}[/bold]")
        total = sum(counts.values())
        listed = list(counts.items())
        shown = listed if limit is None else listed[:limit]
        for bucket, count in shown:
            share = f"{count / total:.1%}" if total else "—"
            console.print(f"  {truncate(bucket, 24):26} {count:>6}  [dim]{share}[/dim]")
        if len(listed) > len(shown):
            console.print(
                f"  [dim]… and {len(listed) - len(shown)} more; --all lists every row[/dim]"
            )


def render_quarantine(
    console: Console,
    entries: Sequence[QuarantinedJob],
    *,
    total_matching: int,
    reason_counts: dict[str, int],
) -> None:
    if not entries:
        console.print(
            "[yellow]Nothing quarantined.[/yellow] Rejections appear here after "
            "[bold]stage sync[/bold]."
        )
        return

    narrow = console.width < NARROW_COLUMNS
    seen_width = 0 if narrow else 5
    company_width = 14 if narrow else 16
    location_width = 0 if narrow else 14
    reason_width = 18 if narrow else 22
    used = seen_width + company_width + location_width + reason_width
    columns = 3 if narrow else 5
    title_width = max(16, console.width - used - 2 * (columns + 1))

    table = Table(box=None, pad_edge=False, header_style="bold")
    if not narrow:
        table.add_column("Seen", width=seen_width, no_wrap=True)
    table.add_column("Company", width=company_width, no_wrap=True, overflow="ellipsis")
    table.add_column("Title", width=title_width, no_wrap=True, overflow="ellipsis")
    if not narrow:
        table.add_column("Location", width=location_width, no_wrap=True, overflow="ellipsis")
    table.add_column("Rejected for", width=reason_width, no_wrap=True, overflow="ellipsis")

    for entry in entries:
        title = clipped(entry.title_raw, title_width, style="dim")
        _link(title, entry.apply_url_raw)
        matched = f"{entry.reason.value}"
        if entry.matched_phrase:
            matched += f" ({truncate(entry.matched_phrase, 24)})"
        cells = []
        if not narrow:
            cells.append(Text(entry.first_seen.astimezone().strftime("%m-%d")))
        cells.append(Text(truncate(entry.company, 22)))
        cells.append(title)
        if not narrow:
            cells.append(Text(truncate(place(entry.location_raw) or "—", 26)))
        cells.append(Text(matched, style="yellow"))
        table.add_row(*cells)

    console.print(table)
    shown = len(entries)
    suffix = f" of {total_matching}" if total_matching > shown else ""
    console.print(f"\n[dim]{shown} rejected posting(s){suffix}.[/dim]")
    if reason_counts:
        breakdown = ", ".join(f"{reason} {count}" for reason, count in reason_counts.items())
        console.print(f"[dim]Across the whole table: {breakdown}.[/dim]")


def _empty_state(
    window_days: int | None,
    last_sync_at: datetime | None,
    now: datetime,
    hint: str = "",
) -> str:
    if last_sync_at is None:
        return (
            "[yellow]No postings yet.[/yellow] The database is empty — run [bold]stage sync[/bold] "
            "to fetch from the registry."
        )
    age = (now - last_sync_at).days
    when = "today" if age == 0 else f"{age} day(s) ago"
    window = f" in the last {window_days} days" if window_days is not None else ""
    if hint:
        return f"[yellow]No matching postings{window}.[/yellow] {hint}"
    return (
        f"[yellow]No matching postings{window}.[/yellow] Widen the window with "
        f"[bold]--last[/bold], relax a filter, or run [bold]stage sync[/bold] "
        f"(last sync: {when})."
    )


async def render_sync(
    console: Console,
    events: AsyncIterator[SyncEvent],
    *,
    request_log: TextIO | None = None,
    progress: Callable[[SyncEvent], None] | None = None,
) -> SyncOutcome:
    outcome = SyncOutcome.FAILURE
    failures: list[tuple[str, str, str]] = []
    planned = 0
    validated = 0
    total = 0
    done = 0

    def step() -> str:
        return f"[dim]{done:>4}/{total}[/dim] " if total else ""

    async for event in events:
        if progress is not None:
            progress(event)
        match event:
            case SyncStarted(sources=sources, companies=companies):
                total = companies
                source_names = ", ".join(sources)
                console.print(
                    f"[bold]Syncing[/bold] {companies} company board(s) via {source_names}"
                )
            case UnroutableCompanies(companies=stranded, platforms=platforms):
                console.print(
                    f"\n[bold red]No adapter for {', '.join(platforms)}[/bold red] — "
                    f"{len(stranded)} enabled row(s) will never be fetched: "
                    f"{truncate(', '.join(stranded), 70)}"
                )
                console.print(
                    "  [dim]Set [bold]enabled: false[/bold] on those rows to record the gap "
                    "without failing the run.[/dim]"
                )
            case BucketPlan() as bound:
                shared = f" ({', '.join(bound.sources)})" if len(bound.sources) > 1 else ""
                open_tag = "[yellow]" if bound.exceeds_ceiling else "[dim]"
                close_tag = "[/yellow]" if bound.exceeds_ceiling else "[/dim]"
                if bound.exceeds_ceiling and bound.planned > 1:
                    detail = (
                        f", worst case {bound.worst_case} against a ceiling of "
                        f"{bound.ceiling} (the ceiling stops the run early)"
                    )
                else:
                    detail = ""
                console.print(
                    f"  {open_tag}bucket {bound.bucket}{close_tag}{shared} — "
                    f"{bound.planned} planned{detail}"
                )
            case SourceBlocked() as blocked:
                console.print(
                    f"\n[bold yellow]{blocked.source} blocked[/bold yellow] — bucket "
                    f"[bold]{blocked.bucket}[/bold] is throttled for another "
                    f"{_duration(blocked.remaining_s)} "
                    f"(clears {blocked.blocked_until:%Y-%m-%d %H:%M UTC})"
                )
                reason = summary(blocked.reason, 70) if blocked.reason else "unknown"
                console.print(
                    f"  [dim]{blocked.consecutive_failures} consecutive failure(s): "
                    f"{reason}. Not fetched this run — clear it with "
                    f"[bold]stage sources --clear {blocked.bucket}[/bold].[/dim]"
                )
            case SourceStarted(source=source, companies=companies):
                console.print(f"\n[bold cyan]{source}[/bold cyan] — {companies} board(s)")
            case SourceRotated() as rotated:
                phase = "cycle complete" if rotated.wrapped else f"resumes after {rotated.cursor}"
                console.print(
                    f"  [dim]rotating[/dim] — {rotated.deferred} board(s) deferred to a later "
                    f"run on bucket {rotated.bucket} ({phase})"
                )
            case SourceCapped() as capped:
                console.print(
                    f"  [yellow]capped[/yellow] — {capped.spent} spent by {capped.source} in 24h "
                    f"across its buckets, so this run may use "
                    f"{capped.allowance} of {capped.ceiling} per bucket"
                )
            case SourceFresh() as fresh:
                console.print(
                    f"  [dim]fresh[/dim] — {fresh.skipped} refreshed within "
                    f"{fresh.refresh_interval_h:.0f}h, {fresh.remaining} left"
                )
            case SourceResting() as resting:
                console.print(
                    f"  [dim]resting[/dim] — {resting.skipped} board(s) backing off after "
                    f"repeated failures, {resting.remaining} left"
                )
            case PlannedRequest(company=company, url=url, has_validator=cached):
                if not planned:
                    console.print(
                        "  [dim]cached = validator on file, a 304 is expected; "
                        "cold = full response expected[/dim]"
                    )
                planned += 1
                validated += int(cached)
                cache_marker = "[cyan]cached[/cyan]" if cached else "[yellow]cold  [/yellow]"
                room = max(20, console.width - 34)
                console.print(f"  {cache_marker} {truncate(company, 22):<22} {truncate(url, room)}")
            case RequestLogged() as record:
                _write_request_log(request_log, record)
            case CompanyFinished(
                company=company, fetched=fetched, elapsed_ms=elapsed, degraded=degraded
            ):
                status_marker = "[yellow]part[/yellow]" if degraded else "[green]ok[/green]  "
                done += 1
                console.print(
                    f"  {step()}{status_marker} {sanitize(company):<28} "
                    f"{fetched:>4} posting(s)  {elapsed:>7.0f}ms"
                )
                if degraded:
                    console.print(f"         [yellow]{truncate(degraded, 88)}[/yellow]")
            case CompanyUnchanged(company=company, elapsed_ms=elapsed):
                done += 1
                console.print(
                    f"  {step()}[cyan]304[/cyan]  {sanitize(company):<28} "
                    f"{'unchanged':>14}  {elapsed:>7.0f}ms"
                )
            case CompanyFailed(source=source, company=company, error=error, elapsed_ms=elapsed):
                failures.append((source, company, error))
                done += 1
                console.print(
                    f"  {step()}[red]fail[/red] {sanitize(company):<28} "
                    f"{summary(error, 60)}  {elapsed:>7.0f}ms"
                )
            case CompanyDeferred(company=company):
                done += 1
                console.print(
                    f"  {step()}[yellow]budget[/yellow] {sanitize(company):<26} "
                    f"{'not attempted, deferred to the next run':>40}"
                )
            case SourceFailed(source=source, error=error):
                failures.append((source, source, error))
                console.print(f"  [red]fail[/red] {sanitize(source)}: {summary(error, 60)}")
            case SourceFinished() as finished:
                _render_source_summary(console, finished)
            case SyncFinished(dry_run=True) as finished:
                outcome = finished.outcome
                console.print(
                    f"\n[bold]dry run[/bold] — {planned} request(s) planned, none sent. "
                    f"{validated} carry a validator, so a real run would likely transfer "
                    f"{planned - validated} full response(s)."
                )
                if outcome is not SyncOutcome.SUCCESS:
                    console.print(
                        "[red]Pre-flight failed[/red] on a fault that needs no network to "
                        "see. Fix it before running the real sync."
                    )
            case SyncFinished() as finished:
                outcome = finished.outcome
                cache_note = ""
                if finished.requests:
                    cache_note = f", {finished.not_modified}/{finished.requests} cached"
                    if finished.requests >= CACHE_RATIO_MINIMUM:
                        ratio = finished.not_modified / finished.requests
                        cache_note += f" ({ratio:.0%})"
                purge_note = f", {finished.purged} purged" if finished.purged else ""
                quarantine_note = ""
                if finished.quarantined:
                    quarantine_note = f", [yellow]{finished.quarantined} quarantined[/yellow]"
                reason_note = (
                    f" [dim]({sanitize(finished.partial_reason)})[/dim]"
                    if finished.partial_reason
                    else ""
                )
                console.print(
                    f"\n[bold]{finished.outcome.value}[/bold]{reason_note} — "
                    f"{finished.added} added, {finished.updated} updated, "
                    f"{finished.closed} closed"
                    f"{quarantine_note}{purge_note}{cache_note}"
                )
                if finished.quarantined:
                    console.print(
                        "[dim]Rejected postings are kept. Review them with "
                        "[bold]stage quarantine[/bold].[/dim]"
                    )
            case _:
                continue

    if failures:
        console.print("\n[bold red]Failed sources[/bold red]")
        for source, company, error in failures:
            console.print(f"  {source}/{sanitize(company)}: {first_line(error)}")
        console.print(
            "\n[dim]Re-run one source with [bold]stage sync --source <name>[/bold] "
            "to reproduce.[/dim]"
        )
    return outcome


_VERDICT_STYLE = {
    ProbeVerdict.MATCH: ("green", "match"),
    ProbeVerdict.UNVERIFIED: ("yellow", "check"),
    ProbeVerdict.REJECTED: ("red", "reject"),
    ProbeVerdict.EMPTY: ("dim", "empty"),
    ProbeVerdict.ERROR: ("red", "error"),
    ProbeVerdict.MISS: ("dim", "miss"),
}


async def render_discovery(
    console: Console,
    events: AsyncIterator[DiscoveryEvent],
    *,
    verified_on: date | None = None,
    display_name: str | None = None,
    request_log: TextIO | None = None,
    collect: bool = False,
    progress: Callable[[DiscoveryEvent], None] | None = None,
) -> bool | DiscoveryFinished:
    from stage.companies import registry_entry_yaml
    from stage.services.discover import to_company

    resolved = False
    outcome: DiscoveryFinished | None = None

    def show_entry(name: str, candidate: PlatformCandidate, note: str) -> None:
        console.print(f"\n[bold green]{sanitize(name)}[/bold green] -> {candidate.label}")
        if note:
            console.print(f"  [dim]{sanitize(note)}[/dim]")
        letter = next((c for c in name.lower() if c.isalpha()), "a")
        console.print(f"\n[dim]Paste into src/stage/data/companies/{letter}.yaml:[/dim]")
        entry = registry_entry_yaml(to_company(name, candidate, verified_on=verified_on))
        for line in entry.splitlines():
            console.print(plain(f"  {line}"), highlight=False)

    async for event in events:
        if progress is not None:
            progress(event)
        match event:
            case DiscoveryStarted(companies=names, platforms=platforms, probes_planned=planned):
                console.print(
                    f"[bold]Probing[/bold] {len(names)} name(s) across {len(platforms)} "
                    f"platform(s) — {planned} probe(s), plus one board-metadata request "
                    "per positive"
                )
                console.print(
                    "  [dim]Slug guessing resolves ~16%, a third of them falsely. "
                    "Prefer [bold]--url[/bold].[/dim]"
                )
            case RequestLogged() as record:
                _write_request_log(request_log, record)
            case CandidateSkipped(company=company, slug=slug, reason=reason):
                console.print(
                    f"  [dim]skip[/dim]   {truncate(company, 22):<22} "
                    f"[dim]{sanitize(slug)} — {sanitize(reason)}[/dim]"
                )
            case UrlResolved(candidate=candidate, detail=detail):
                resolved = True
                note = detail
                if display_name is None:
                    note = (
                        f"{detail + '. ' if detail else ''}Set the display name with "
                        "--name — the slug is a board token, not a company name"
                    )
                show_entry(display_name or candidate.slug, candidate, note)
            case UrlUnrecognized(url=url, detail=detail):
                console.print(f"\n[yellow]Unrecognized[/yellow] {truncate(url, 70)}")
                console.print(f"  {sanitize(detail)}")
                _show_custom_skeleton(console, url, display_name)
            case PlatformProbed(result=result) if result.verdict is not ProbeVerdict.MISS:
                style, label = _VERDICT_STYLE[result.verdict]
                count = "" if result.job_count is None else f"{result.job_count:>5} job(s)"
                console.print(
                    f"  [{style}]{label:<6}[/{style}] {truncate(result.company, 22):<22} "
                    f"{result.candidate.label:<34} {count}"
                )
                if result.detail:
                    console.print(f"         [dim]{truncate(result.detail, 88)}[/dim]")
            case DiscoveryFinished() as finished:
                outcome = finished
                resolved = resolved or bool(finished.matched)
                _render_discovery_summary(console, finished, show_entry, quiet=collect)
            case _:
                continue
    return outcome if collect and outcome is not None else resolved


def _show_custom_skeleton(console: Console, url: str, display_name: str | None) -> None:
    target = web_url(url)
    if target is None:
        return
    console.print(
        "\n[dim]If that page fills itself in from a JSON request, this is "
        "[bold]custom_json[/bold]. Open DevTools, filter Fetch/XHR, reload the page, and copy "
        "the request that returns the job list. Then paste this into the registry file for that "
        "company's first letter, under src/stage/data/companies/, with the field names taken "
        "from that response:[/dim]"
    )
    for line in (
        f"- name: {display_name or 'REPLACE ME'}",
        "  platform: custom_json",
        f"  slug: {registry_slug(display_name or target)}",
        "  enabled: false",
        "  custom:",
        "    url: PASTE_THE_JSON_REQUEST_URL_HERE",
        "    jobs_path: data.jobs",
        "    fields:",
        "      id: id",
        "      title: title",
        "      location: location",
        "      url: absoluteUrl",
    ):
        console.print(plain(f"  {line}"), highlight=False)


def registry_slug(value: str) -> str:
    from stage.lexicon import fold

    return "-".join(fold(value).split())[:40] or "replace-me"


def _render_discovery_summary(
    console: Console,
    event: DiscoveryFinished,
    show_entry: Callable[[str, PlatformCandidate, str], None],
    quiet: bool = False,
) -> None:
    for warning in event.ceiling_hit:
        console.print(
            f"\n[bold red]Per-host ceiling reached[/bold red] — {sanitize(warning)}. "
            "Probing stopped for that platform; split the batch across runs."
        )
    for platform, count in event.non_json:
        console.print(
            f"\n[yellow]{platform}: all {count} probe(s) returned non-JSON.[/yellow] "
            "Open one URL by hand before trusting a miss here."
        )
    console.print(
        f"\n[bold]{len(event.matched)} match(es)[/bold], {len(event.unverified)} needing a "
        f"manual check, {len(event.rejected)} rejected, {event.missed} miss(es), "
        f"{event.errors} error(s) in {event.requests} request(s) "
        f"({event.elapsed_ms / 1000:.1f}s)"
    )
    for result in () if quiet else event.matched:
        show_entry(result.company, result.candidate, f"board name {result.board_name!r} confirmed")
    if event.unverified:
        console.print(
            "\n[yellow]Verify before adding.[/yellow] These expose no board name, "
            "and a 200 with jobs is not evidence of the right company."
        )
        for result in event.unverified:
            console.print(plain(f"  {result.company} -> {result.candidate.label} {result.url}"))
    if not event.matched and not event.unverified:
        console.print(
            "\n[dim]Nothing resolved. Re-run with "
            "[bold]stage discover --url <careers-page>[/bold], the only path that "
            "resolves Workday.[/dim]"
        )


def _render_source_summary(console: Console, event: SourceFinished) -> None:
    parts = [
        f"{event.added} added",
        f"{event.updated} updated",
        f"{event.closed} closed",
    ]
    if event.quarantined:
        parts.append(f"[yellow]{event.quarantined} quarantined[/yellow]")
    if event.requests:
        parts.append(f"{event.not_modified}/{event.requests} cached")
        parts.append(f"p50 {event.latency_p50_ms:.0f}ms")
        parts.append(f"p95 {event.latency_p95_ms:.0f}ms")
    if event.fetch_ms or event.normalize_ms or event.write_ms:
        parts.extend(
            (
                f"fetch {event.fetch_ms / 1000:.1f}s",
                f"process {event.normalize_ms / 1000:.1f}s",
                f"write {event.write_ms / 1000:.1f}s",
            )
        )
    if event.retries:
        parts.append(f"{event.retries} retried")
    if event.tightenings:
        parts.append(f"[yellow]{event.tightenings} rate tightening(s)[/yellow]")
    console.print(f"  [dim]{', '.join(parts)} in {event.elapsed_ms / 1000:.1f}s[/dim]")


def _write_request_log(stream: TextIO | None, record: RequestLogged) -> None:
    if stream is None:
        return
    stream.write(
        json.dumps(
            {
                "source": record.source,
                "method": record.method,
                "url": record.url,
                "status": record.status,
                "elapsed_ms": round(record.elapsed_ms, 2),
                "attempt": record.attempt,
                "error": record.error,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
