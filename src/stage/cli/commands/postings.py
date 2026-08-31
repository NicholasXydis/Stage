from pathlib import Path
from typing import Annotated, Any

import typer

from stage.cli.options import (
    WORD,
    AllOption,
    CompanyOption,
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    LanguageOption,
    LastDaysOption,
    LocationOption,
    NewOption,
    RoleOption,
    SourceOption,
    TermOption,
    _count,
    _database,
    _filters,
    _needs_a_row,
    _no_such_posting,
    _print_failure,
    _print_missing,
    _require_enum,
    _resolve_posting,
    app,
    run_async,
)

MAX_OPEN_AT_ONCE = 10


async def _closed_since_you_looked(repository: Any) -> str:
    from stage.cli.selection import read

    selection = read()
    if selection is None or not selection.ids:
        return ""
    gone = await repository.closed_among(selection.ids)
    if not gone:
        return ""
    return f"{gone} posting(s) from your last listing have closed."


async def _company_hint(company: str | None, repository: Any) -> str:
    if not company:
        return ""
    from difflib import get_close_matches

    from stage.domain.text import escape_markup

    names = await repository.company_names()
    if company in names:
        return ""
    asked = f"'{escape_markup(company)[:60]}'"
    near = get_close_matches(company.casefold(), [n.casefold() for n in names], n=1, cutoff=0.6)
    if not near:
        return f"No employer named {asked} is stored. Try [bold]stage list --all[/bold]."
    match = next(n for n in names if n.casefold() == near[0])
    suggestion = f"'{escape_markup(match)[:60]}'"
    return f"No employer named {asked} is stored. Did you mean {suggestion}?"


def _window(last: int | None) -> int | None:
    from stage.domain import DEFAULT_WINDOW_DAYS

    if last is None:
        return DEFAULT_WINDOW_DAYS
    return last or None


@app.command(
    help="Browse postings, stats, and review queues in a full-screen interface",
    rich_help_panel="Everyday",
)
def tui(
    db: DatabaseOption = None,
) -> None:
    from stage.tui.app import launch, summarize

    target = _database(db)
    launch(target, run_async(summarize(target)))


@app.command("list", help="Browse recent open postings, newest first", rich_help_panel="Everyday")
def list_postings(
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: AllOption = False,
    last: LastDaysOption = None,
    only_new: NewOption = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1, None),
            help="Maximum number of postings to show; --all shows every match",
        ),
    ] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from dataclasses import replace as _replace

    from stage.cli.selection import remember
    from stage.cli.serialize import emit, jobs_to_json
    from stage.services.query import JobListing, list_jobs
    from stage.storage import open_repository

    try:
        filters = _filters(
            location=location,
            term=term,
            role=role,
            language=language,
            source=source,
            company=company,
            limit=None if show_all else limit,
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> tuple[JobListing, str, str]:
        async with open_repository(_database(db)) as repository:
            scoped = filters
            if only_new:
                since = await repository.previous_sync_at()
                if since is not None:
                    scoped = _replace(filters, first_seen_after=since)
            found = await list_jobs(repository, scoped, window_days=_window(last))
            hint = "" if found.jobs else await _company_hint(company, repository)
            closed = await _closed_since_you_looked(repository)
            return found, hint, closed

    listing, hint, closed = run_async(run())
    numbered = remember(tuple(job.id for job in listing.jobs), listing.last_sync_at)

    if as_json:
        emit(jobs_to_json(listing.jobs))
        return

    from stage.cli.render import render_jobs, terminal

    console = terminal()
    if closed:
        console.print(f"[dim]{closed}[/dim]")
    render_jobs(
        console,
        listing.jobs,
        total_matching=listing.total_matching,
        window_days=listing.window_days,
        last_sync_at=listing.last_sync_at,
        numbered=numbered,
        hint=hint,
    )


@app.command(help="Search titles, employers, and descriptions", rich_help_panel="Everyday")
def search(
    query: Annotated[
        str,
        typer.Argument(
            metavar="WORDS",
            click_type=WORD,
            help="Words or phrases to find; accents optional",
        ),
    ],
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: AllOption = False,
    last: Annotated[
        int | None,
        typer.Option(
            "--last",
            metavar="DAYS",
            click_type=_count(0, 3650),
            help="Search only the last N days; by default, search every row",
        ),
    ] = None,
    only_new: NewOption = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1, None),
            help="Maximum number of postings to show; --all shows every match",
        ),
    ] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from dataclasses import replace as _replace

    from stage.cli.selection import remember
    from stage.cli.serialize import emit, jobs_to_json
    from stage.services.query import JobListing
    from stage.services.query import search_jobs as search_service
    from stage.storage import open_repository

    try:
        filters = _filters(
            location=location,
            term=term,
            role=role,
            language=language,
            source=source,
            company=company,
            limit=None if show_all else limit,
        )
    except InvalidOptionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> tuple[JobListing, str]:
        async with open_repository(_database(db)) as repository:
            scoped = filters
            if only_new:
                since = await repository.previous_sync_at()
                if since is not None:
                    scoped = _replace(filters, first_seen_after=since)
            found = await search_service(repository, query, scoped, window_days=last or None)
            hint = "" if found.jobs else await _company_hint(company, repository)
            return found, hint

    listing, hint = run_async(run())
    numbered = remember(tuple(job.id for job in listing.jobs), listing.last_sync_at)

    if as_json:
        emit(jobs_to_json(listing.jobs))
        return

    from stage.cli.render import render_search, terminal

    render_search(terminal(), listing, numbered=numbered, hint=hint)


