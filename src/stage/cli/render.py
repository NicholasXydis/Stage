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


def render_rate_state(console: Console, states: Sequence[RateState], now: datetime) -> None:
    if not states:
        console.print(
            "[dim]No rate state stored. Every bucket is at its configured posture — "
            "nothing has been throttled or blocked.[/dim]"
        )
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
            summary(state.reason, 40) or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(
        "[dim]Tightening decays on each clean run. "
        "[bold]stage sources --reset-rate-limit <bucket>[/bold] resets a block now.[/dim]"
    )


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
) -> None:
    moment = now or datetime.now(UTC)
    if not jobs:
        console.print(_empty_state(window_days, last_sync_at, moment))
        return

    fixed = (6, 10, 18, 18)
    title_width = max(24, console.width - sum(fixed) - 2 * len(fixed))

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Age", width=fixed[0], no_wrap=True)
    table.add_column("Seen", width=fixed[1], no_wrap=True)
    table.add_column("Company", width=fixed[2], no_wrap=True, overflow="ellipsis")
    table.add_column("Title", width=title_width, no_wrap=True, overflow="ellipsis")
    table.add_column("Location", width=fixed[3], no_wrap=True, overflow="ellipsis")

    for job in jobs:
        style, label = _age_style(job.first_seen, moment)
        title = clipped(job.title_raw, title_width, style=style)
        _link(title, job.apply_url_raw)
        table.add_row(
            Text(label, style=style),
            job.first_seen.astimezone().strftime("%Y-%m-%d"),
            truncate(job.company, 22),
            title,
            truncate(job.location_raw or "—", 22),
        )

    console.print(table)
    shown = len(jobs)
    suffix = f" of {total_matching}" if total_matching > shown else ""
    console.print(f"\n[dim]{shown} posting(s){suffix}.[/dim]")


def render_search(console: Console, listing: "JobListing", *, now: datetime | None = None) -> None:
    if not listing.terms:
        console.print(
            f"[yellow]Nothing searchable in {quoted(listing.query, 40)}.[/yellow] "
            "Search matches words — letters and digits, accents optional."
        )
        return
    if not listing.jobs:
        matched = " ".join(listing.terms)
        console.print(
            f"[yellow]No posting matches {matched!r}.[/yellow] Terms are combined with AND and "
            "matched as prefixes, so drop a word to widen it, or relax a filter."
        )
        return
    render_jobs(
        console,
        listing.jobs,
        total_matching=listing.total_matching,
        window_days=listing.window_days,
        last_sync_at=listing.last_sync_at,
        now=now,
    )
    console.print(f"[dim]Matched {' '.join(listing.terms)} as prefixes, ranked by relevance.[/dim]")


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
    where = f"{_sanitize(job.location_raw) or '—'} [{job.location.value}]{remote}"
    console.print(_field("id", job.id))
    console.print(_field("status", job.status.value))
    console.print(_field("location", where))
    console.print(_field("term", job.term))
    console.print(_field("role", job.role.value))
    console.print(_field("language", job.language.value))
    console.print(_field("degree", job.degree_requirement.value))
    if job.work_auth_flag:
        console.print(_field("work auth", "restricted — the posting states an eligibility limit"))
    if job.compensation:
        console.print(_field("compensation", _sanitize(job.compensation)))
    console.print(_field("first seen", f"{job.first_seen.astimezone():%Y-%m-%d} ({age})"))
    console.print(_field("last seen", f"{job.last_seen.astimezone():%Y-%m-%d}"))
    if job.source_posted_at:
        console.print(_field("source date", f"{job.source_posted_at.astimezone():%Y-%m-%d}"))
    console.print(_field("source", f"{job.source} / {job.board_key}"))
    console.print(_field("apply", _sanitize(job.apply_url_raw) or "—"))

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
            "[dim]No description stored. Feeds publish none, and some boards carry bodies "
            "only on a detail fetch.[/dim]"
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


