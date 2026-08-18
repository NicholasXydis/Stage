import sys
from collections.abc import AsyncIterator, Coroutine
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
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
schedule_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Manage opt-in automatic syncs for this user account",
)
app.add_typer(schedule_app, name="schedule")

RegistryOption = Annotated[
    Path | None,
    typer.Option("--registry", help="Use this company registry instead of the default"),
]
DatabaseOption = Annotated[
    Path | None,
    typer.Option("--db", help="Use this SQLite database instead of the default"),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")]
LocationOption = Annotated[
    str | None,
    typer.Option(
        "--location",
        help="Filter by location: montreal, canada, usa, international, unknown",
    ),
]
TermOption = Annotated[
    str | None,
    typer.Option("--term", help="Filter by term, such as summer-2027"),
]
RoleOption = Annotated[
    str | None,
    typer.Option(
        "--role",
        help=(
            "Filter by role: swe, security, data, ml-ai, quant, infra, hardware, embedded, "
            "general-cs, unknown"
        ),
    ),
]
DegreeOption = Annotated[
    str | None,
    typer.Option(
        "--degree",
        help="Filter by degree requirement: none, bachelors, masters, phd, unknown, any",
    ),
]
LanguageOption = Annotated[
    str | None,
    typer.Option("--lang", help="Filter by language: en, fr, bilingual, unknown"),
]
SourceOption = Annotated[
    str | None,
    typer.Option("--source", help="Filter by source adapter, such as greenhouse or lever"),
]
CompanyOption = Annotated[
    str | None,
    typer.Option("--company", help="Filter by exact employer name"),
]
WindowOption = Annotated[
    bool,
    typer.Option("--all", help="Include postings older than the default 14-day window"),
]
StaleDaysOption = Annotated[
    int | None,
    typer.Option(
        "--stale-days",
        min=1,
        max=3650,
        help="Treat a board as stale after this many days without a successful fetch",
    ),
]


def _database(explicit: Path | None) -> Path:
    from stage.paths import database_path

    return explicit.expanduser() if explicit else database_path()


def _lock_path(explicit: Path | None) -> Path:
    target = _database(explicit)
    return target.with_name(f".{target.name}.lock")


def _print_failure(exc: BaseException) -> None:
    from rich.console import Console

    from stage.cli.render import failure

    Console().print(failure(exc))


def _print_missing(posting: str) -> None:
    from rich.console import Console

    Console().print(_no_such_posting(posting))


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
    registry: RegistryOption = None,
    db: DatabaseOption = None,
) -> None:
    from contextlib import ExitStack

    from rich.console import Console

    from stage.cli.logfile import open_request_log
    from stage.cli.render import failure, render_sync
    from stage.cli.runlock import AnotherRunInProgressError, single_run
    from stage.companies import RegistryError, load_companies
    from stage.domain import SyncOutcome
    from stage.services.sync import NoSourcesSelectedError
    from stage.services.sync import sync as sync_service
    from stage.storage import open_repository

    console = Console()

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
                return await render_sync(console, events, request_log=stream)

    try:
        if dry_run:
            outcome = run_async(run())
        else:
            with single_run("sync", _lock_path(db)):
                outcome = run_async(run())
    except AnotherRunInProgressError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    except (RegistryError, NoSourcesSelectedError) as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    raise typer.Exit(code=0 if outcome is SyncOutcome.SUCCESS else 1)


@app.command("list", help="Browse recent open postings, newest first")
def list_postings(
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    degree: DegreeOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: WindowOption = False,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum number of postings to show"),
    ] = 50,
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


@app.command(help="Search titles, employers, and descriptions")
def search(
    query: Annotated[str, typer.Argument(help="Words or phrases to find; accents optional")],
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
            help="Search only postings from the last N days; by default, search every row",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum number of postings to show"),
    ] = 50,
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


