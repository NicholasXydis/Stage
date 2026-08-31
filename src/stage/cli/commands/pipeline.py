from pathlib import Path
from typing import Annotated, Any

import typer

from stage.cli.options import (
    DatabaseOption,
    JsonOption,
    RegistryOption,
    _count,
    _database,
    _lock_path,
    _print_failure,
    app,
    run_async,
)


@app.command(
    help="Fetch enabled sources and store matching postings", rich_help_panel="Keeping current"
)
def sync(
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source", metavar="SOURCE", help="Sync only these source adapters; repeatable"
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", metavar="NAME", help="Skip these source adapters; repeatable"),
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
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from contextlib import ExitStack

    from stage.cli.logfile import open_request_log
    from stage.cli.render import failure, render_sync, terminal
    from stage.cli.runlock import AnotherRunInProgressError, single_run
    from stage.cli.schedule_state import ScheduleStateWriter
    from stage.companies import RegistryError, load_companies
    from stage.domain import SyncOutcome
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

    if source and exclude:
        console.print("[red]Pass either --source or --exclude, not both.[/red]")
        raise typer.Exit(code=2)

    try:
        companies = load_companies(registry)
    except RegistryError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    async def run() -> SyncOutcome:
        with ExitStack() as stack:
            stream = (
                stack.enter_context(open_request_log(request_log))
                if request_log is not None
                else None
            )
            async with open_repository(_database(db)) as repository:
                events = sync_service(
                    repository,
                    companies,
                    sources=source or None,
                    excluded=exclude or None,
                    dry_run=dry_run,
                    force_refresh=force_refresh,
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
                outcome = run_async(run())
    except AnotherRunInProgressError as exc:
        if scheduled_state is not None:
            scheduled_state.blocked(str(exc))
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    except (RegistryError, NoSourcesSelectedError) as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    raise typer.Exit(code=0 if outcome is SyncOutcome.SUCCESS else 1)


async def _announce_new_postings(repository: Any, console: Any) -> None:
    from dataclasses import replace

    from stage.cli import notify
    from stage.cli.render import plain
    from stage.domain import JobFilters
    from stage.normalize.location import display_location
    from stage.services.query import list_jobs

    webhook = notify.read()
    if not webhook:
        return
    since = await repository.previous_sync_at()
    if since is None:
        return
    filters = replace(JobFilters(limit=notify.MAX_LISTED), first_seen_after=since)
    listing = await list_jobs(repository, filters, window_days=None)
    if not listing.jobs:
        return
    postings = [
        notify.Posting(
            company=job.company,
            title=job.title_raw,
            location=display_location(job.location_raw),
            url=job.apply_url_raw,
        )
        for job in listing.jobs
    ]
    try:
        notify.post(webhook, notify.compose(postings, listing.total_matching))
    except notify.NotifyError as exc:
        console.print(plain(f"Could not post to Discord: {exc}", style="yellow"))


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
    from stage.services.maintenance import preview_purge, purge_expired
    from stage.storage import open_repository

    console = terminal()

    async def run() -> PurgeResult:
        async with open_repository(_database(db)) as repository:
            now = datetime.now(UTC)
            if dry_run:
                return await preview_purge(repository, now=now)
            return await purge_expired(repository, now=now)

    result = run_async(run())

    from stage.cli.housekeeping import tidy

    swept = tidy(dry_run=dry_run)
    if swept.captures_removed or swept.journal_rotated:
        parts = []
        if swept.captures_removed:
            verb = "Would remove" if dry_run else "Removed"
            parts.append(f"{verb} {swept.captures_removed} old captured payload(s)")
        if swept.journal_rotated:
            parts.append("rotated the probe journal" if not dry_run else "would rotate the journal")
        console.print(f"[dim]{', '.join(parts)}.[/dim]")

    if dry_run:
        if result.purged:
            console.print(
                f"Would purge {result.purged} posting(s) and create or refresh "
                f"{result.tombstoned} tombstone(s). No postings removed."
            )
        else:
            console.print("[dim]Nothing would be removed. No postings removed.[/dim]")
        return
    if not result.purged:
        if result.promoted:
            console.print(
                f"Nothing outside the retention window, and {result.promoted} orphaned "
                "duplicate(s) were promoted back into view."
            )
            return
        console.print("[dim]Nothing outside the retention window.[/dim]")
        return
    promoted = f", {result.promoted} duplicate(s) promoted" if result.promoted else ""
    console.print(
        f"Purged {result.purged} posting(s), {result.tombstoned} tombstone(s) kept{promoted}."
    )
    console.print(
        "[dim]Tombstones keep the original first_seen so a still-open posting is not "
        "re-ingested as new.[/dim]"
    )


@app.command(
    help="Reclassify stored postings after a lexicon change",
    rich_help_panel="Registry and maintenance",
)
def rescreen(db: DatabaseOption = None) -> None:
    from datetime import UTC, datetime

    from stage.cli.render import terminal
    from stage.services.maintenance import RESCREEN_LIMIT, RescreenResult
    from stage.services.maintenance import rescreen as rescreen_service
    from stage.storage import open_repository

    console = terminal()

    async def run() -> RescreenResult:
        async with open_repository(_database(db)) as repository:
            return await rescreen_service(repository, now=datetime.now(UTC))

    result = run_async(run())
    if not result.examined and not result.released:
        console.print("[dim]Nothing stored yet — run stage sync.[/dim]")
        return
    if result.skipped:
        console.print(
            f"[yellow]{result.skipped} posting(s) were not examined[/yellow] — this pass reads "
            f"at most {RESCREEN_LIMIT} rows. Run stage purge, then rescreen again."
        )
    if not result.changed:
        console.print(
            f"Re-screened {result.examined} posting(s); the lexicon agrees with every one."
        )
        return
    changes: list[str] = []
    if result.updated:
        changes.append(f"[yellow]{result.updated} classification(s) updated[/yellow]")
    if result.quarantined:
        changes.append(f"[yellow]{result.quarantined} moved to quarantine[/yellow]")
    if result.released:
        changes.append(f"[green]{result.released} restored from quarantine[/green]")
    if result.relabelled:
        changes.append(f"[dim]{result.relabelled} rejection(s) given a sharper reason[/dim]")
    if result.relocated:
        changes.append(f"[dim]{result.relocated} quarantine location(s) re-read[/dim]")
    console.print(f"Re-screened {result.examined} posting(s) — {', '.join(changes)}.")
    if result.released:
        console.print(
            "[dim]Restored postings keep their original title, link, and location. A later source "
            "refresh may fill metadata that quarantine does not retain.[/dim]"
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
            help="Give up after this long; every probe is a live network request",
        ),
    ] = 300,
    as_json: JsonOption = False,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import render_canary, terminal
    from stage.cli.serialize import canary_to_json, emit
    from stage.companies import RegistryError, load_companies
    from stage.services.canary import CanaryReport
    from stage.services.canary import canary as run_canary
    from stage.storage import open_repository

    console = terminal()

    async def run() -> CanaryReport:
        import asyncio

        companies = load_companies(registry)
        async with open_repository(_database(db)) as repository:
            return await asyncio.wait_for(run_canary(repository, companies), timeout)

    try:
        report = run_async(run())
    except RegistryError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    except TimeoutError:
        console.print(
            f"[red]Gave up after {timeout}s.[/red] Boards can be slow or unreachable; "
            "raise the ceiling with [bold]--timeout[/bold]."
        )
        raise typer.Exit(code=1) from None

    if as_json:
        emit(canary_to_json(report))
    else:
        render_canary(console, report, verbose=verbose)
    if not report.passed:
        raise typer.Exit(code=1)
