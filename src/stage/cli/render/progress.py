import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TextIO

from rich.console import Console
from rich.padding import Padding
from rich.text import Text

from stage.cli.render import duration, error_cell, error_text, plain, sanitize, truncate
from stage.domain import (
    BucketPlan,
    CandidateSkipped,
    Company,
    CompanyDeferred,
    CompanyFailed,
    CompanyFinished,
    CompanyUnchanged,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    PlannedRequest,
    PlatformCandidate,
    PlatformProbed,
    ProbeVerdict,
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
    web_url,
)
from stage.domain.text import counted
from stage.domain.text import first_line as _first_line

CACHE_RATIO_MINIMUM = 20


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
    named = 0
    planned_by: dict[str, int] = {}
    done_by: dict[str, int] = {}

    def step(marker: str, width: int) -> str:
        counter = f"[dim]{done:>4}/{total}[/dim] " if total else ""
        tag = f"[dim]{truncate(under, named):<{named}}[/dim] " if named else ""
        return f"  {counter}{marker}{' ' * (width - len(Text.from_markup(marker)))} {tag}"

    async for event in events:
        if progress is not None:
            progress(event)
        under = getattr(event, "source", "")
        if isinstance(event, CompanyFinished | CompanyUnchanged | CompanyFailed | CompanyDeferred):
            done_by[under] = done_by.get(under, 0) + 1
        elif isinstance(event, SourceStarted):
            planned_by[under] = event.companies or 1
            total += planned_by[under]
        elif isinstance(event, SourceFinished) and under in planned_by:
            total += done_by.get(under, 0) - planned_by.pop(under)
        match event:
            case SyncStarted(sources=sources, companies=companies):
                named = min(15, max(map(len, sources))) if len(sources) > 1 else 0
                source_names = ", ".join(sources)
                console.print(
                    f"[bold]Syncing[/bold] {counted(companies, 'company board')} via {source_names}"
                )
            case UnroutableCompanies(companies=stranded, platforms=platforms):
                console.print(
                    f"\n[bold red]No adapter for {', '.join(platforms)}.[/bold red] "
                    f"{counted(len(stranded), 'enabled row')} will never be fetched: "
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
                    f"  {open_tag}bucket {bound.bucket}{close_tag}{shared}: "
                    f"{bound.planned} planned{detail}"
                )
            case SourceBlocked() as blocked:
                console.print(
                    f"\n[bold yellow]{blocked.source} blocked:[/bold yellow] bucket "
                    f"[bold]{blocked.bucket}[/bold] is throttled for another "
                    f"{duration(blocked.remaining_s)} "
                    f"(clears {blocked.blocked_until:%Y-%m-%d %H:%M UTC})"
                )
                reason = error_cell(blocked.reason, 70) or "no reason recorded"
                console.print(
                    f"  [dim]{counted(blocked.consecutive_failures, 'consecutive failure')}: "
                    f"{reason}. Not fetched this run. Clear it with "
                    f"[bold]stage sources --reset-rate-limit {blocked.bucket}[/bold].[/dim]"
                )
            case SourceStarted(source=source, companies=companies):
                size = counted(companies, "board") if companies else "feed"
                if named:
                    console.print(f"  [cyan]{source}[/cyan][dim]: {size}[/dim]")
                else:
                    console.print(f"\n[bold cyan]{source}[/bold cyan] - {size}")
            case SourceRotated() as rotated:
                phase = "cycle complete" if rotated.wrapped else f"resumes after {rotated.cursor}"
                console.print(
                    f"  [dim]rotating:[/dim] {counted(rotated.deferred, 'board')} deferred to "
                    f"a later run on bucket {rotated.bucket} ({phase})"
                )
            case SourceCapped() as capped:
                console.print(
                    f"  [yellow]capped:[/yellow] {capped.spent} spent by {capped.source} in 24h "
                    f"across its buckets, so this run may use "
                    f"{capped.allowance} of {capped.ceiling} per bucket"
                )
            case SourceFresh() as fresh:
                console.print(
                    f"  [dim]fresh:[/dim] {fresh.skipped} refreshed within "
                    f"{fresh.refresh_interval_h:g}h, {fresh.remaining} left"
                )
            case SourceResting() as resting:
                console.print(
                    f"  [dim]resting:[/dim] {counted(resting.skipped, 'board')} backing off after "
                    f"repeated failures, {resting.remaining} left"
                )
            case PlannedRequest(company=company, url=url, has_validator=cached):
                if not planned:
                    console.print(
                        "  [dim]cached = validator on file, a 304 is expected. "
                        "cold = full response expected.[/dim]"
                    )
                planned += 1
                validated += int(cached)
                cache_marker = "[cyan]cached[/cyan]" if cached else "[yellow]cold  [/yellow]"
                room = max(20, console.width - 34)
                console.print(f"  {cache_marker} {truncate(company, 22):<22} {truncate(url, room)}")
            case RequestLogged() as record:
                write_request_log(request_log, record)
            case CompanyFinished(
                company=company, fetched=fetched, elapsed_ms=elapsed, degraded=degraded
            ):
                status_marker = "[yellow]part[/yellow]" if degraded else "[green]ok[/green]"
                done += 1
                console.print(
                    f"{step(status_marker, 6)}{truncate(company, 24):<24} "
                    f"{counted(fetched, 'posting'):>14} {elapsed / 1000:>6.1f}s"
                )
                if degraded:
                    room = max(20, min(88, console.width - 10))
                    console.print(f"        [yellow]{truncate(degraded, room)}[/yellow]")
            case CompanyUnchanged(company=company, elapsed_ms=elapsed):
                done += 1
                console.print(
                    f"{step('[cyan]304[/cyan]', 6)}{truncate(company, 24):<24} "
                    f"{'unchanged':>14} {elapsed / 1000:>6.1f}s"
                )
            case CompanyFailed(source=source, company=company, error=error, elapsed_ms=elapsed):
                failures.append((source, company, error))
                done += 1
                head = step("[red]fail[/red]", 6)
                room = max(14, min(60, console.width - Text.from_markup(head).cell_len - 33))
                console.print(
                    f"{head}{truncate(company, 24):<24} {error_cell(error, room)} "
                    f"{elapsed / 1000:>6.1f}s"
                )
            case CompanyDeferred(company=company):
                done += 1
                console.print(
                    f"{step('[yellow]budget[/yellow]', 6)}{truncate(company, 24):<24} "
                    f"{'not attempted':>14}"
                )
            case SourceFailed(source=source, error=error):
                failures.append((source, source, error))
                console.print(f"  [red]fail[/red] {sanitize(source)}: {error_cell(error, 60)}")
            case SourceFinished() as finished:
                _render_source_summary(console, finished)
            case SyncFinished(dry_run=True) as finished:
                outcome = finished.outcome
                carry = "carries" if validated == 1 else "carry"
                console.print(
                    f"\n[bold]dry run:[/bold] {counted(planned, 'request')} planned, none sent. "
                    f"{validated:,} {carry} a validator, so a real run would likely transfer "
                    f"{counted(planned - validated, 'full response')}."
                )
                if outcome is not SyncOutcome.SUCCESS:
                    culprits = (
                        ", ".join(finished.failed_sources) or "a registry row no adapter routes"
                    )
                    console.print(
                        f"[red]Pre-flight failed[/red] for {sanitize(culprits)}, on a fault that "
                        "needs no network to see. Fix it before running the real sync."
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
                if finished.checked:
                    purge_note += f", {finished.checked} checked ({finished.checked_gone} gone)"
                quarantine_note = ""
                if finished.quarantined:
                    quarantine_note = f", {finished.quarantined:,} rejected"
                reason_note = (
                    f" [dim]({sanitize(finished.partial_reason)})[/dim]"
                    if finished.partial_reason
                    else ""
                )
                console.print(
                    f"\n[bold]{finished.outcome.value}[/bold]{reason_note}: "
                    f"{finished.added:,} added, {finished.updated:,} updated, "
                    f"{finished.closed:,} closed"
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
            console.print(f"  {source}/{sanitize(company)}")
            console.print(Padding(Text(_first_line(error_text(error))), (0, 0, 0, 6)))
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
    display_name: str | None = None,
    request_log: TextIO | None = None,
    verifying: bool = False,
    progress: Callable[[DiscoveryEvent], None] | None = None,
    known: Sequence[Company] = (),
    destination: str = "your board list",
) -> bool | DiscoveryFinished:
    from stage.services.discover import is_gone, registered_as, registered_board

    resolved = False
    outcome: DiscoveryFinished | None = None

    def show_entry(name: str, candidate: PlatformCandidate, note: str) -> None:
        console.print(f"\n[bold green]{sanitize(name)}[/bold green] -> {candidate.label}")
        if note:
            console.print(f"  [dim]{sanitize(note)}[/dim]")
        row = registered_board(known, candidate)
        if row is not None:
            console.print(
                plain(f"  Already registered as {row.name} ({registered_state(row)}).", "yellow")
            )
            return
        others = registered_as(known, name)
        if others:
            boards = ", ".join(board_text(other) for other in others)
            console.print(
                plain(
                    f"  {name} is already registered on {boards}, and this is another board.",
                    "yellow",
                )
            )

    async for event in events:
        if progress is not None:
            progress(event)
        match event:
            case DiscoveryStarted(companies=names, platforms=platforms, probes_planned=planned):
                if verifying:
                    console.print(
                        f"[bold]Checking[/bold] {counted(len(names), 'registry row')} across "
                        f"{counted(len(platforms), 'platform')}"
                    )
                    continue
                console.print(
                    f"[bold]Probing[/bold] {counted(len(names), 'name')} across "
                    f"{counted(len(platforms), 'platform')}: {counted(planned, 'probe')}, "
                    "plus one board-metadata request "
                    "per positive"
                )
                console.print(
                    "  [dim]Slug guessing resolves ~16%, a third of them falsely. "
                    "Prefer [bold]--url[/bold].[/dim]"
                )
            case RequestLogged() as record:
                write_request_log(request_log, record)
            case CandidateSkipped(company=company, slug=slug, reason=reason):
                console.print(
                    f"  [dim]skip[/dim]   {truncate(company, 22):<22} "
                    f"[dim]{sanitize(slug)}: {sanitize(reason)}[/dim]"
                )
            case UrlResolved(candidate=candidate, detail=detail):
                resolved = True
                note = detail
                if display_name is None:
                    note = (
                        f"{detail + '. ' if detail else ''}Set the display name with "
                        "--name. The slug is a board token, not a company name"
                    )
                show_entry(display_name or candidate.slug, candidate, note)
            case UrlUnrecognized(url=url, detail=detail):
                console.print(f"\n[yellow]Unrecognized[/yellow] {truncate(url, 70)}")
                console.print(f"  {sanitize(detail)}")
                _show_custom_skeleton(console, url, display_name, destination)
            case PlatformProbed(result=result) if result.verdict is not ProbeVerdict.MISS or (
                verifying and is_gone(result)
            ):
                style, label = (
                    ("red", "gone")
                    if result.verdict is ProbeVerdict.MISS
                    else _VERDICT_STYLE[result.verdict]
                )
                count = "" if result.job_count is None else f"{counted(result.job_count, 'job')}"
                console.print(
                    f"  [{style}]{label:<6}[/{style}] {truncate(result.company, 22):<22} "
                    f"{result.candidate.label:<34} {count}"
                )
                if result.detail:
                    console.print(f"         [dim]{error_cell(result.detail, 88)}[/dim]")
            case DiscoveryFinished() as finished:
                outcome = finished
                resolved = resolved or bool(finished.matched)
                _render_discovery_summary(console, finished, show_entry, verifying=verifying)
            case _:
                continue
    return outcome if verifying and outcome is not None else resolved


def registered_state(row: Company) -> str:
    return "polled" if row.enabled else "switched off"


def board_text(row: Company) -> str:
    from stage.companies import board_label

    return board_label(row)


def _show_custom_skeleton(
    console: Console, url: str, display_name: str | None, destination: str = "your board list"
) -> None:
    target = web_url(url)
    if target is None:
        return
    console.print(
        plain(
            "\nIf that page fills itself in from a JSON request, this is custom_json. Open "
            "DevTools, filter Fetch/XHR, reload the page, and copy the request that returns the "
            f"job list. Then add this to {destination} with the field names taken from that "
            "response, and set enabled to true:",
            "dim",
        ),
        soft_wrap=True,
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
    verifying: bool = False,
) -> None:
    for warning in event.ceiling_hit:
        console.print(
            f"\n[bold red]Per-host ceiling reached:[/bold red] {sanitize(warning)}. "
            "Probing stopped for that platform, so split the batch across runs."
        )
    for platform, count in event.non_json:
        console.print(
            f"\n[yellow]{platform}: no probe returned JSON "
            f"({counted(count, 'probe')} sent).[/yellow] "
            "Open one URL by hand before trusting a miss here."
        )
    console.print(
        f"\n[bold]{len(event.matched)} matched[/bold], {len(event.unverified)} needing a "
        f"manual check, {len(event.rejected)} rejected, {event.missed} missed, "
        f"{counted(event.errors, 'error')} in {counted(event.requests, 'request')} "
        f"({event.elapsed_ms / 1000:.1f}s)"
    )
    if verifying:
        if event.unverified:
            console.print(
                f"[dim]{counted(len(event.unverified), 'board')} answered on a platform that "
                "publishes no board name, so --apply leaves them as they are.[/dim]"
            )
        return
    for result in event.matched:
        show_entry(result.company, result.candidate, f'board name "{result.board_name}" confirmed')
    if event.unverified:
        console.print(
            "\n[yellow]Verify before adding.[/yellow] These expose no board name, "
            "and a 200 with jobs is not evidence of the right company."
        )
        for result in event.unverified:
            console.print(
                plain(f"  {result.company} -> {result.candidate.label} {result.url}"),
                soft_wrap=True,
            )
    if not event.matched and not event.unverified:
        console.print(
            "\n[dim]Nothing resolved. Re-run with "
            "[bold]stage discover --url <careers-page>[/bold], the only path that "
            "resolves Workday.[/dim]"
        )


def _render_source_summary(console: Console, event: SourceFinished) -> None:
    if not (event.added or event.updated or event.closed or event.quarantined or event.requests):
        return
    counts = [
        f"{event.added:,} added",
        f"{event.updated:,} updated",
        f"{event.closed:,} closed",
    ]
    if event.quarantined:
        counts.append(f"{event.quarantined:,} rejected")
    if event.tightenings:
        counts.append(f"[yellow]{counted(event.tightenings, 'rate tightening')}[/yellow]")
    console.print(
        f"  [bold cyan]{event.source}[/bold cyan] done in {event.elapsed_ms / 1000:.1f}s: "
        f"{', '.join(counts)}"
    )
    timings = []
    if event.requests:
        timings.append(f"{event.not_modified:,}/{event.requests:,} cached")
        timings.append(f"p50 {event.latency_p50_ms:.0f}ms, p95 {event.latency_p95_ms:.0f}ms")
    if event.fetch_ms or event.normalize_ms or event.write_ms:
        timings.append(
            f"fetch {event.fetch_ms / 1000:.1f}s, process {event.normalize_ms / 1000:.1f}s, "
            f"write {event.write_ms / 1000:.1f}s"
        )
    if event.retries:
        timings.append(f"{event.retries} retried")
    if timings:
        console.print(f"    [dim]{', '.join(timings)}[/dim]")


def write_request_log(stream: TextIO | None, record: RequestLogged) -> None:
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