def render_coverage(
    console: Console, report: "CoverageReport", now: datetime, *, include_classified: bool = False
) -> None:
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

    gaps = report.gaps
    if gaps:
        console.print()
        console.print("[bold yellow]Producing nothing, though the board answered[/bold yellow]")
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("company")
        table.add_column("board")
        table.add_column("last success", justify="right")
        for row in gaps:
            table.add_row(
                truncate(row.company, 28), truncate(row.board, 38), _ago(row.last_success_at, now)
            )
        console.print(table)
        console.print(
            "[dim]An answering board with no internships is a real state in August — "
            "compare it against [bold]stage stats[/bold] before switching a row off.[/dim]"
        )

    notes = (
        (CoverageState.NEVER_REACHED, "rotation has not reached yet", "no evidence either way"),
        (CoverageState.FAILING, "have never succeeded", "a fetch problem; see stage doctor"),
        (CoverageState.STALE, "have not succeeded lately", "stale rather than empty"),
        (CoverageState.UNROUTABLE, "have no adapter", "enabled rows nothing will ever fetch"),
    )
    for state, label, note in notes:
        _render_coverage_note(console, report, state, label, note)

    if report.unregistered:
        console.print()
        console.print(
            f"[bold]Seen in a feed, absent from the registry[/bold] ({len(report.unregistered)})"
        )
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("company")
        table.add_column("postings", justify="right")
        table.add_column("sources")
        for unknown in report.unregistered[:30]:
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
    console: Console, report: "CoverageReport", state: CoverageState, label: str, note: str
) -> None:
    rows = [row for row in report.rows if row.state is state]
    if not rows:
        return
    names = ", ".join(truncate(row.company, 24) for row in rows[:8])
    more = f" and {len(rows) - 8} more" if len(rows) > 8 else ""
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
    table.add_column("source")
    table.add_column("stored", justify="right")
    table.add_column("volume")
    table.add_column("success", justify="right")
    table.add_column("cache", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("req", justify="right")
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
        f"[dim]stored is open postings held. A board is stale after {stale_after_days} "
        "days without a success, failing when it has never succeeded.[/dim]"
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
        "[dim]Listed boards are still completing a safe reconciliation cycle; their "
        "postings cannot close until a safe terminal pass succeeds. Boards not listed "
        "have no retained partial cursor.[/dim]"
    )


def render_board_health(console: Console, sources: Sequence["SourceHealth"], now: datetime) -> None:
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
            summary(board.last_error, 40) or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(
        "[dim]A board with no row has not been reached by rotation yet, and is not "
        "listed here.[/dim]"
    )


def render_canary(console: Console, report: "CanaryReport") -> None:
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("source")
    table.add_column("board")
    table.add_column("result")
    table.add_column("postings", justify="right")
    table.add_column("note")

    for probe in report.probes:
        if probe.is_failure:
            result = "[red]failed[/red]"
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
            summary(note, 44) or "[dim]—[/dim]",
        )

    console.print(table)
    if report.skipped_platforms:
        console.print(
            f"[dim]Skipped {', '.join(report.skipped_platforms)} — bot-managed, never "
            "probed on a schedule.[/dim]"
        )
    console.print()
    if report.passed:
        console.print(
            f"[green]{len(report.probes)} board(s) still answer the shape we parse.[/green]"
        )
    else:
        console.print(
            f"[red]{len(report.failures)} failed, {len(report.empties)} returned "
            "nothing.[/red] Rebuild the fixture from the captured payload."
        )


def render_doctor(console: Console, report: "DoctorReport", now: datetime) -> None:
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
        for board in failing[:10]:
            console.print(
                f"  [yellow]{truncate(board.label, 32)}[/yellow] "
                f"({board.source}) {board.consecutive_failures} consecutive failure(s) — "
                f"{summary(board.last_error, 48) or 'no error recorded'}"
            )
        if len(failing) > 10:
            console.print(f"  [dim]… and {len(failing) - 10} more[/dim]")
        console.print("[dim]These are registry rows to fix or switch off.[/dim]")

    due = report.due_for_recheck
    if due:
        console.print()
        console.print(f"[bold yellow]Registry rows due for re-check[/bold yellow] ({len(due)})")
        for entry in due[:10]:
            console.print(f"  [yellow]{truncate(entry, 60)}[/yellow]")
        if len(due) > 10:
            console.print(f"  [dim]… and {len(due) - 10} more[/dim]")
        console.print("[dim]A disable reason expires; read the note and re-measure.[/dim]")

    console.print()
    if not report.is_healthy:
        console.print("[red]Problems above need attention.[/red]")
    elif report.warnings:
        console.print(f"[yellow]No errors, {report.warnings} warning(s).[/yellow]")
    else:
        console.print("[green]Healthy.[/green]")


def render_stats(console: Console, report: "StatsReport", now: datetime) -> None:
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
        for bucket, count in list(counts.items())[:10]:
            share = f"{count / total:.1%}" if total else "—"
            console.print(f"  {truncate(bucket, 24):26} {count:>6}  [dim]{share}[/dim]")


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

    fixed = (10, 18, 22, 22)
    title_width = max(20, console.width - sum(fixed) - 2 * len(fixed))

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Seen", width=fixed[0], no_wrap=True)
    table.add_column("Company", width=fixed[1], no_wrap=True, overflow="ellipsis")
    table.add_column("Title", width=title_width, no_wrap=True, overflow="ellipsis")
    table.add_column("Location", width=fixed[2], no_wrap=True, overflow="ellipsis")
    table.add_column("Rejected for", width=fixed[3], no_wrap=True, overflow="ellipsis")

    for entry in entries:
        title = clipped(entry.title_raw, title_width, style="dim")
        _link(title, entry.apply_url_raw)
        matched = f"{entry.reason.value}"
        if entry.matched_phrase:
            matched += f" ({truncate(entry.matched_phrase, 24)})"
        table.add_row(
            entry.first_seen.astimezone().strftime("%Y-%m-%d"),
            truncate(entry.company, 22),
            title,
            truncate(entry.location_raw or "—", 26),
            Text(matched, style="yellow"),
        )

    console.print(table)
    shown = len(entries)
    suffix = f" of {total_matching}" if total_matching > shown else ""
    console.print(f"\n[dim]{shown} rejected posting(s){suffix}.[/dim]")
    if reason_counts:
        breakdown = ", ".join(f"{reason} {count}" for reason, count in reason_counts.items())
        console.print(f"[dim]Across the whole table: {breakdown}.[/dim]")


