from collections.abc import AsyncIterator, Coroutine
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from datetime import date

    from rich.console import Console

    from stage.domain import JobFilters


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    import asyncio

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
JsonOption = Annotated[bool, typer.Option("--json", help="JSON output")]
LocationOption = Annotated[str | None, typer.Option("--location")]
TermOption = Annotated[str | None, typer.Option("--term")]
RoleOption = Annotated[str | None, typer.Option("--role")]
DegreeOption = Annotated[
    str | None,
    typer.Option("--degree", help="none, bachelors, masters, phd, unknown, any"),
]
LanguageOption = Annotated[str | None, typer.Option("--lang")]
SourceOption = Annotated[str | None, typer.Option("--source")]
CompanyOption = Annotated[str | None, typer.Option("--company")]
WindowOption = Annotated[bool, typer.Option("--all", help="Ignore the date window")]
StaleDaysOption = Annotated[
    int | None,
    typer.Option(
        "--stale-days",
        min=1,
        max=3650,
        help="Days without a success before a board is stale",
    ),
]


def _database(explicit: Path | None) -> Path:
    from stage.paths import database_path

    return explicit.expanduser() if explicit else database_path()


def _print_failure(exc: BaseException) -> None:
    from rich.console import Console

    from stage.cli.render import failure

    Console().print(failure(exc))


def _print_missing(posting: str) -> None:
    from rich.console import Console

    Console().print(_no_such_posting(posting))


@app.command(help="Fetch every enabled source into the local database")
def sync(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Only these sources, repeatable"),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Every source but these, repeatable"),
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
    from stage.cli.render import failure, render_sync
    from stage.companies import RegistryError, load_companies
    from stage.domain import SyncOutcome
    from stage.services.sync import NoSourcesSelectedError
    from stage.services.sync import sync as sync_service
    from stage.storage import open_repository

    console = Console()

    if source and exclude:
        console.print("[red]Pass either --source or --exclude, not both.[/red]")
        raise typer.Exit(code=2)

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
                    sources=source or None,
                    excluded=exclude or None,
                    dry_run=dry_run,
                )
                return await render_sync(console, events, request_log=stream)

    try:
        outcome = run_async(run())
    except (RegistryError, NoSourcesSelectedError) as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    raise typer.Exit(code=0 if outcome is SyncOutcome.SUCCESS else 1)