@app.command(help="Show every stored detail for one posting")
def show(
    posting: Annotated[str, typer.Argument(help="Posting ID from stage list or stage search")],
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


@app.command("open", help="Open a posting's application page in the browser")
def open_posting(
    posting: Annotated[str, typer.Argument(help="Posting ID from stage list or stage search")],
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the application URL instead of opening a browser")
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


@app.command(help="Export matching postings as CSV, JSON, Markdown, or PDF")
def export(
    fmt: Annotated[str, typer.Option("--format", help="Output format: csv, json, md, pdf")] = "csv",
    out: Annotated[Path | None, typer.Option("--out", help="Output file or directory")] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing output file")] = False,
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    degree: DegreeOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: WindowOption = False,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=5000, help="Maximum number of postings to export"),
    ] = 1000,
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


@app.command(help="Find registry gaps and unclassified companies seen in feeds")
def coverage(
    unregistered: Annotated[
        bool,
        typer.Option(
            "--unregistered",
            help="Show feed companies not in the registry and not already reviewed",
        ),
    ] = False,
    classified: Annotated[
        bool,
        typer.Option("--classified", help="Show researched feed-company classifications"),
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
                    repository,
                    companies,
                    now=now,
                    unregistered=unregistered,
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
    render_coverage(console, report, now, include_classified=classified)


@app.command(help="Record why a feed-seen employer is not directly synced")
def classify(
    company: Annotated[
        str, typer.Argument(help="Employer name from stage coverage --unregistered")
    ],
    disposition: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Required unless --clear: feed-only, unavailable, custom-json-candidate, "
            "adapter-candidate, or deferred",
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Required unless --clear: evidence for this decision"),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Public careers or jobs endpoint")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove this classification")] = False,
    db: DatabaseOption = None,
) -> None:
    from datetime import UTC, datetime

    from rich.console import Console

    from stage.domain import CoverageClassification, CoverageDisposition
    from stage.storage import open_repository

    console = Console()
    if clear:
        if disposition is not None or note is not None or url is not None:
            console.print("[red]--clear cannot be combined with --status, --note, or --url.[/red]")
            raise typer.Exit(code=2)
        try:

            async def clear_entry() -> bool:
                async with open_repository(_database(db)) as repository:
                    return await repository.clear_coverage_classification(company)

            removed = run_async(clear_entry())
        except ValueError as exc:
            _print_failure(exc)
            raise typer.Exit(code=2) from exc
        if not removed:
            console.print("[yellow]No matching classification was recorded.[/yellow]")
            raise typer.Exit(code=1)
        console.print("Classification removed.")
        return
    if disposition is None or note is None:
        console.print("[red]Pass both --status and --note, or pass --clear.[/red]")
        raise typer.Exit(code=2)
    try:
        entry = CoverageClassification(
            company=company,
            disposition=_require_enum(disposition, CoverageDisposition, "--status"),
            note=note,
            checked_on=datetime.now(UTC),
            url=url,
        )

        async def record_entry() -> bool:
            async with open_repository(_database(db)) as repository:
                return await repository.record_coverage_classification(entry)

        replaced = run_async(record_entry())
    except (InvalidOptionError, ValueError) as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    console.print("Classification updated." if replaced else "Classification recorded.")


@schedule_app.command("enable", help="Create this user's daily sync and weekly discovery schedule")
def schedule_enable() -> None:
    from rich.console import Console

    from stage.cli.schedule import ScheduleError, enable

    try:
        report = enable()
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(Console(), report)


@schedule_app.command("status", help="Show whether this user's automatic schedule is enabled")
def schedule_status() -> None:
    from rich.console import Console

    from stage.cli.schedule import ScheduleError, status

    try:
        report = status()
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(Console(), report)


@schedule_app.command("disable", help="Remove this user's automatic schedule")
def schedule_disable() -> None:
    from rich.console import Console

    from stage.cli.schedule import ScheduleError, disable

    try:
        report = disable()
    except ScheduleError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    _render_schedule(Console(), report)


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


@app.command(help="Check database integrity, source health, and staleness")
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


@app.command(help="Show sync history and database totals")
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


@app.command(help="Inspect source health, rate limits, and HTTP caches")
def sources(
    reset_rate_limit: Annotated[
        str | None,
        typer.Option(
            "--reset-rate-limit",
            help="Reset stored rate-limit state for one host bucket",
        ),
    ] = None,
    reset_all_rate_limits: Annotated[
        bool,
        typer.Option(
            "--reset-all",
            help="Reset stored rate-limit state for every host bucket",
        ),
    ] = False,
    boards: Annotated[
        bool,
        typer.Option("--boards", help="Include every failing or stale board"),
    ] = False,
    clear_cache: Annotated[
        str | None,
        typer.Option(
            "--clear-cache", help="Clear saved HTTP validators for one source to force a refetch"
        ),
    ] = None,
    clear_cache_all: Annotated[
        bool,
        typer.Option("--clear-cache-all", help="Clear saved HTTP validators for every source"),
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
        render_workday_crawl_progress,
    )
    from stage.cli.serialize import emit, sources_to_json
    from stage.services.health import DoctorReport
    from stage.services.health import doctor as run_doctor
    from stage.services.maintenance import RateStateView, rate_state
    from stage.storage import open_repository

    console = Console()
    now = datetime.now(UTC)

    if reset_rate_limit is not None and reset_all_rate_limits:
        console.print(
            "[red]Pass either --reset-rate-limit <bucket> or --reset-all, not both.[/red]"
        )
        raise typer.Exit(code=2)
    if clear_cache is not None and clear_cache_all:
        console.print(
            "[red]Pass either --clear-cache <source> or --clear-cache-all, not both.[/red]"
        )
        raise typer.Exit(code=2)

    async def run() -> tuple[RateStateView, DoctorReport]:
        async with open_repository(_database(db)) as repository:
            view = await rate_state(
                repository,
                bucket=reset_rate_limit,
                clear_all=reset_all_rate_limits,
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
        console.print(f"Cleared {view.validators_cleared} saved HTTP validator(s) for {target}.")
        console.print("[dim]The next sync will fully refetch those sources.[/dim]")

    if reset_rate_limit is not None or reset_all_rate_limits:
        target = (
            "every host bucket" if reset_all_rate_limits else f"host bucket {reset_rate_limit!r}"
        )
        if cleared:
            console.print(f"Reset rate-limit state for {target} ({cleared} row(s)).")
        else:
            console.print(f"[yellow]No stored rate state for {target}.[/yellow]")

    if as_json:
        emit(
            sources_to_json(
                report,
                states,
                include_boards=boards,
                rate_states_cleared=cleared,
                cache_validators_cleared=view.validators_cleared,
            )
        )
        return

    render_source_health(console, report.sources, report.stale_after_days)
    console.print()
    render_workday_crawl_progress(console, report.workday_crawls)
    console.print()
    render_rate_state(console, states, now)
    if boards:
        console.print()
        render_board_health(console, report.sources, now)


@app.command(help="Inspect rejected postings and why they were rejected")
def quarantine(
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help=(
                "Filter by rejection reason: unknown-location, not-an-internship, "
                "out-of-scope-degree, not-a-cs-role, unknown-cs-role"
            ),
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Filter by source adapter name"),
    ] = None,
    company: Annotated[
        str | None,
        typer.Option("--company", help="Filter by exact employer name"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            max=1000,
            help="Maximum number of rejected postings to show",
        ),
    ] = 50,
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


@app.command(help="Find the job-board platform a company uses")
def discover(
    companies: Annotated[
        list[str] | None,
        typer.Argument(help="Company names to probe"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help="Extract the platform from a careers URL without fetching that URL",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Company name to show with --url"),
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", help="Probe only these platform adapters; repeatable"),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Skip these platform adapters while probing; repeatable",
        ),
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--company",
            help="With --verify, recheck only these registry companies; repeatable",
        ),
    ] = None,
    expect_size: Annotated[
        str | None,
        typer.Option(
            "--expect-size",
            help="Optional employer-size hint: startup, mid, large",
        ),
    ] = None,
    request_log: Annotated[
        Path | None,
        typer.Option(
            "--request-log",
            help="Write outbound HTTP requests to this JSON Lines file",
        ),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help="Recheck existing registry companies",
        ),
    ] = False,
    unregistered: Annotated[
        bool,
        typer.Option(
            "--unregistered",
            help="Probe companies found in feeds but missing from the registry",
        ),
    ] = False,
    direct_only: Annotated[
        bool,
        typer.Option(
            "--direct-only",
            help="With --unregistered, verify only ATS links already present in feed applications",
        ),
    ] = False,
    adopt_unnamed: Annotated[
        bool,
        typer.Option(
            "--adopt-unnamed",
            help="With --direct-only, also adopt boards whose platform publishes no board name",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            max=2000,
            help="Maximum unregistered company names to probe",
        ),
    ] = 40,
    db: DatabaseOption = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write results to the registry; requires --verify or --unregistered",
        ),
    ] = False,
    registry: RegistryOption = None,
) -> None:
    from contextlib import ExitStack

    from rich.console import Console

    from stage.cli.logfile import open_request_log
    from stage.cli.render import failure, plain, render_discovery
    from stage.cli.runlock import AnotherRunInProgressError, single_run
    from stage.companies import RegistryError
    from stage.companies import load_companies as load_registry
    from stage.domain import DiscoveryEvent, DiscoveryFinished, EmployerSize, Platform
    from stage.services.discover import NoMatchingCompanyError

    console = Console()

    if url is not None and companies:
        console.print("[red]Pass either company names or --url, not both.[/red]")
        raise typer.Exit(code=2)
    if verify and unregistered:
        console.print("[red]Pass either --verify or --unregistered, not both.[/red]")
        raise typer.Exit(code=2)
    if direct_only and not unregistered:
        console.print("[red]--direct-only requires --unregistered.[/red]")
        raise typer.Exit(code=2)
    if adopt_unnamed and not direct_only:
        console.print(
            "[red]--adopt-unnamed requires --direct-only[/red] — the token must come from an "
            "apply URL."
        )
        raise typer.Exit(code=2)
    if apply and not (verify or unregistered):
        console.print("[red]--apply only means something with --verify or --unregistered.[/red]")
        raise typer.Exit(code=2)
    if platform and exclude:
        console.print("[red]Pass either --platform or --exclude, not both.[/red]")
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
        excluded = (
            [_require_enum(value, Platform, "--exclude") for value in exclude] if exclude else None
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
                from stage.companies import load_companies, update_registry
                from stage.services.discover import apply_verification, verify_registry

                rows = checked if checked is not None else load_companies(registry)
                outcome = await render_discovery(
                    console,
                    verify_registry(rows, platforms=platforms, excluded=excluded, only=only),
                    verified_on=today,
                    request_log=stream,
                    collect=True,
                )
                if apply and isinstance(outcome, DiscoveryFinished):

                    def update(
                        existing: tuple[Any, ...],
                    ) -> tuple[tuple[Any, ...], tuple[int, int]]:
                        updated, ok, off = apply_verification(existing, outcome, today)
                        return updated, (ok, off)

                    target, (ok, off) = update_registry(update, registry)
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
                    excluded=excluded,
                    size=size,
                    limit=limit,
                    direct_only=direct_only,
                    adopt_unnamed=adopt_unnamed,
                    apply_rows=apply,
                    today=today,
                    stream=stream,
                )

            events = probe_companies(
                companies or [], platforms=platforms, excluded=excluded, size=size
            )
            return await render_discovery(console, events, verified_on=today, request_log=stream)

    checked = None
    if verify:
        try:
            checked = load_registry(registry)
        except RegistryError as exc:
            console.print(failure(exc))
            raise typer.Exit(code=2) from exc

    try:
        if url is not None:
            resolved = run_async(run())
        else:
            with single_run("discover", _lock_path(db)):
                resolved = run_async(run())
    except AnotherRunInProgressError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    except (RegistryError, NoMatchingCompanyError) as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=0 if resolved or unregistered else 1)


