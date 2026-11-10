from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from stage.cli.render import (
    Cell,
    Column,
    ago,
    cut,
    cut_often,
    duration,
    error_cell,
    error_text,
    grid,
    more,
    place,
    ratio,
    sanitize,
    table,
    truncate,
)
from stage.domain import (
    CoverageState,
    IntegrityRepair,
    QuarantinedJob,
    RateState,
    VisitState,
    VolumeVerdict,
)
from stage.domain.text import counted
from stage.domain.text import first_line as _first_line
from stage.domain.text import sanitize as _sanitize

if TYPE_CHECKING:
    from stage.domain import WorkdayCrawl
    from stage.services.boards import BoardListSummary
    from stage.services.canary import CanaryReport
    from stage.services.connect import AppHealth
    from stage.services.coverage import CoverageReport
    from stage.services.health import BoardHealth, DoctorReport, SourceHealth, StatsReport

NARROW_COLUMNS = 70
WIDE_HEALTH = 90
ROW_PREVIEW = 30
NAME_PREVIEW = 8
BOARD_PREVIEW = 10
STATS_FULL_WIDTH = 72
STATS_REJECTED_WIDTH = 50
FULL_ERROR = 4000

_COVERAGE_STYLE = {
    CoverageState.PRODUCING: "green",
    CoverageState.EMPTY: "yellow",
    CoverageState.FAILING: "red",
    CoverageState.STALE: "yellow",
    CoverageState.NEVER_REACHED: "dim",
    CoverageState.UNROUTABLE: "red",
}
_COVERAGE_NOTES = (
    (CoverageState.FAILING, "failed on their last fetch", "stage doctor shows why"),
    (CoverageState.STALE, "have not succeeded lately", "stale rather than empty"),
    (CoverageState.UNROUTABLE, "have no adapter", "enabled rows nothing will ever fetch"),
    (CoverageState.NEVER_REACHED, "have not been polled yet", "no evidence either way"),
)
_VOLUME = {
    VolumeVerdict.COLLAPSED: "[red]collapsed[/red]",
    VolumeVerdict.DROPPED: "[red]dropped[/red]",
    VolumeVerdict.UNPROVEN: "[dim]unproven[/dim]",
}


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

    shown = table(
        "bucket", "status", "interval", "fails", "rotation", "reason", right=("interval", "fails")
    )
    for state in sorted(states, key=lambda item: item.bucket):
        if state.is_blocked(now):
            status = f"[red]blocked {duration(state.blocks_remaining_s(now))}[/red]"
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
        shown.add_row(
            truncate(state.bucket, 34),
            status,
            override,
            str(state.consecutive_failures),
            truncate(state.rotation_cursor, 24) or "[dim]-[/dim]",
            error_cell(state.reason, FULL_ERROR if verbose else 40) or "[dim]-[/dim]",
        )

    console.print(shown)
    console.print(
        "[dim]Tightening decays on each clean run. "
        "[bold]stage sources --reset-rate-limit <bucket>[/bold] resets a block now.[/dim]"
    )


