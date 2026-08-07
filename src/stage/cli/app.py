import asyncio
from collections.abc import AsyncIterator, Coroutine
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class InvalidOptionError(Exception):
    pass


def _parse_enum[E: StrEnum](value: str | None, enum: type[E], flag: str) -> E | None:
    if value is None:
        return None
    return _require_enum(value, enum, flag)


def _require_enum[E: StrEnum](value: str, enum: type[E], flag: str) -> E:
    try:
        return enum(value)
    except ValueError as exc:
        options = ", ".join(enum.__members__.values())
        raise InvalidOptionError(f"{flag} must be one of: {options}") from exc


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Aggregates CS internship postings into a local SQLite database.",
)

RegistryOption = Annotated[
    Path | None,
    typer.Option("--registry", help="Registry path"),
]
DatabaseOption = Annotated[
    Path | None,
    typer.Option("--db", help="Database path"),
]


def _database(explicit: Path | None) -> Path:
    from stage.paths import database_path

    return explicit.expanduser() if explicit else database_path()


@app.command()
def sync(
    source: Annotated[
        str | None, typer.Option("--source", help="One source only")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan, do not fetch"),
    ] = False,
    request_log: Annotated[
        Path | None,
        typer.Option(
            "--request-log",
            help="JSONL request log",
        ),
    ] = None,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from contextlib import ExitStack

    from rich.console import Console

    from stage.cli.logfile import open_request_log
    from stage.cli.render import render_sync
    from stage.companies import RegistryError, load_companies
    from stage.domain import SyncOutcome
    from stage.services.sync import NoSourcesSelectedError
    from stage.services.sync import sync as sync_service
    from stage.storage import open_repository

    console = Console()

    async def run() -> SyncOutcome:
        companies = load_companies(registry)
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
                    sources=[source] if source is not None else None,
                    dry_run=dry_run,
                )
                return await render_sync(console, events, request_log=stream)

    try:
        outcome = run_async(run())
    except (RegistryError, NoSourcesSelectedError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    raise typer.Exit(code=0 if outcome is SyncOutcome.SUCCESS else 1)


@app.command("list")
def list_postings(
    location: Annotated[str | None, typer.Option("--location")] = None,
    term: Annotated[str | None, typer.Option("--term")] = None,
    role: Annotated[str | None, typer.Option("--role")] = None,
    degree: Annotated[
        str | None,
        typer.Option("--degree", help="none, bachelors, masters, phd, unknown, any"),
    ] = None,
    language: Annotated[str | None, typer.Option("--lang")] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    company: Annotated[str | None, typer.Option("--company")] = None,
    show_all: Annotated[bool, typer.Option("--all", help="Ignore the date window")] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    as_json: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
    db: DatabaseOption = None,
) -> None:
    from rich.console import Console

    from stage.cli.render import jobs_to_json, render_jobs
    from stage.domain import (
        DEFAULT_WINDOW_DAYS,
        DegreeRequirement,
        JobFilters,
        Language,
        LocationBucket,
        RoleCategory,
    )
    from stage.services.query import JobListing, list_jobs
    from stage.storage import open_repository

    console = Console()

    try:
        filters = JobFilters(
            location=_parse_enum(location, LocationBucket, "--location"),
            term=term,
            role=_parse_enum(role, RoleCategory, "--role"),
            degree=(
                None
                if degree is None or degree.lower() == "any"
                else _parse_enum(degree, DegreeRequirement, "--degree")
            ),
            language=_parse_enum(language, Language, "--lang"),
            source=source,
            company=company,
            limit=limit,
        )
    except InvalidOptionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    async def run() -> JobListing:
        async with open_repository(_database(db)) as repository:
            return await list_jobs(
                repository,
                filters,
                window_days=None if show_all else DEFAULT_WINDOW_DAYS,
            )

    listing = run_async(run())

    if as_json:
        console.print_json(jobs_to_json(listing.jobs))
        return

    render_jobs(
        console,
        listing.jobs,
        total_matching=listing.total_matching,
        window_days=listing.window_days,
        last_sync_at=listing.last_sync_at,
    )


@app.command()
def purge(db: DatabaseOption = None) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.domain import PurgeResult
    from stage.services.maintenance import purge_expired
    from stage.storage import open_repository

    console = Console()

    async def run() -> PurgeResult:
        async with open_repository(_database(db)) as repository:
            return await purge_expired(repository, now=datetime.now(UTC))

    result = run_async(run())
    if not result.purged:
        console.print("[dim]Nothing outside the retention window.[/dim]")
        return
    promoted = (
        f", {result.promoted} duplicate(s) promoted" if result.promoted else ""
    )
    console.print(
        f"Purged {result.purged} posting(s), {result.tombstoned} tombstone(s) kept"
        f"{promoted}."
    )
    console.print(
        "[dim]Tombstones keep the original first_seen so a still-open posting is not "
        "re-ingested as new.[/dim]"
    )


@app.command()
def sources(
    clear: Annotated[
        str | None,
        typer.Option("--clear", help="Clear one bucket"),
    ] = None,
    clear_all: Annotated[
        bool,
        typer.Option("--clear-all", help="Clear every bucket"),
    ] = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_rate_state
    from stage.services.maintenance import RateStateView, rate_state
    from stage.storage import open_repository

    console = Console()

    if clear is not None and clear_all:
        console.print("[red]Pass either --clear <bucket> or --clear-all, not both.[/red]")
        raise typer.Exit(code=2)

    async def run() -> RateStateView:
        async with open_repository(_database(db)) as repository:
            return await rate_state(repository, bucket=clear, clear_all=clear_all)

    view = run_async(run())
    cleared, states = view.cleared, view.states

    if clear is not None or clear_all:
        target = "every bucket" if clear_all else f"bucket {clear!r}"
        if cleared:
            console.print(f"Cleared rate state for {target} ({cleared} row(s)).")
        else:
            console.print(f"[yellow]No stored rate state for {target}.[/yellow]")

    render_rate_state(console, states, datetime.now(UTC))


@app.command()
def quarantine(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    company: Annotated[str | None, typer.Option("--company")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    as_json: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
    db: DatabaseOption = None,
) -> None:
    from rich.console import Console

    from stage.cli.render import quarantine_to_json, render_quarantine
    from stage.domain import QuarantineFilters, RejectionReason
    from stage.services.quarantine import QuarantineListing, list_quarantined
    from stage.storage import open_repository

    console = Console()

    try:
        filters = QuarantineFilters(
            reason=_parse_enum(reason, RejectionReason, "--reason"),
            source=source,
            company=company,
            limit=limit,
        )
    except InvalidOptionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    async def run() -> QuarantineListing:
        async with open_repository(_database(db)) as repository:
            return await list_quarantined(repository, filters)

    listing = run_async(run())

    if as_json:
        console.print_json(quarantine_to_json(listing.entries))
        return

    render_quarantine(
        console,
        listing.entries,
        total_matching=listing.total_matching,
        reason_counts=listing.reason_counts,
    )


@app.command()
def discover(
    companies: Annotated[
        list[str] | None,
        typer.Argument(help="Names to slug-probe"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help="Resolve a careers URL, parsed never fetched",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Display name for --url"),
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", help="Limit to these platforms"),
    ] = None,
    expect_size: Annotated[
        str | None,
        typer.Option(
            "--expect-size",
            help="Size hint: startup, mid, large",
        ),
    ] = None,
    request_log: Annotated[
        Path | None,
        typer.Option(
            "--request-log",
            help="JSONL request log",
        ),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="Re-probe existing registry rows",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write results back to the registry",
        ),
    ] = False,
    registry: RegistryOption = None,
) -> None:
    from contextlib import ExitStack

    from rich.console import Console

    from stage.cli.logfile import open_request_log
    from stage.cli.render import render_discovery
    from stage.domain import DiscoveryEvent, DiscoveryFinished, EmployerSize, Platform

    console = Console()

    if url is not None and companies:
        console.print("[red]Pass either company names or --url, not both.[/red]")
        raise typer.Exit(code=2)
    if apply and not verify:
        console.print("[red]--apply only means something with --verify.[/red]")
        raise typer.Exit(code=2)
    if not verify and url is None and not companies:
        console.print(
            "[red]Nothing to discover.[/red] Pass a company name, a careers page with "
            "[bold]--url[/bold], or [bold]--verify[/bold] to re-probe the registry."
        )
        raise typer.Exit(code=2)

    try:
        size = _parse_enum(expect_size, EmployerSize, "--expect-size")
        platforms = (
            [_require_enum(value, Platform, "--platform") for value in platform]
            if platform
            else None
        )
    except InvalidOptionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    async def run() -> object:
        from datetime import UTC, datetime

        from stage.services.discover import probe_companies, resolve_careers_url

        today = datetime.now(UTC).date()
        with ExitStack() as stack:
            stream = (
                stack.enter_context(open_request_log(request_log))
                if request_log is not None
                else None
            )
            if verify:
                from stage.companies import load_companies, write_registry
                from stage.services.discover import apply_verification, verify_registry

                rows = load_companies(registry)
                outcome = await render_discovery(
                    console,
                    verify_registry(rows, platforms=platforms),
                    verified_on=today,
                    request_log=stream,
                    collect=True,
                )
                if apply and isinstance(outcome, DiscoveryFinished):
                    updated, ok, off = apply_verification(rows, outcome, today)
                    target = write_registry(updated, registry)
                    console.print(
                        f"\n[bold]applied[/bold] — {ok} row(s) verified, {off} disabled, "
                        f"written to {target}"
                    )
                return isinstance(outcome, DiscoveryFinished) and bool(outcome.matched)
            if url is not None:

                async def once() -> AsyncIterator[DiscoveryEvent]:
                    yield resolve_careers_url(url)

                return await render_discovery(
                    console, once(), verified_on=today, display_name=name
                )

            events = probe_companies(companies or [], platforms=platforms, size=size)
            return await render_discovery(
                console, events, verified_on=today, request_log=stream
            )

    resolved = run_async(run())
    raise typer.Exit(code=0 if resolved else 1)


def main() -> None:
    app()
