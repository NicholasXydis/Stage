from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from stage.cli.options import (
    STOPPED,
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    RegistryOption,
    _count,
    _database,
    _lock_path,
    _print_error,
    _print_failure,
    _refusing,
    _validated_source,
    _warn_unread_boards,
    _with_db,
    app,
    run_async,
)

if TYPE_CHECKING:
    from stage.domain import PurgeResult

MAINTENANCE_WAIT_SECONDS = 900.0
OFFLINE_PATIENCE_SECONDS = 120.0
OFFLINE_MESSAGE = (
    "No internet connection, so nothing was synced and no board was marked as failing."
)
SYNC_STOPPED = "Sync stopped. What it saved is kept, and stage sync picks up the rest."


@app.command(
    help="Fetch enabled sources and store matching postings", rich_help_panel="Keeping current"
)
def sync(
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source", metavar="SOURCE", help="Sync only these source adapters (repeatable)"
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", metavar="NAME", help="Skip these source adapters (repeatable)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the sync plan without fetching or changing the database",
        ),
    ] = False,
    force_refresh: Annotated[
        bool,
        typer.Option(
            "--force-refresh",
            help="Re-fetch every board even if it was refreshed inside its window",
        ),
    ] = False,
    request_log: Annotated[
        Path | None,
        typer.Option(
            "--request-log",
            metavar="FILE",
            help="Write outbound HTTP requests to this JSON Lines file",
        ),
    ] = None,
    scheduled_progress: Annotated[
        Path | None,
        typer.Option("--scheduled-progress", hidden=True),
    ] = None,
    progress_file: Annotated[
        Path | None,
        typer.Option("--progress-file", hidden=True),
    ] = None,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from contextlib import ExitStack

    from stage.cli.logfile import open_request_log
    from stage.cli.render import plain, terminal
    from stage.cli.render.progress import render_sync
    from stage.cli.schedule_state import ScheduleStateWriter
    from stage.companies import RegistryError, load_boards
    from stage.domain import SyncOutcome
    from stage.domain.text import plain_error
    from stage.paths import registry_override
    from stage.runlock import AnotherRunInProgressError, single_run
    from stage.services.maintenance import rescreen_after_upgrade, upgrade_note
    from stage.services.sync import NoSourcesSelectedError
    from stage.services.sync import sync as sync_service
    from stage.storage import open_repository

    console = terminal()
    scheduled_state = (
        ScheduleStateWriter.open(scheduled_progress, "sync")
        if scheduled_progress is not None
        else None
    )
    progress = scheduled_state.sync_event if scheduled_state is not None else None
    tracked = (
        ScheduleStateWriter.start("sync", Path(), destination=progress_file)
        if progress_file is not None and scheduled_state is None and not dry_run
        else None
    )
    if tracked is not None:
        progress = tracked.sync_event

    refusal = "Pass either --source or --exclude, not both." if source and exclude else ""
    try:
        for name in (*(source or ()), *(exclude or ())):
            _validated_source(name)
    except InvalidOptionError as exc:
        refusal = refusal or str(exc)
    if refusal:
        _print_error(plain(refusal, "red"))
        if tracked is not None:
            tracked.failed(refusal)
        raise typer.Exit(code=2)

    try:
        companies, personal = load_boards(registry)
    except RegistryError as exc:
        _print_failure(exc)
        if tracked is not None:
            tracked.failed(str(exc))
        raise typer.Exit(code=2) from exc
    _warn_unread_boards(personal)

    if not dry_run:
        from stage.http import wait_until_online

        patience = OFFLINE_PATIENCE_SECONDS if scheduled_state is not None else 0.0
        if not wait_until_online(patience):
            if scheduled_state is not None:
                scheduled_state.offline(OFFLINE_MESSAGE)
                console.print(OFFLINE_MESSAGE)
                raise typer.Exit(code=0)
            if tracked is not None:
                tracked.offline(OFFLINE_MESSAGE)
            _print_error(f"[yellow]{OFFLINE_MESSAGE}[/yellow]")
            raise typer.Exit(code=1)

    async def run() -> SyncOutcome:
        with ExitStack() as stack:
            stream = (
                stack.enter_context(open_request_log(request_log))
                if request_log is not None
                else None
            )
            async with open_repository(_database(db)) as repository:
                if not dry_run:
                    checked = await rescreen_after_upgrade(repository)
                    if checked is not None:
                        console.print(plain(upgrade_note(checked)), soft_wrap=True)
                events = sync_service(
                    repository,
                    companies,
                    sources=source or None,
                    excluded=exclude or None,
                    dry_run=dry_run,
                    force_refresh=force_refresh,
                    close_orphans=not personal.problem and registry_override(registry) is None,
                )
                outcome = await render_sync(console, events, request_log=stream, progress=progress)
                if scheduled_progress is not None and not dry_run:
                    await _announce_new_postings(repository, console)
                return outcome

    try:
        if dry_run:
            outcome = run_async(run())
        else:
            with single_run("sync", _lock_path(db)):
                if tracked is not None:
                    tracked.started("syncing")
                outcome = run_async(run(), stopped=SYNC_STOPPED)
    except AnotherRunInProgressError as exc:
        if scheduled_state is not None:
            scheduled_state.blocked(str(exc))
        if tracked is not None:
            tracked.blocked(str(exc))
        _print_failure(exc, soft_wrap=True)
        raise typer.Exit(code=2) from exc
    except (RegistryError, NoSourcesSelectedError) as exc:
        if tracked is not None:
            tracked.failed(str(exc))
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    except typer.Exit as stop:
        if tracked is not None:
            tracked.failed("The sync was stopped before it finished.", exit_code=stop.exit_code)
        raise
    except Exception as exc:
        if tracked is not None:
            tracked.failed(f"The sync stopped on an unexpected error ({plain_error(str(exc))}).")
        raise

    code = 0 if outcome is SyncOutcome.SUCCESS else 1
    if tracked is not None:
        tracked.complete(sync_exit_code=code, doctor_exit_code=None, exit_code=code)
    raise typer.Exit(code=code)