@app.command(help="Show every stored detail for one posting", rich_help_panel="Everyday")
def show(
    postings: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="ROW...",
            click_type=WORD,
            help="Row numbers from the last listing, or full posting IDs",
        ),
    ] = None,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.selection import StaleSelectionError

    if not postings:
        terminal().print(_needs_a_row("show"))
        raise typer.Exit(code=2)
    from stage.cli.serialize import emit, posting_to_json, postings_to_json
    from stage.services.query import PostingDetail, get_posting
    from stage.storage import open_repository

    async def run() -> list[PostingDetail | None]:
        async with open_repository(_database(db)) as repository:
            found: list[PostingDetail | None] = []
            for reference in postings:
                target = await _resolve_posting(reference, repository)
                found.append(await get_posting(repository, target))
            return found

    try:
        details = run_async(run())
    except StaleSelectionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    if as_json:
        found = [detail for detail in details if detail is not None]
        emit(posting_to_json(found[0]) if len(postings) == 1 and found else postings_to_json(found))
        if any(detail is None for detail in details):
            raise typer.Exit(code=1)
        return

    from stage.cli.render import render_posting, rule, terminal

    console = terminal()
    missing = False
    for index, (reference, detail) in enumerate(zip(postings, details, strict=True)):
        if detail is None:
            _print_missing(reference)
            missing = True
            continue
        if index:
            console.print(rule())
        render_posting(console, detail)
    if missing:
        raise typer.Exit(code=1)


@app.command(
    "open", help="Open a posting's application page in the browser", rich_help_panel="Everyday"
)
def open_posting(
    postings: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="ROW...",
            click_type=WORD,
            help="Row numbers from the last listing, or full posting IDs",
        ),
    ] = None,
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the application URL instead of opening a browser")
    ] = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import plain, quoted, terminal
    from stage.cli.selection import StaleSelectionError
    from stage.domain import Job, web_url
    from stage.services.query import get_posting
    from stage.storage import open_repository

    console = terminal()

    if not postings:
        console.print(_needs_a_row("open"))
        raise typer.Exit(code=2)

    if not print_only and len(postings) > MAX_OPEN_AT_ONCE:
        console.print(
            f"[red]{len(postings)} is more than {MAX_OPEN_AT_ONCE} tabs.[/red] "
            "Open fewer, or use --print to list the URLs."
        )
        raise typer.Exit(code=2)

    async def run() -> list[Job | None]:
        async with open_repository(_database(db)) as repository:
            found: list[Job | None] = []
            for reference in postings:
                target = await _resolve_posting(reference, repository)
                detail = await get_posting(repository, target)
                found.append(None if detail is None else detail.job)
            return found

    try:
        jobs = run_async(run())
    except StaleSelectionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    import webbrowser

    missing = False
    refused = False
    for reference, job in zip(postings, jobs, strict=True):
        if job is None:
            console.print(_no_such_posting(reference))
            missing = True
            continue

        url = web_url(job.apply_url_raw)
        if url is None:
            console.print(
                f"[red]Refusing to open {quoted(job.apply_url_raw, 60)}[/red] — a posting's "
                "apply URL is untrusted input, and only a plain http or https address is "
                "launched."
            )
            refused = True
            continue

        if print_only:
            console.print(plain(url))
            continue

        if not webbrowser.open(url):
            console.print(plain(f"No browser available. The apply URL is {url}"), style="yellow")
            raise typer.Exit(code=1)
        console.print(plain(f"Opened {job.company} — {url}"))

    if refused:
        raise typer.Exit(code=2)
    if missing:
        raise typer.Exit(code=1)


@app.command(
    help="Export matching postings as CSV, JSON, Markdown, or PDF", rich_help_panel="Everyday"
)
def export(
    fmt: Annotated[
        str,
        typer.Option("--format", metavar="FORMAT", help="Output format: csv, json, md, pdf"),
    ],
    out: Annotated[
        Path | None, typer.Option("--out", metavar="FILE", help="Output file or directory")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing output file")] = False,
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    show_all: AllOption = False,
    last: LastDaysOption = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1, None),
            help="Maximum number of postings to export; every match by default",
        ),
    ] = None,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import failure, render_export, terminal
    from stage.domain import ExportFormat
    from stage.services.export import ExportError, ExportResult, export_jobs
    from stage.storage import open_repository

    console = terminal()

    try:
        export_format = _require_enum(fmt, ExportFormat, "--format")
        filters = _filters(
            location=location,
            term=term,
            role=role,
            language=language,
            source=source,
            company=company,
            limit=None if show_all else limit,
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
                window_days=_window(last),
                force=force,
            )

    try:
        result = run_async(run())
    except ExportError as exc:
        console.print(failure(exc))
        raise typer.Exit(code=2) from exc

    if not result.count:
        widen = "Relax a filter, or widen the window with [bold]--last[/bold]."
        console.print(
            f"[yellow]Nothing matched, so {result.path.name} holds headers only.[/yellow] {widen}"
        )
        return
    render_export(console, result)