def render_source_health(
    console: Console, sources: Sequence["SourceHealth"], stale_after_days: int
) -> None:
    if not sources:
        console.print("[dim]No sync has run yet. Run stage sync.[/dim]")
        return

    narrow = console.width < WIDE_HEALTH
    timings = () if narrow else ("cache", "mid", "slow", "calls")
    shown = table(
        "source",
        "open",
        "volume",
        "ok",
        *timings,
        "boards",
        right=("open", "ok", "cache", "mid", "slow", "calls"),
    )
    shown.columns[0].no_wrap = True
    shown.columns[2].no_wrap = True

    for source in sources:
        failing, stale = len(source.failing_boards), len(source.stale_boards)
        refusing, gone = len(source.refusing_boards), len(source.gone_boards)
        parts = [
            f"[red]{failing} failing[/red]" if failing else "",
            f"[yellow]{stale} stale[/yellow]" if stale else "",
            f"[dim]{refusing} refuse here[/dim]" if refusing else "",
            f"[dim]{gone} gone[/dim]" if gone else "",
        ]
        if any(parts):
            boards = ", ".join(part for part in parts if part)
        elif source.boards:
            boards = f"[green]{len(source.boards)} ok[/green]"
        else:
            boards = "[dim]-[/dim]"

        rate = source.success_rate
        if rate is None:
            success = "[dim]-[/dim]"
        elif rate < 1.0:
            success = f"[yellow]{rate:.0%}[/yellow]"
        else:
            success = f"[green]{rate:.0%}[/green]"

        cells = [
            source.source,
            str(source.stored),
            _VOLUME.get(source.volume.verdict, "[green]steady[/green]"),
            success,
        ]
        if not narrow:
            cells += [
                ratio(source.cache_hit_ratio),
                f"{source.latency_p50_ms:.0f}ms",
                f"{source.latency_p95_ms:.0f}ms",
                str(source.requests),
            ]
        shown.add_row(*cells, boards)

    console.print(shown)
    legend = (
        ""
        if narrow
        else "  cache: served unchanged.\nmid and slow: typical and worst response times.  "
        "calls: requests made."
    )
    console.print(
        f"[dim]open: postings held now.  ok: successful fetches.{legend}\nA board is stale "
        f"after {counted(stale_after_days, 'day')} without a success, and failing when its "
        "last fetch failed.[/dim]"
    )


def render_workday_crawl_progress(
    console: Console, crawls: Sequence["WorkdayCrawl"], limit: int | None = None
) -> None:
    console.print("[bold]Workday crawl progress[/bold]")
    if not crawls:
        console.print("[dim]No incomplete Workday crawl is retained.[/dim]")
        return

    room = max(12, min(48, console.width - 31))
    shown = table("board", "next offset", "reported total", right=("next offset", "reported total"))
    for crawl in crawls[:limit]:
        shown.add_row(
            truncate(crawl.board, room),
            str(crawl.next_offset),
            str(crawl.total) if crawl.total is not None else "[dim]unknown[/dim]",
        )
    console.print(shown)
    more(console, len(crawls) - len(crawls[:limit]))
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

    shown = table(
        "board",
        "source",
        "state",
        "last success",
        "fails",
        "error",
        right=("last success", "fails"),
    )
    for board in rows:
        colour = {
            VisitState.FAILING: "red",
            VisitState.REFUSING: "dim",
            VisitState.GONE: "dim",
        }.get(board.state, "yellow")
        shown.add_row(
            truncate(board.label, 30),
            board.source,
            f"[{colour}]{board.state.value}[/{colour}]",
            ago(board.last_success_at, now),
            str(board.consecutive_failures),
            error_cell(board.last_error, FULL_ERROR if verbose else 40) or "[dim]-[/dim]",
        )

    console.print(shown)
    hint = (
        ""
        if verbose or not any(len(error_text(board.last_error)) > 40 for board in rows)
        else "  --verbose prints each error in full."
    )
    console.print(f"[dim]Boards not listed have not been reached yet.{hint}[/dim]")


def render_canary(
    console: Console,
    report: "CanaryReport",
    *,
    verbose: bool = False,
) -> None:
    shown = table("source", "board", "result", "postings", "note", right=("postings",))
    for probe in report.probes:
        if probe.is_failure:
            result = "[red]failed[/red]"
        elif probe.is_unreachable:
            result = "[yellow]unreachable[/yellow]"
        elif probe.is_empty:
            result = "[red]no postings[/red]"
        elif probe.declared_empty:
            result = "[dim]none listed[/dim]"
        elif probe.unchanged:
            result = "[dim]unchanged[/dim]"
        else:
            result = "[green]ok[/green]"
        shown.add_row(
            probe.source,
            truncate(probe.company, 28),
            result,
            "[dim]-[/dim]" if probe.unchanged else str(probe.fetched),
            error_cell(probe.error or probe.degraded, FULL_ERROR if verbose else 44)
            or "[dim]-[/dim]",
        )

    console.print(shown)
    if report.skipped_platforms:
        console.print(
            f"[dim]Skipped {', '.join(report.skipped_platforms)}: bot-protected boards are "
            "never probed on a schedule.[/dim]"
        )
    console.print()
    if report.unreachable:
        console.print(
            f"[yellow]{counted(len(report.unreachable), 'board')} refused or dropped the "
            "request.[/yellow] "
            "That is their server, not the parser. [bold]stage doctor[/bold] tracks "
            "repeat failures."
        )
    if report.failures or report.empties:
        console.print(
            f"[red]{len(report.failures)} failed, {len(report.empties)} returned "
            "nothing.[/red] Rebuild the fixture from the captured payload."
        )
    elif not report.timed_out:
        console.print(
            f"[green]Boards still answering in the shape we parse: "
            f"{len(report.probes) - len(report.unreachable)}.[/green]"
        )


