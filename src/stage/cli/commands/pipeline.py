from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from stage.cli.options import (
    DatabaseOption,
    JsonOption,
    RegistryOption,
    _database,
    _lock_path,
    _print_failure,
    app,
    run_async,
)

if TYPE_CHECKING:
    pass


@app.command(help="Fetch enabled sources and store matching postings")
def sync(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Sync only these source adapters; repeatable"),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Skip these source adapters; repeatable"),
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

    from rich.console import Console

    from stage.cli.logfile import open_request_log
    from stage.cli.render import failure, render_sync
    from stage.cli.runlock import AnotherRunInProgressError, single_run
    from stage.cli.schedule_state import ScheduleStateWriter
    from stage.companies import RegistryError, load_companies
    from stage.domain import SyncOutcome
    from stage.services.sync import NoSourcesSelectedError
    from stage.services.sync import sync as sync_service
    from stage.storage import open_repository

    console = Console()
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
                return await render_sync(console, events, request_log=stream, progress=progress)

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


@app.command(help="Remove postings outside the retention window")
def purge(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview retention cleanup without removing postings"),
    ] = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.domain import PurgeResult
    from stage.services.maintenance import preview_purge, purge_expired
    from stage.storage import open_repository

    console = Console()

    async def run() -> PurgeResult:
        async with open_repository(_database(db)) as repository:
            now = datetime.now(UTC)
            if dry_run:
                return await preview_purge(repository, now=now)
            return await purge_expired(repository, now=now)

    result = run_async(run())
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


@app.command(help="Reclassify stored postings after a lexicon change")
def rescreen(db: DatabaseOption = None) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.services.maintenance import RESCREEN_LIMIT, RescreenResult
    from stage.services.maintenance import rescreen as rescreen_service
    from stage.storage import open_repository

    console = Console()

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
    console.print(f"Re-screened {result.examined} posting(s) — {', '.join(changes)}.")
    if result.released:
        console.print(
            "[dim]Restored postings keep their original title, link, and location. A later source "
            "refresh may fill metadata that quarantine does not retain.[/dim]"
        )


@app.command(help="Check one live board per platform against each parser")
def canary(
    as_json: JsonOption = False,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from rich.console import Console

    from stage.cli.render import render_canary
    from stage.cli.serialize import canary_to_json, emit
    from stage.companies import RegistryError, load_companies
    from stage.services.canary import CanaryReport
    from stage.services.canary import canary as run_canary
    from stage.storage import open_repository

    console = Console()

    async def run() -> CanaryReport:
        companies = load_companies(registry)
        async with open_repository(_database(db)) as repository:
            return await run_canary(repository, companies)

    try:
        report = run_async(run())
    except RegistryError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    if as_json:
        emit(canary_to_json(report))
    else:
        render_canary(console, report)
    if not report.passed:
        raise typer.Exit(code=1)