async def _adopt_unregistered(
    console: "Console",
    *,
    registry: Path | None,
    db: Path | None,
    platforms: list[Any] | None,
    excluded: list[Any] | None,
    size: Any,
    limit: int,
    direct_only: bool,
    adopt_unnamed: bool,
    apply_rows: bool,
    today: date,
    stream: Any,
) -> bool:
    from stage.cli.logfile import open_probe_journal, probe_journal_path
    from stage.cli.render import plain
    from stage.companies import load_companies, update_registry
    from stage.domain import PlatformProbed
    from stage.services.coverage import coverage
    from stage.services.discover import (
        adopt_unregistered,
        probe_companies,
        select_unregistered,
        verify_registry,
    )
    from stage.storage import open_repository

    rows = load_companies(registry)
    async with open_repository(_database(db)) as repository:
        report = await coverage(repository, rows, unregistered=True)
        ranked = [entry.company for entry in report.unregistered]
        apply_urls = await repository.company_apply_urls(ranked)
    direct_rows, name_rows = select_unregistered(
        ranked,
        apply_urls,
        limit=limit,
        direct_only=direct_only,
        platforms=platforms,
        excluded=excluded,
    )
    direct = list(direct_rows)
    names = list(name_rows)
    if not names and not direct:
        console.print(plain("No unregistered employers to probe. Run stage sync first."))
        return False

    total = len(names) + len(direct)
    console.print(
        plain(f"Probing {total} unregistered employer(s); {len(direct)} from direct feed links…")
    )
    results: list[tuple[str, Any]] = []
    streams = []
    if direct:
        streams.append(verify_registry(direct, platforms=platforms, excluded=excluded))
    if names:
        streams.append(probe_companies(names, platforms=platforms, excluded=excluded, size=size))
    with open_probe_journal() as journal:
        for events in streams:
            async for event in events:
                if stream is not None:
                    stream(event)
                if isinstance(event, PlatformProbed):
                    results.append((event.result.company, event.result))
                    journal(
                        {
                            "company": event.result.company,
                            "platform": event.result.candidate.platform.value,
                            "slug": event.result.candidate.slug,
                            "verdict": event.result.verdict.value,
                            "job_count": event.result.job_count,
                            "board_name": event.result.board_name,
                            "detail": event.result.detail,
                        }
                    )

    outcome = adopt_unregistered(rows, results, today=today, adopt_unnamed=adopt_unnamed)
    console.print(
        plain(
            f"{outcome.probed} probed — {len(outcome.adopted)} adoptable "
            f"({outcome.postings} posting(s)), {len(outcome.review)} needing review, "
            f"{len(outcome.refused)} refused, {outcome.already_known} already known"
        )
    )
    for row in outcome.adopted[:20]:
        console.print(plain(f"  + {row.company.name} — {row.job_count} job(s)"))
    if outcome.review:
        console.print(
            plain("Boards with postings but no board name — a human decides these (§5.3):")
        )
        for candidate in outcome.review[:40]:
            mark = "slug is distinctive" if candidate.distinctive else "slug is generic, check it"
            console.print(
                plain(
                    f"  ? {candidate.company} ({candidate.label}) — "
                    f"{candidate.job_count} job(s), {mark}"
                )
            )
    for company, board, reason in outcome.refused[:10]:
        console.print(plain(f"  - {company} ({board}): {reason}"))
    console.print(plain(f"Every probe result was journalled to {probe_journal_path()}"))

    if not outcome.adopted:
        return False
    if not apply_rows:
        console.print(plain("Nothing written. Re-run with --apply to add these rows."))
        return True

    def update(existing: tuple[Any, ...]) -> tuple[list[Any], Any]:
        latest = adopt_unregistered(existing, results, today=today, adopt_unnamed=adopt_unnamed)
        return [*existing, *(row.company for row in latest.adopted)], latest

    target, applied = update_registry(update, registry)
    console.print(plain(f"applied — {len(applied.adopted)} row(s) added, written to {target}"))
    return True