def render_repairs(console: Console, repairs: Sequence[IntegrityRepair]) -> None:
    if not repairs:
        return
    console.print("[bold]Repaired[/bold]")
    for entry in repairs:
        console.print(
            f"  {sanitize(entry.check)}: {entry.repaired} repaired ({sanitize(entry.detail)})"
        )
    console.print()


def render_app_health(console: Console, apps: "Sequence[AppHealth]") -> None:
    console.print()
    if not apps:
        console.print("[bold]AI apps[/bold] [dim]none connected (stage connect sets one up)[/dim]")
        return
    console.print("[bold]AI apps[/bold]")
    for app in apps:
        state = app.status.value
        line = Text.assemble("  ", _sanitize(app.label), "  ")
        if state == "connected":
            line.append("connected", style="green")
            if app.actions:
                line.append(", actions on")
        else:
            line.append(state, style="yellow" if state == "outdated" else "red")
            if app.detail:
                line.append(f": {_sanitize(app.detail)}")
            line.append(f". Run stage connect {app.key}.")
        console.print(line)


def render_board_list(console: Console, boards: "BoardListSummary") -> None:
    console.print()
    line = Text.assemble(("Your boards", "bold"), " ")
    if boards.problem:
        line.append("could not be read, so Stage ignores them: ", style="yellow")
        line.append(_sanitize(boards.problem))
    elif not boards.boards:
        line.append("none yet (stage discover --unregistered --apply adds some)", style="dim")
    else:
        line.append(f"{counted(boards.enabled, 'board')} polled, ")
        line.append(f"{boards.recent:,} added in the last 7 days")
        if boards.disabled:
            line.append(f", {boards.disabled:,} switched off")
        if boards.shipped:
            line.append(f", {boards.shipped:,} now shipped with Stage")
        line.append(f" ({_sanitize(str(boards.path))})", style="dim")
    console.print(line, soft_wrap=True)