def _empty_state(window_days: int | None, last_sync_at: datetime | None, now: datetime) -> str:
    if last_sync_at is None:
        return (
            "[yellow]No postings yet.[/yellow] The database is empty — run [bold]stage sync[/bold] "
            "to fetch from the registry."
        )
    age = (now - last_sync_at).days
    when = "today" if age == 0 else f"{age} day(s) ago"
    window = f" in the last {window_days} days" if window_days is not None else ""
    return (
        f"[yellow]No matching postings{window}.[/yellow] Try [bold]--all[/bold] to ignore the "
        f"window, relax a filter, or run [bold]stage sync[/bold] (last sync: {when})."
    )


async def render_sync(
    console: Console,
    events: AsyncIterator[SyncEvent],
    *,
    request_log: TextIO | None = None,
) -> SyncOutcome:
    outcome = SyncOutcome.FAILURE
    failures: list[tuple[str, str, str]] = []
    planned = 0
    validated = 0

    async for event in events:
        match event:
            case SyncStarted(sources=sources, companies=companies):
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
                console.print(
                    f"  {open_tag}bucket {bound.bucket}{close_tag}{shared} — "
                    f"{bound.planned} planned, "
                    f"worst case {bound.worst_case} against a ceiling of {bound.ceiling}"
                    f"{' (the ceiling stops the run early)' if bound.exceeds_ceiling else ''}"
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
                    f"  [yellow]capped[/yellow] — {capped.spent} spent on {capped.bucket} in 24h, "
                    f"so this run may use {capped.allowance} of {capped.ceiling}"
                )
            case SourceFresh() as fresh:
                console.print(
                    f"  [dim]fresh[/dim] — {fresh.skipped} refreshed within "
                    f"{fresh.refresh_interval_h:.0f}h, {fresh.remaining} left"
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
                console.print(
                    f"  {status_marker} {sanitize(company):<28} "
                    f"{fetched:>4} posting(s)  {elapsed:>7.0f}ms"
                )
                if degraded:
                    console.print(f"         [yellow]{truncate(degraded, 88)}[/yellow]")
            case CompanyUnchanged(company=company, elapsed_ms=elapsed):
                console.print(
                    f"  [cyan]304[/cyan]  {sanitize(company):<28} "
                    f"{'unchanged':>14}  {elapsed:>7.0f}ms"
                )
            case CompanyFailed(source=source, company=company, error=error, elapsed_ms=elapsed):
                failures.append((source, company, error))
                console.print(
                    f"  [red]fail[/red] {sanitize(company):<28} "
                    f"{summary(error, 60)}  {elapsed:>7.0f}ms"
                )
            case CompanyDeferred(company=company):
                console.print(
                    f"  [yellow]budget[/yellow] {sanitize(company):<26} "
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
                    ratio = finished.not_modified / finished.requests
                    cache_note = (
                        f", {finished.not_modified}/{finished.requests} cached ({ratio:.0%})"
                    )
                purge_note = f", {finished.purged} purged" if finished.purged else ""
                quarantine_note = ""
                if finished.quarantined:
                    quarantine_note = f", [yellow]{finished.quarantined} quarantined[/yellow]"
                console.print(
                    f"\n[bold]{finished.outcome.value}[/bold] — {finished.added} added, "
                    f"{finished.updated} updated, {finished.closed} closed"
                    f"{quarantine_note}{purge_note}{cache_note}"
                )
                if finished.quarantined:
                    console.print(
                        "[dim]Rejected postings are kept, not discarded — audit them with "
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
) -> bool | DiscoveryFinished:
    from stage.companies import registry_entry_yaml
    from stage.services.discover import to_company

    resolved = False
    outcome: DiscoveryFinished | None = None

    def show_entry(name: str, candidate: PlatformCandidate, note: str) -> None:
        console.print(f"\n[bold green]{sanitize(name)}[/bold green] -> {candidate.label}")
        if note:
            console.print(f"  [dim]{sanitize(note)}[/dim]")
        console.print("\n[dim]Paste into data/companies.yaml:[/dim]")
        entry = registry_entry_yaml(to_company(name, candidate, verified_on=verified_on))
        for line in entry.splitlines():
            console.print(plain(f"  {line}"), highlight=False)

    async for event in events:
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
        "the request that returns the job list. Then paste this into data/companies.yaml, with "
        "the field names taken from that response:[/dim]"
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
