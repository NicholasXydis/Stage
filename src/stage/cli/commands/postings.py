from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from stage.cli.options import (
    CompanyOption,
    DatabaseOption,
    DegreeOption,
    InvalidOptionError,
    JsonOption,
    LanguageOption,
    LocationOption,
    RoleOption,
    SourceOption,
    TermOption,
    WindowOption,
    _database,
    _filters,
    _no_such_posting,
    _parse_enum,
    _print_failure,
    _print_missing,
    _require_enum,
    app,
    run_async,
)

if TYPE_CHECKING:
    pass


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