@contextmanager
def _maintenance_lock(command: str, db: Path | None, console: Any) -> Iterator[None]:
    from stage.cli.render import plain
    from stage.runlock import AnotherRunInProgressError, single_run

    def waiting(holder: str) -> None:
        console.print(plain(f"Waiting for {holder} to finish before the {command} starts."))

    try:
        with (
            _refusing(AnotherRunInProgressError, soft_wrap=True),
            single_run(
                command,
                _lock_path(db),
                wait_s=MAINTENANCE_WAIT_SECONDS,
                busy=f"run stage {command} again when it finishes",
                waiting=waiting,
            ),
        ):
            yield
    except KeyboardInterrupt:
        _print_error(f"[yellow]{STOPPED}[/yellow]")
        raise typer.Exit(code=130) from None


async def _announce_new_postings(repository: Any, console: Any) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from stage.cli import notify
    from stage.cli.render import plain
    from stage.domain import JobFilters
    from stage.normalize.location import display_location
    from stage.services.query import list_jobs

    webhook = notify.read()
    if not webhook:
        return
    since = notify.announced_until() or await repository.previous_sync_at()
    if since is None:
        return
    now = datetime.now(UTC)
    if since > now:
        runs = await repository.run_history(1)
        if runs:
            since = min(since, runs[0].started_at)
    filters = replace(JobFilters(limit=None), first_seen_after=since)
    listing = await list_jobs(repository, filters, window_days=None)
    current = [job for job in listing.jobs if job.first_seen <= now]
    if not current:
        return
    first_reads = {
        board: read for board, read in (await repository.first_reads()).items() if read <= now
    }
    copies = await repository.earliest_copies([job.id for job in current])
    fresh = [
        job
        for job in current
        if not notify.is_backlog(job, first_reads)
        and not (job.id in copies and copies[job.id] < since)
    ]
    newest = max(job.first_seen for job in current) + timedelta(microseconds=1)
    if not fresh:
        notify.mark_announced(newest)
        return
    hidden = listing.total_matching - len(listing.jobs)
    chosen = sorted(fresh, key=notify.rank)
    postings = [
        notify.Posting(
            company=job.company,
            title=job.title_raw,
            location=display_location(job.location_raw),
            url=job.apply_url_raw,
        )
        for job in chosen
    ]
    try:
        for payload in notify.messages(postings, len(fresh) + hidden):
            notify.post(webhook, payload)
    except notify.NotifyError as exc:
        console.print(
            plain(f"New postings did not reach Discord. {exc} The next sync tries again.", "yellow")
        )
        return
    notify.mark_announced(newest)


@app.command(
    help="Remove expired postings and prune old diagnostic files",
    rich_help_panel="Registry and maintenance",
)
def purge(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview retention cleanup without removing postings"),
    ] = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import terminal
    from stage.domain import PurgeResult
    from stage.domain.text import counted
    from stage.services.maintenance import preview_purge, purge_expired
    from stage.storage import open_repository

    console = terminal()

    async def run() -> PurgeResult:
        async with open_repository(_database(db)) as repository:
            now = datetime.now(UTC)
            if dry_run:
                return await preview_purge(repository, now=now)
            return await purge_expired(repository, now=now)

    from stage.cli.housekeeping import tidy

    if dry_run:
        result = run_async(run())
        swept = tidy(dry_run=dry_run)
    else:
        with _maintenance_lock("purge", db, console):
            result = run_async(run())
            swept = tidy(dry_run=dry_run)
    if swept.captures_removed or swept.journal_rotated:
        parts = []
        if swept.captures_removed:
            verb = "Would remove" if dry_run else "Removed"
            parts.append(f"{verb} {counted(swept.captures_removed, 'old captured payload')}")
        if swept.journal_rotated:
            parts.append("rotated the probe journal" if not dry_run else "would rotate the journal")
        console.print(f"[dim]{', '.join(parts)}.[/dim]")

    if result.tracker_busy:
        console.print(
            "[yellow]Your tracker was busy, so postings you track may not have moved to the "
            "copy that stays. Run stage tracker to check them.[/yellow]"
        )
    for line in _purge_lines(result, dry_run):
        console.print(line)
    if result.purged and not dry_run:
        console.print(
            "[dim]Stage remembers when each purged posting was first seen, so one that is "
            "still open does not come back as new.[/dim]"
        )