_HELP_GUIDE = (
    "\nStart here:\n"
    "  stage sync                         Fetch and save current postings\n"
    "  stage list                         Browse recent open postings\n"
    '  stage search "python"              Search titles, employers, and descriptions\n'
    "  stage show ID                      Inspect a posting from list or search\n"
    "  stage open ID                      Open its application page\n"
    "  stage export --format csv          Save matching postings to a file\n"
    "\nCommon filters:\n"
    "  stage list --role swe --location montreal\n"
    '  stage search "python" --term summer-2027\n'
    "  stage export --format csv --all\n"
    "\nHealth and maintenance:\n"
    "  stage doctor                       Check database and source health\n"
    "  stage schedule enable              Enable daily syncs at 10:00 local time\n"
    "  stage schedule status              Check automatic scheduling\n"
    "  stage sources                      Inspect source health, limits, and caches\n"
    "  stage canary                       Check live parser compatibility\n"
    "  stage coverage                     Find registry gaps\n"
    "  stage coverage --unregistered      Review feed employers missing from the registry\n"
    '  stage classify COMPANY --status feed-only --note "why"\n'
    "                                     Record a reviewed feed employer\n"
    "  stage quarantine                   Review rejected postings\n"
    "  stage stats                        Review sync history and totals\n"
    "  stage rescreen                     Reclassify after a lexicon change\n"
    "  stage purge --dry-run              Preview retention cleanup\n"
    "\nDiscovery:\n"
    "  stage discover COMPANY             Find a company career board\n"
    "\nLearn any command:\n"
    "  stage help COMMAND                 Explain one command and its options\n"
    "  stage COMMAND --help               Show the same detailed help"
)


