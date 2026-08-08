import json
import re
import unicodedata
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, TextIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from stage.services.canary import CanaryReport
    from stage.services.health import DoctorReport, SourceHealth, StatsReport

from stage.domain import (
    BucketPlan,
    CandidateSkipped,
    CompanyFailed,
    CompanyFinished,
    CompanyUnchanged,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    Job,
    PlannedRequest,
    PlatformProbed,
    ProbeVerdict,
    QuarantinedJob,
    RateState,
    RequestLogged,
    SourceBlocked,
    SourceFinished,
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
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize(value: str) -> str:
    return _CONTROL.sub("", value)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"unserializable value of type {type(value).__name__}")


def _json_safe(value: object) -> object:
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def _dump(payload: object) -> str:
    return json.dumps(_json_safe(payload), indent=2, ensure_ascii=False, default=_json_default)


def first_line(value: str) -> str:
    lines = [line.strip() for line in sanitize(value).splitlines() if line.strip()]
    return lines[0] if lines else ""


def truncate(value: str, width: int) -> str:
    clean = sanitize(value)
    graphemes: list[str] = []
    for char in clean:
        if unicodedata.combining(char) and graphemes:
            graphemes[-1] += char
        else:
            graphemes.append(char)
    if len(graphemes) <= width:
        return clean
    return "".join(graphemes[: max(width - 1, 0)]) + "…"


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
            truncate(first_line(state.reason), 40) or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(
        "[dim]Tightening decays on each clean run. "
        "[bold]stage sources --clear <bucket>[/bold] drops a block now.[/dim]"
    )


def jobs_to_json(jobs: Sequence[Job]) -> str:
    from dataclasses import asdict

    return _dump([asdict(job) for job in jobs])


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
        title = Text(truncate(job.title_raw, title_width), style=style)
        if job.apply_url_raw:
            title.stylize(f"link {job.apply_url_raw}")
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


def quarantine_to_json(entries: Sequence[QuarantinedJob]) -> str:
    from dataclasses import asdict

    return _dump([asdict(entry) for entry in entries])


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


def render_board_health(
    console: Console, sources: Sequence["SourceHealth"], now: datetime
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
            truncate(first_line(board.last_error), 40) or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(
        "[dim]A board with no row has not been reached by rotation yet, and is not "
        "listed here.[/dim]"
    )


def health_to_json(report: "DoctorReport") -> str:
    from dataclasses import asdict

    payload = {
        "schema_version": report.schema_version,
        "last_sync_at": report.last_sync_at,
        "never_synced": report.never_synced,
        "healthy": report.is_healthy,
        "warnings": report.warnings,
        "integrity": [asdict(finding) for finding in report.integrity],
        "blocks": [asdict(state) for state in report.blocks],
        "sources": [
            {
                **asdict(source),
                "cache_hit_ratio": source.cache_hit_ratio,
                "success_rate": source.success_rate,
            }
            for source in report.sources
        ],
    }
    return _dump(payload)


def stats_to_json(report: "StatsReport") -> str:
    from dataclasses import asdict

    payload = {
        "schema_version": report.schema_version,
        "total_jobs": report.total_jobs,
        "duplicates": report.duplicates,
        "tombstones": report.tombstones,
        "cached_urls": report.cached_urls,
        "quarantined": report.quarantined,
        "composition": report.composition,
        "runs": [asdict(run) for run in report.runs],
    }
    return _dump(payload)


def canary_to_json(report: "CanaryReport") -> str:
    from dataclasses import asdict

    payload = {
        "passed": report.passed,
        "skipped_platforms": list(report.skipped_platforms),
        "probes": [
            {**asdict(probe), "failure": probe.is_failure, "empty": probe.is_empty}
            for probe in report.probes
        ],
    }
    return _dump(payload)


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
            truncate(first_line(note), 44) or "[dim]—[/dim]",
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
                f"{truncate(first_line(board.last_error), 48) or 'no error recorded'}"
            )
        if len(failing) > 10:
            console.print(f"  [dim]… and {len(failing) - 10} more[/dim]")
        console.print("[dim]These are registry rows to fix or switch off.[/dim]")

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
        title = Text(truncate(entry.title_raw, title_width), style="dim")
        if entry.apply_url_raw:
            title.stylize(f"link {entry.apply_url_raw}")
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
                    f"{truncate(', '.join(sanitize(name) for name in stranded), 70)}"
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
                reason = truncate(first_line(blocked.reason), 70) if blocked.reason else "unknown"
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
                console.print(
                    f"  {cache_marker} {truncate(company, 22):<22} {truncate(url, room)}"
                )
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
                    console.print(f"         [yellow]{truncate(sanitize(degraded), 88)}[/yellow]")
            case CompanyUnchanged(company=company, elapsed_ms=elapsed):
                console.print(
                    f"  [cyan]304[/cyan]  {sanitize(company):<28} "
                    f"{'unchanged':>14}  {elapsed:>7.0f}ms"
                )
            case CompanyFailed(source=source, company=company, error=error, elapsed_ms=elapsed):
                failures.append((source, company, error))
                console.print(
                    f"  [red]fail[/red] {sanitize(company):<28} "
                    f"{truncate(first_line(error), 60)}  {elapsed:>7.0f}ms"
                )
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
                purge_note = (
                    f", {finished.purged} purged" if finished.purged else ""
                )
                quarantine_note = ""
                if finished.quarantined:
                    quarantine_note = (
                        f", [yellow]{finished.quarantined} quarantined[/yellow]"
                    )
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

    def show_entry(name: str, candidate: object, note: str) -> None:
        from stage.domain import PlatformCandidate

        assert isinstance(candidate, PlatformCandidate)
        console.print(f"\n[bold green]{sanitize(name)}[/bold green] -> {candidate.label}")
        if note:
            console.print(f"  [dim]{note}[/dim]")
        console.print("\n[dim]Paste into data/companies.yaml:[/dim]")
        entry = registry_entry_yaml(to_company(name, candidate, verified_on=verified_on))
        for line in entry.splitlines():
            console.print(f"  {line}", highlight=False)

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
                    f"  [dim]skip[/dim]   {truncate(sanitize(company), 22):<22} "
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
                console.print(f"\n[yellow]Unrecognized[/yellow] {truncate(sanitize(url), 70)}")
                console.print(f"  {sanitize(detail)}")
            case PlatformProbed(result=result) if result.verdict is not ProbeVerdict.MISS:
                style, label = _VERDICT_STYLE[result.verdict]
                count = "" if result.job_count is None else f"{result.job_count:>5} job(s)"
                console.print(
                    f"  [{style}]{label:<6}[/{style}] {truncate(sanitize(result.company), 22):<22} "
                    f"{result.candidate.label:<34} {count}"
                )
                if result.detail:
                    console.print(f"         [dim]{truncate(sanitize(result.detail), 88)}[/dim]")
            case DiscoveryFinished() as finished:
                outcome = finished
                resolved = resolved or bool(finished.matched)
                _render_discovery_summary(console, finished, show_entry, quiet=collect)
            case _:
                continue
    return outcome if collect and outcome is not None else resolved


def _render_discovery_summary(
    console: Console,
    event: DiscoveryFinished,
    show_entry: Callable[[str, object, str], None],
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
            console.print(f"  {sanitize(result.company)} -> {result.candidate.label} {result.url}")
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