def _purge_lines(result: "PurgeResult", dry_run: bool) -> list[str]:
    from stage.domain import QUARANTINE_RETENTION_DAYS
    from stage.domain.text import counted

    lines: list[str] = []
    if result.purged:
        verb = "Would purge" if dry_run else "Purged"
        lines.append(f"{verb} {counted(result.purged, 'posting')} past the retention window.")
    if result.promoted:
        lines.append(f"Promoted {counted(result.promoted, 'orphaned duplicate')} back into view.")
    if result.forgotten:
        verb = "Would forget" if dry_run else "Forgot"
        lines.append(
            f"{verb} {counted(result.forgotten, 'rejected posting')} that no board has listed "
            f"for {QUARANTINE_RETENTION_DAYS} days."
        )
    if not lines:
        lines.append(
            "[dim]No posting is past the retention window, and every rejected posting was "
            f"listed in the last {QUARANTINE_RETENTION_DAYS} days.[/dim]"
        )
    if dry_run:
        lines.append("[dim]This was a dry run, so nothing was removed.[/dim]")
    return lines


@app.command(
    help="Reclassify stored postings after a lexicon change",
    rich_help_panel="Registry and maintenance",
)
def rescreen(db: DatabaseOption = None) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import terminal
    from stage.domain.text import counted
    from stage.services.maintenance import RESCREEN_LIMIT
    from stage.services.maintenance import rescreen as rescreen_service

    console = terminal()
    console.print("[dim]Rescreening stored and rejected postings. This takes a few seconds.[/dim]")

    with _maintenance_lock("rescreen", db, console):
        result = _with_db(
            db, lambda repository: rescreen_service(repository, now=datetime.now(UTC))
        )
    if not result.examined and not result.released:
        console.print("[dim]Nothing stored yet. Run stage sync.[/dim]")
        return
    if result.skipped:
        console.print(
            f"[yellow]{counted(result.skipped, 'posting')} not examined.[/yellow] This pass reads "
            f"at most {RESCREEN_LIMIT:,} rows. Run stage purge, then stage rescreen again."
        )
    if not result.changed:
        console.print(f"Re-screened {counted(result.examined, 'posting')}. None changed.")
        return
    changes: list[str] = []
    if result.updated:
        changes.append(f"[yellow]{counted(result.updated, 'classification')} updated[/yellow]")
    if result.quarantined:
        changes.append(f"[yellow]{result.quarantined:,} newly rejected[/yellow]")
    if result.released:
        changes.append(f"[green]{result.released:,} no longer rejected[/green]")
    if result.relabelled:
        changes.append(
            f"[dim]{counted(result.relabelled, 'rejection')} given a sharper reason[/dim]"
        )
    if result.relocated:
        changes.append(f"[dim]{counted(result.relocated, 'rejected location')} re-read[/dim]")
    if result.relinked:
        changes.append(f"[dim]{counted(result.relinked, 'duplicate link')} corrected[/dim]")
    console.print(f"Re-screened {counted(result.examined, 'posting')}: {', '.join(changes)}.")
    if result.released:
        console.print(
            "[dim]Postings no longer rejected keep their title, link and location. Their next "
            "sync fills in the details Stage does not keep for rejected postings.[/dim]"
        )


@app.command(help="Check one live board per platform against each parser", rich_help_panel="Health")
def canary(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print each probe note in full"),
    ] = False,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            metavar="SECONDS",
            click_type=_count(1, 3600),
            help="Give up after this long, since every probe is a live network request",
        ),
    ] = 300,
    as_json: JsonOption = False,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.render.health import render_canary
    from stage.cli.serialize import canary_to_json, emit
    from stage.companies import RegistryError, load_companies
    from stage.domain.text import counted
    from stage.services.canary import canary as run_canary

    console = terminal()

    with _refusing(RegistryError):
        companies = load_companies(registry)
    report = _with_db(db, lambda repository: run_canary(repository, companies, timeout=timeout))

    if as_json:
        emit(canary_to_json(report))
    else:
        render_canary(console, report, verbose=verbose)
    if report.timed_out:
        _print_error(
            f"[red]Gave up after {timeout}s[/red] with {counted(len(report.probes), 'board')} "
            "checked. Boards can be slow or unreachable, so raise the limit with "
            "[bold]--timeout[/bold]."
        )
    if not report.passed:
        raise typer.Exit(code=1)