def render_doctor(
    console: Console,
    report: "DoctorReport",
    now: datetime,
    *,
    limit: int | None = BOARD_PREVIEW,
    verbose: bool = False,
    file_checked: bool = True,
) -> None:
    console.print(f"[bold]schema[/bold] v{report.schema_version}")
    if report.never_synced:
        console.print(
            "[yellow]No sync has ever run here[/yellow], so integrity is clean by "
            "default. Run stage sync."
        )
    else:
        console.print(f"[bold]last sync[/bold] {ago(report.last_sync_at, now)}")
    console.print()

    problems = report.integrity_problems
    if problems:
        console.print("[bold red]Integrity[/bold red]")
        for finding in problems:
            console.print(
                f"  {escape(finding.check)}: [red]{finding.count:,}[/red]. {escape(finding.detail)}"
            )
    else:
        console.print(
            f"[bold green]Integrity[/bold green] all {len(report.integrity)} checks clean"
        )
    if not file_checked:
        console.print("  [dim]The file was not read for damage. Plain stage doctor reads it.[/dim]")
    console.print()

    console.print("[bold]Sources[/bold]")
    render_source_health(console, report.sources, report.stale_after_days)
    console.print()
    render_workday_crawl_progress(console, report.workday_crawls, limit)

    if report.volume_alerts:
        console.print()
        console.print("[bold red]Volume[/bold red]")
        for source in report.volume_alerts:
            console.print(f"  [red]{source.source}[/red] {escape(source.volume.detail)}")

    if report.warning_blocks:
        console.print()
        console.print("[bold red]Blocked buckets[/bold red]")
        for state in report.warning_blocks:
            reason = escape(_first_line(error_text(state.reason))) or "no reason recorded"
            console.print(
                f"  [red]{escape(state.bucket)}[/red] for another "
                f"{duration(state.blocks_remaining_s(now))} ({reason})"
            )
        console.print("  [dim]Clear one with stage sources --reset-rate-limit <bucket>.[/dim]")

    if report.failing_boards:
        _render_failing_boards(console, report.failing_boards, limit=limit, verbose=verbose)
    if report.refusing_boards:
        _render_quiet_boards(
            console,
            "Boards that refuse this computer",
            report.refusing_boards,
            "They answer 401, 403 or 429 to this computer. Many refuse servers and cloud "
            "addresses but answer a home connection. Stage tries them less often and closes "
            "none of their postings.",
            limit=limit,
        )
        if report.resting_blocks:
            hosts = ", ".join(sorted(state.bucket for state in report.resting_blocks))
            console.print(f"[dim]Resting after a refusal: {escape(truncate(hosts, 400))}.[/dim]")
    if report.gone_boards:
        _render_quiet_boards(
            console,
            "Boards that no longer exist",
            report.gone_boards,
            "They answered 404 or 410 three times in a row. Stage checks them less often, "
            "closes none of their postings and reads them again if they come back. These are "
            "registry rows to fix or switch off.",
            limit=limit,
        )

    due = report.due_for_recheck
    if due:
        console.print()
        console.print(f"[bold yellow]Registry rows due for re-check[/bold yellow] ({len(due)})")
        shown_due = due if limit is None else due[:limit]
        for entry in shown_due:
            console.print(f"  [yellow]{truncate(entry, 60)}[/yellow]")
        more(console, len(due) - len(shown_due))
        console.print("[dim]Read each note and re-check before deciding.[/dim]")

    console.print()
    if not report.is_healthy:
        console.print("[red]Problems above need attention.[/red]")
    elif report.warnings:
        console.print(f"[yellow]No errors, {counted(report.warnings, 'warning')}.[/yellow]")
    else:
        console.print("[green]Healthy.[/green]")


def _render_failing_boards(
    console: Console, failing: Sequence["BoardHealth"], *, limit: int | None, verbose: bool
) -> None:
    console.print()
    console.print(f"[bold yellow]Boards needing a look[/bold yellow] ({len(failing)})")
    shown = failing if limit is None else failing[:limit]
    for board in shown:
        times = board.consecutive_failures
        failed = "failed its last fetch" if times == 1 else f"failed {times:,} fetches in a row"
        error = error_cell(board.last_error, FULL_ERROR if verbose else 48)
        console.print(
            f"  [yellow]{truncate(board.label, 32)}[/yellow] ({board.source}) {failed}"
            + (f": {error}" if error else ", with no error recorded")
        )
    more(console, len(failing) - len(shown))
    breaker = sum(1 for board in failing if _skipped_by_breaker(board.last_error))
    if breaker == len(failing):
        advice = (
            "Stage's circuit breaker skipped these after their host kept failing. They retry "
            "on the next sync."
        )
    elif breaker:
        advice = (
            f"{breaker:,} of these were skipped by Stage's circuit breaker and retry on the next "
            "sync. The rest are registry rows to fix or switch off."
        )
    else:
        advice = "These are registry rows to fix or switch off."
    console.print(
        f"[dim]{advice}" + ("" if verbose else "  --verbose prints each error in full.") + "[/dim]"
    )


def _render_quiet_boards(
    console: Console,
    heading: str,
    boards: Sequence["BoardHealth"],
    advice: str,
    *,
    limit: int | None,
) -> None:
    console.print()
    console.print(f"[bold]{heading}[/bold] ({len(boards)})")
    names = sorted(board.label for board in boards)
    shown = names if limit is None else names[:limit]
    console.print(f"  {truncate(', '.join(shown), 400)}")
    more(console, len(names) - len(shown))
    console.print(f"[dim]{advice}[/dim]")


def _skipped_by_breaker(error: str) -> bool:
    from stage.http import skipped_by_breaker

    return skipped_by_breaker(error) or "circuit breaker is open" in error