def _render_schedule(console: Any, report: Any) -> None:
    console.print(f"Scheduler: {report.backend} (per user)")
    from stage.cli.schedule import matches_definition

    for action, enabled, installed in report.actions:
        state = "enabled" if enabled else "disabled"
        console.print(f"  {action.label}: {state} — {action.cadence} at {action.time} local time")
        if enabled and not matches_definition(action, installed):
            console.print(
                f"    [yellow]installed as {installed}[/yellow] — run stage schedule enable"
            )
    console.print(f"Logs: {report.log_dir}")


@app.command("help", help="Show commands, workflows, or one command options")
def show_help(
    context: typer.Context,
    topic: Annotated[str | None, typer.Argument(help="Command name to explain")] = None,
) -> None:
    root = context.parent or context
    if topic is not None:
        from typer.core import TyperGroup

        group = root.command
        if not isinstance(group, TyperGroup):
            raise typer.BadParameter(f"Unknown command {topic!r}", param_hint="topic")
        command = group.commands.get(topic)
        if command is None:
            choices = ", ".join(group.commands)
            raise typer.BadParameter(
                f"Unknown command {topic!r}. Choose from: {choices}",
                param_hint="topic",
            )
        with command.make_context(topic, [], parent=root) as command_context:
            typer.echo(command.get_help(command_context))
        return
    typer.echo(root.get_help())
    typer.echo(_HELP_GUIDE)


def main() -> None:
    from stage.cli.serialize import configure_terminal_output
    from stage.storage.migrations import SchemaVersionError

    configure_terminal_output(sys.stdout, sys.platform)
    try:
        app()
    except SchemaVersionError as exc:
        typer.echo(str(exc), err=True)
        raise SystemExit(2) from None