@app.command("list", help="Recent postings, newest first")
def list_postings(
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    degree: DegreeOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: WindowOption = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.serialize import emit, jobs_to_json
    from stage.domain import DEFAULT_WINDOW_DAYS
    from stage.services.query import JobListing, list_jobs
    from stage.storage import open_repository

    try:
        filters = _filters(
            location=location,
            term=term,
            role=role,
            degree=degree,
            language=language,
            source=source,
            company=company,
            limit=limit,
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
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
        emit(jobs_to_json(listing.jobs))
        return

    from rich.console import Console

    from stage.cli.render import render_jobs

    render_jobs(
        Console(),
        listing.jobs,
        total_matching=listing.total_matching,
        window_days=listing.window_days,
        last_sync_at=listing.last_sync_at,
    )


def _filters(
    *,
    location: str | None,
    term: str | None,
    role: str | None,
    degree: str | None,
    language: str | None,
    source: str | None,
    company: str | None,
    limit: int,
) -> "JobFilters":
    from stage.domain import (
        DegreeRequirement,
        JobFilters,
        Language,
        LocationBucket,
        RoleCategory,
    )

    return JobFilters(
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


@app.command(help="Full-text search over titles, employers and bodies")
def search(
    query: Annotated[str, typer.Argument(help="Words to match, accents optional")],
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    degree: DegreeOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    window: Annotated[
        int | None,
        typer.Option(
            "--window",
            min=1,
            max=3650,
            help="Limit to the last N days; the default searches every row",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.serialize import emit, jobs_to_json
    from stage.services.query import JobListing
    from stage.services.query import search_jobs as search_service
    from stage.storage import open_repository

    try:
        filters = _filters(
            location=location,
            term=term,
            role=role,
            degree=degree,
            language=language,
            source=source,
            company=company,
            limit=limit,
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> JobListing:
        async with open_repository(_database(db)) as repository:
            return await search_service(repository, query, filters, window_days=window)

    listing = run_async(run())

    if as_json:
        emit(jobs_to_json(listing.jobs))
        return

    from rich.console import Console

    from stage.cli.render import render_search

    render_search(Console(), listing)


@app.command(help="Everything stored about one posting")
def show(
    posting: Annotated[str, typer.Argument(help="Posting id, as printed by stage list --json")],
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.serialize import emit, posting_to_json
    from stage.services.query import PostingDetail, get_posting
    from stage.storage import open_repository

    async def run() -> PostingDetail | None:
        async with open_repository(_database(db)) as repository:
            return await get_posting(repository, posting)

    detail = run_async(run())
    if detail is None:
        _print_missing(posting)
        raise typer.Exit(code=1)

    if as_json:
        emit(posting_to_json(detail))
        return

    from rich.console import Console

    from stage.cli.render import render_posting

    render_posting(Console(), detail)


@app.command("open", help="Launch a posting's apply URL in the browser")
def open_posting(
    posting: Annotated[str, typer.Argument(help="Posting id, as printed by stage list --json")],
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the apply URL instead of launching it")
    ] = False,
    db: DatabaseOption = None,
) -> None:
    from rich.console import Console

    from stage.cli.render import plain, quoted
    from stage.domain import Job, web_url
    from stage.services.query import get_posting
    from stage.storage import open_repository

    console = Console()

    async def run() -> Job | None:
        async with open_repository(_database(db)) as repository:
            detail = await get_posting(repository, posting)
            return None if detail is None else detail.job

    job = run_async(run())
    if job is None:
        console.print(_no_such_posting(posting))
        raise typer.Exit(code=1)

    url = web_url(job.apply_url_raw)
    if url is None:
        console.print(
            f"[red]Refusing to open {quoted(job.apply_url_raw, 60)}[/red] — a posting's apply "
            "URL is untrusted input, and only a plain http or https address is launched."
        )
        raise typer.Exit(code=2)

    if print_only:
        console.print(plain(url))
        return

    import webbrowser

    if not webbrowser.open(url):
        console.print(plain(f"No browser available. The apply URL is {url}"), style="yellow")
        raise typer.Exit(code=1)
    console.print(plain(f"Opened {job.company} — {url}"))


def _no_such_posting(posting: str) -> str:
    from stage.cli.render import quoted

    return (
        f"[red]No posting with id {quoted(posting, 80)}.[/red] Ids look like "
        "[bold]greenhouse:acme:1234[/bold] — find one with [bold]stage list --json[/bold] "
        "or [bold]stage search[/bold]."
    )


@app.command(help="Write the current filters out as csv, json, md or pdf")
def export(
    fmt: Annotated[str, typer.Option("--format", help="csv, json, md, pdf")] = "csv",
    out: Annotated[Path | None, typer.Option("--out", help="Destination file or directory")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file")] = False,
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    degree: DegreeOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: WindowOption = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=5000)] = 1000,
    db: DatabaseOption = None,
) -> None:
    from rich.console import Console

    from stage.cli.render import failure, render_export
    from stage.domain import DEFAULT_WINDOW_DAYS, ExportFormat
    from stage.services.export import ExportError, ExportResult, export_jobs
    from stage.storage import open_repository

    console = Console()

    try:
        export_format = _require_enum(fmt, ExportFormat, "--format")
        filters = _filters(
            location=location,
            term=term,
            role=role,
            degree=degree,
            language=language,
            source=source,
            company=company,
            limit=limit,
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> ExportResult:
        async with open_repository(_database(db)) as repository:
            return await export_jobs(
                repository,
                filters,
                fmt=export_format,
                destination=out,
                window_days=None if show_all else DEFAULT_WINDOW_DAYS,
                force=force,
            )

    try:
        result = run_async(run())
    except ExportError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    if not result.count:
        widen = (
            "Relax a filter."
            if show_all
            else "Relax a filter, or pass [bold]--all[/bold] to ignore the 14-day window."
        )
        console.print(
            f"[yellow]Nothing matched, so {result.path.name} holds headers only.[/yellow] {widen}"
        )
        return
    render_export(console, result)


@app.command(help="Registry rows producing nothing, and employers absent from it")
def coverage(
    unregistered: Annotated[
        bool,
        typer.Option(
            "--unregistered", help="Companies seen in a feed but absent from the registry"
        ),
    ] = False,
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_coverage
    from stage.cli.serialize import coverage_to_json, emit
    from stage.companies import RegistryError, load_companies
    from stage.services.coverage import CoverageReport
    from stage.services.coverage import coverage as coverage_service
    from stage.storage import open_repository

    console = Console()
    now = datetime.now(UTC)

    async def run() -> CoverageReport:
        companies = load_companies(registry)
        async with open_repository(_database(db)) as repository:
            if stale_days is None:
                return await coverage_service(
                    repository, companies, now=now, unregistered=unregistered
                )
            return await coverage_service(
                repository,
                companies,
                now=now,
                unregistered=unregistered,
                stale_after_days=stale_days,
            )

    try:
        report = run_async(run())
    except RegistryError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    if as_json:
        emit(coverage_to_json(report))
        return
    render_coverage(console, report, now)


@app.command(help="Apply the retention policy now")
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


@app.command(help="Re-run classification over stored postings after a lexicon change")
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
    if not result.examined:
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
    console.print(
        f"Re-screened {result.examined} posting(s) — "
        f"[yellow]{result.quarantined} moved to quarantine[/yellow]."
    )
    console.print(
        "[dim]Screening only. A posting the lexicon should now keep comes back on the next "
        "sync, which is what re-fetches the fields classification needs.[/dim]"
    )


@app.command(help="Integrity, source health and staleness in one check")
def doctor(
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
    registry: RegistryOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import failure, render_doctor
    from stage.cli.serialize import emit, health_to_json
    from stage.companies import RegistryError, load_companies
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.storage import open_repository

    console = Console()
    now = datetime.now(UTC)
    try:
        rows = load_companies(registry)
    except RegistryError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    async def run() -> DoctorReport:
        async with open_repository(_database(db)) as repository:
            if stale_days is None:
                return await run_doctor(repository, now=now, companies=rows)
            return await run_doctor(
                repository, now=now, stale_after_days=stale_days, companies=rows
            )

    report = run_async(run())
    if as_json:
        emit(health_to_json(report))
    else:
        render_doctor(console, report, now)
    if not report.is_healthy:
        raise typer.Exit(code=1)


@app.command(help="Sync history and database composition")
def stats(
    runs: Annotated[
        int,
        typer.Option("--runs", min=1, max=200, help="How many recent syncs to show"),
    ] = 10,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import render_stats
    from stage.cli.serialize import emit, stats_to_json
    from stage.services.health import StatsReport, statistics
    from stage.storage import open_repository

    console = Console()

    async def run() -> StatsReport:
        async with open_repository(_database(db)) as repository:
            return await statistics(repository, history=max(1, runs))

    report = run_async(run())
    if as_json:
        emit(stats_to_json(report))
    else:
        render_stats(console, report, datetime.now(UTC))


@app.command(help="Probe one live board per platform against the shape we parse")
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


@app.command(help="Per-source health, latency, cache ratio and rate state")
def sources(
    clear: Annotated[
        str | None,
        typer.Option("--clear", help="Clear one bucket"),
    ] = None,
    clear_all: Annotated[
        bool,
        typer.Option("--clear-all", help="Clear every bucket"),
    ] = False,
    boards: Annotated[
        bool,
        typer.Option("--boards", help="List every board that is failing or stale"),
    ] = False,
    clear_cache: Annotated[
        str | None,
        typer.Option(
            "--clear-cache", help="Drop one source's cached validators to force a refetch"
        ),
    ] = None,
    clear_cache_all: Annotated[
        bool,
        typer.Option("--clear-cache-all", help="Drop every cached validator"),
    ] = False,
    stale_days: StaleDaysOption = None,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.cli.render import (
        render_board_health,
        render_rate_state,
        render_source_health,
    )
    from stage.cli.serialize import emit, health_to_json
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import RateStateView, rate_state
    from stage.storage import open_repository

    console = Console()
    now = datetime.now(UTC)

    if clear is not None and clear_all:
        console.print("[red]Pass either --clear <bucket> or --clear-all, not both.[/red]")
        raise typer.Exit(code=2)

    async def run() -> tuple[RateStateView, DoctorReport]:
        async with open_repository(_database(db)) as repository:
            view = await rate_state(
                repository,
                bucket=clear,
                clear_all=clear_all,
                clear_cache=clear_cache,
                clear_cache_all=clear_cache_all,
            )
            report = (
                await run_doctor(repository, now=now)
                if stale_days is None
                else await run_doctor(repository, now=now, stale_after_days=stale_days)
            )
            return view, report

    view, report = run_async(run())
    cleared, states = view.cleared, view.states

    if view.validators_cleared or clear_cache is not None or clear_cache_all:
        target = "every source" if clear_cache_all else f"source {clear_cache!r}"
        console.print(
            f"Dropped {view.validators_cleared} cached validator(s) for {target} — "
            "the next sync refetches in full instead of asking for a 304."
        )

    if clear is not None or clear_all:
        target = "every bucket" if clear_all else f"bucket {clear!r}"
        if cleared:
            console.print(f"Cleared rate state for {target} ({cleared} row(s)).")
        else:
            console.print(f"[yellow]No stored rate state for {target}.[/yellow]")

    if as_json:
        emit(health_to_json(report))
        return

    render_source_health(console, report.sources, report.stale_after_days)
    console.print()
    render_rate_state(console, states, now)
    if boards:
        console.print()
        render_board_health(console, report.sources, now)


@app.command(help="Audit rejected postings and the reasons they were rejected")
def quarantine(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    company: Annotated[str | None, typer.Option("--company")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from rich.console import Console

    from stage.cli.render import render_quarantine
    from stage.cli.serialize import emit, quarantine_to_json
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
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> QuarantineListing:
        async with open_repository(_database(db)) as repository:
            return await list_quarantined(repository, filters)

    listing = run_async(run())

    if as_json:
        emit(quarantine_to_json(listing.entries))
        return

    render_quarantine(
        console,
        listing.entries,
        total_matching=listing.total_matching,
        reason_counts=listing.reason_counts,
    )


@app.command(help="Resolve which board a company publishes on")
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
    only: Annotated[
        list[str] | None,
        typer.Option("--company", help="With --verify, probe only these registry rows"),
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
    unregistered: Annotated[
        bool,
        typer.Option(
            "--unregistered",
            help="Probe employers seen in feeds but absent from the registry",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=2000, help="How many unregistered names to probe"),
    ] = 40,
    db: DatabaseOption = None,
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
    from stage.cli.render import failure, plain, render_discovery
    from stage.companies import RegistryError
    from stage.domain import DiscoveryEvent, DiscoveryFinished, EmployerSize, Platform
    from stage.services.discover import NoMatchingCompanyError

    console = Console()

    if url is not None and companies:
        console.print("[red]Pass either company names or --url, not both.[/red]")
        raise typer.Exit(code=2)
    if verify and unregistered:
        console.print("[red]Pass either --verify or --unregistered, not both.[/red]")
        raise typer.Exit(code=2)
    if apply and not (verify or unregistered):
        console.print("[red]--apply only means something with --verify or --unregistered.[/red]")
        raise typer.Exit(code=2)
    if not verify and not unregistered and url is None and not companies:
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
        _print_failure(exc)
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
                    verify_registry(rows, platforms=platforms, only=only),
                    verified_on=today,
                    request_log=stream,
                    collect=True,
                )
                if apply and isinstance(outcome, DiscoveryFinished):
                    updated, ok, off = apply_verification(rows, outcome, today)
                    target = write_registry(updated, registry)
                    console.print(
                        plain(
                            f"\napplied — {ok} row(s) verified, {off} disabled, written to {target}"
                        )
                    )
                return isinstance(outcome, DiscoveryFinished) and bool(outcome.matched)
            if url is not None:

                async def once() -> AsyncIterator[DiscoveryEvent]:
                    yield resolve_careers_url(url)

                return await render_discovery(console, once(), display_name=name)

            if unregistered:
                return await _adopt_unregistered(
                    console,
                    registry=registry,
                    db=db,
                    platforms=platforms,
                    size=size,
                    limit=limit,
                    apply_rows=apply,
                    today=today,
                    stream=stream,
                )

            events = probe_companies(companies or [], platforms=platforms, size=size)
            return await render_discovery(console, events, verified_on=today, request_log=stream)

    try:
        resolved = run_async(run())
    except (RegistryError, NoMatchingCompanyError) as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=0 if resolved else 1)


async def _adopt_unregistered(
    console: "Console",
    *,
    registry: Path | None,
    db: Path | None,
    platforms: list[Any] | None,
    size: Any,
    limit: int,
    apply_rows: bool,
    today: "date",
    stream: Any,
) -> bool:
    from stage.cli.render import plain
    from stage.companies import load_companies, write_registry
    from stage.domain import PlatformProbed
    from stage.services.coverage import coverage
    from stage.services.discover import adopt_unregistered, probe_companies
    from stage.storage import open_repository

    rows = load_companies(registry)
    async with open_repository(_database(db)) as repository:
        report = await coverage(repository, rows, unregistered=True)
    names = [entry.company for entry in report.unregistered][:limit]
    if not names:
        console.print(plain("No unregistered employers to probe. Run stage sync first."))
        return False

    console.print(plain(f"Probing {len(names)} unregistered employer(s)…"))
    results: list[tuple[str, Any]] = []
    async for event in probe_companies(names, platforms=platforms, size=size):
        if stream is not None:
            stream(event)
        if isinstance(event, PlatformProbed):
            results.append((event.result.company, event.result))

    outcome = adopt_unregistered(rows, results, today=today)
    console.print(
        plain(
            f"{outcome.probed} probed — {len(outcome.adopted)} adoptable "
            f"({outcome.postings} posting(s)), {len(outcome.refused)} refused, "
            f"{outcome.already_known} already known"
        )
    )
    for row in outcome.adopted[:20]:
        console.print(plain(f"  + {row.company.name} — {row.job_count} job(s)"))
    for company, board, reason in outcome.refused[:10]:
        console.print(plain(f"  - {company} ({board}): {reason}"))

    if not outcome.adopted:
        return False
    if not apply_rows:
        console.print(plain("Nothing written. Re-run with --apply to add these rows."))
        return True
    target = write_registry([*rows, *(row.company for row in outcome.adopted)], registry)
    console.print(plain(f"applied — {len(outcome.adopted)} row(s) added, written to {target}"))
    return True


@app.command("help", help="Show this command list")
def show_help(context: typer.Context) -> None:
    root = context.parent or context
    typer.echo(root.get_help())


def main() -> None:
    app()