def render_stats(
    console: Console,
    report: "StatsReport",
    now: datetime,
    *,
    limit: int | None = BOARD_PREVIEW,
) -> None:
    console.print(
        f"[bold]{report.total_jobs:,}[/bold] canonical "
        f"{'posting' if report.total_jobs == 1 else 'postings'}, {report.duplicates:,} linked as "
        f"{'a duplicate' if report.duplicates == 1 else 'duplicates'}, "
        f"[bold]{sum(report.quarantined.values()):,}[/bold] rejected, "
        f"{counted(report.tombstones, 'tombstone')}, "
        f"{counted(report.cached_urls, 'cached validator')}, "
        f"schema v{report.schema_version}"
    )
    console.print()

    if not report.runs:
        console.print("[dim]No sync runs recorded yet. Run stage sync.[/dim]")
    else:
        timing = console.width >= STATS_FULL_WIDTH
        headers = [
            *(("elapsed",) if timing else ()),
            "added",
            "closed",
            *(("rejected",) if console.width >= STATS_REJECTED_WIDTH else ()),
            *(("requests", "cache") if timing else ()),
        ]
        shown = table("when", "outcome", *headers, right=headers, title="Recent syncs")
        shown.title_justify = "left"
        for numbers in shown.columns[1:]:
            numbers.no_wrap = True
        for run in report.runs:
            requests = sum(stats.requests for stats in run.sources)
            cached = sum(stats.not_modified for stats in run.sources)
            elapsed = (run.finished_at - run.started_at).total_seconds()
            colour = {"success": "green", "partial": "yellow"}.get(run.outcome.value, "red")
            values = {
                "elapsed": f"{elapsed:.1f}s",
                "added": f"{sum(stats.added for stats in run.sources):,}",
                "closed": f"{sum(stats.closed for stats in run.sources):,}",
                "rejected": f"{sum(stats.quarantined for stats in run.sources):,}",
                "requests": f"{requests:,}",
                "cache": ratio(cached / requests if requests else None),
            }
            shown.add_row(
                ago(run.finished_at, now),
                f"[{colour}]{run.outcome.value}[/{colour}]",
                *(values[header] for header in headers),
            )
        console.print(shown)

    for column, counts in report.composition.items():
        if not counts:
            continue
        console.print()
        console.print(f"[bold]{column}[/bold]")
        total = sum(counts.values())
        listed = list(counts.items())
        shown_counts = listed if limit is None else listed[:limit]
        name_width = max(8, min(26, console.width - 18))
        for bucket, count in shown_counts:
            share = f"{count / total:.1%}" if total else "-"
            label = truncate(bucket, name_width - 2)
            console.print(f"  {label:{name_width}} {count:>7,}  [dim]{share}[/dim]")
        more(console, len(listed) - len(shown_counts))


def render_quarantine(
    console: Console,
    entries: Sequence[QuarantinedJob],
    *,
    total_matching: int,
    reason_counts: dict[str, int],
    filtered: bool = False,
) -> None:
    if not entries and (filtered or any(reason_counts.values())):
        console.print("[yellow]No rejected posting matches those filters.[/yellow]")
        return
    if not entries:
        console.print(
            "[yellow]No rejected postings yet.[/yellow] They appear here after "
            "[bold]stage sync[/bold]."
        )
        return

    narrow = console.width < NARROW_COLUMNS
    company_width = 14 if narrow else 16
    location_width = 0 if narrow else 14
    reason_width = 18 if narrow else max(22, min(49, console.width // 4))
    used = (0 if narrow else 5) + company_width + location_width + reason_width
    title_width = max(16, console.width - used - 2 * ((3 if narrow else 5) + 1))

    columns = [
        *(() if narrow else (Column("Seen", 5, crop=False),)),
        Column("Company", company_width),
        Column("Title", title_width),
        *(() if narrow else (Column("Location", location_width),)),
        Column("Rejected for", reason_width),
    ]

    def row(entry: QuarantinedJob) -> list[Cell]:
        matched = entry.reason.value
        if entry.matched_phrase:
            room = max(8, reason_width - len(matched) - 3)
            matched += f" ({cut_often(entry.matched_phrase, room)})"
        company = Cell(cut_often(entry.company, company_width))
        title = Cell(cut(entry.title_raw, title_width), "dim", entry.apply_url_raw)
        reason = Cell(cut_often(matched, reason_width), "yellow")
        if narrow:
            return [company, title, reason]
        seen = Cell(entry.first_seen.astimezone().strftime("%m-%d"))
        where = Cell(cut_often(place(entry.location_raw) or "-", location_width))
        return [seen, company, title, where, reason]

    grid(console, columns, map(row, entries))
    shown = len(entries)
    suffix = f" of {total_matching:,}" if total_matching > shown else ""
    console.print(f"\n[dim]{counted(shown, 'rejected posting')}{suffix}.[/dim]")
    if reason_counts:
        breakdown = ", ".join(f"{reason} {count:,}" for reason, count in reason_counts.items())
        scope = (
            "Matching the other filters, by reason" if filtered else "Every rejection, by reason"
        )
        console.print(f"[dim]{scope}: {breakdown}.[/dim]")


def render_contradictions(console: Console, report: "CoverageReport") -> None:
    if not report.contradictions:
        console.print("[dim]No review verdict contradicts the registry or has aged out.[/dim]")
        return
    shown = table(
        "Company", "Verdict", "Why it no longer holds", title="Review verdicts to re-derive"
    )
    for record, reason in report.contradictions:
        shown.add_row(
            sanitize(record.company), sanitize(record.disposition.value), sanitize(reason)
        )
    console.print(shown)


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
        f"[{_COVERAGE_STYLE[state]}]{counts[state]:,} {state.value}[/{_COVERAGE_STYLE[state]}]"
        for state in CoverageState
        if state in counts
    )
    console.print(
        f"[bold]{report.enabled:,}[/bold] enabled {'row' if report.enabled == 1 else 'rows'}: "
        f"{breakdown or 'none'}"
    )
    console.print(
        f"[dim]{counted(report.disabled, 'disabled row')}, not expected to produce.[/dim]"
    )
    for state, label, note in _COVERAGE_NOTES:
        _render_coverage_note(console, report, state, label, note, limit=limit)

    gaps = report.gaps
    if gaps:
        console.print()
        console.print(
            f"[bold]No internships open right now[/bold] ({len(gaps):,}) "
            "[dim]these boards answered, they just had nothing matching[/dim]"
        )
        shown = table("company", "board", "last success", right=("last success",))
        listed = gaps if limit is None else gaps[:limit]
        for row in listed:
            shown.add_row(
                truncate(row.company, 28), truncate(row.board, 38), ago(row.last_success_at, now)
            )
        console.print(shown)
        more(console, len(gaps) - len(listed))
        console.print("[dim]Expected most of the year. Internship postings are seasonal.[/dim]")

    if report.unregistered:
        console.print()
        console.print(
            f"[bold]Seen in a feed, absent from the registry[/bold] ({len(report.unregistered):,})"
        )
        shown = table("company", "postings", "sources", right=("postings",))
        unknown = report.unregistered if limit is None else report.unregistered[:limit]
        for entry in unknown:
            shown.add_row(
                truncate(entry.company, 34),
                f"{entry.postings:,}",
                ", ".join(entry.sources),
            )
        console.print(shown)
        more(console, len(report.unregistered) - len(unknown))
        console.print(
            '[dim]After researching an employer, record it with [bold]stage classify "Company" '
            '--status feed-only --note "why"[/bold]. To identify a career-board URL without '
            "fetching it, use [bold]stage discover --url URL[/bold].[/dim]"
        )

    if include_classified:
        console.print()
        if report.classifications:
            console.print(f"[bold]Reviewed feed employers[/bold] ({len(report.classifications)})")
            shown = table("company", "status", "checked", "note", right=("checked",))
            for classified in report.classifications:
                shown.add_row(
                    truncate(classified.company, 26),
                    classified.disposition.value,
                    classified.checked_on.date().isoformat(),
                    truncate(classified.note, 56),
                )
            console.print(shown)
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
    hidden = f" and {len(rows) - cap:,} more" if len(rows) > cap else ""
    console.print()
    console.print(f"[bold]{counted(len(rows), 'row')} {label}[/bold]: {note}")
    console.print(f"  [dim]{names}{hidden}[/dim]")
