import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from stage.cli.options import (
    WORD,
    AllOption,
    CompanyOption,
    DatabaseOption,
    ExportAllOption,
    InvalidOptionError,
    JsonOption,
    LanguageOption,
    LastDaysOption,
    LocationOption,
    MineOption,
    NewOption,
    RoleOption,
    SourceOption,
    TermOption,
    UntrackedOption,
    _count,
    _database,
    _filters,
    _needs_a_row,
    _print_error,
    _print_missing,
    _print_warning,
    _refuse,
    _refusing,
    _require_enum,
    _resolve_posting,
    app,
    output_path,
    run_async,
)

if TYPE_CHECKING:
    from stage.domain import JobFilters
    from stage.services.tracker import TrackerView

FIRST_SYNC_NOTE = "Only one sync so far, so every posting is new. Newest posted first."


async def _tracked(
    repository: Any, filters: "JobFilters", untracked: bool
) -> tuple["JobFilters", "TrackerView"]:
    from dataclasses import replace

    from stage.services.tracker import tracker_view

    view = await tracker_view(repository)
    if untracked and view.problem:
        _refuse(
            "[red]--untracked needs your tracker, which could not be read, so nothing was "
            "listed.[/red] Run [bold]stage tracker[/bold] to see why."
        )
    excluded = view.tracked if untracked else view.hidden
    return replace(filters, excluded_ids=filters.excluded_ids | excluded), view


async def _scoped(
    repository: Any, filters: "JobFilters", untracked: bool, only_new: bool
) -> tuple["JobFilters", "TrackerView", bool]:
    from dataclasses import replace

    scoped, view = await _tracked(repository, filters, untracked)
    since = await repository.previous_sync_at() if only_new else None
    if since is not None:
        scoped = replace(scoped, first_seen_after=since)
    return scoped, view, only_new and since is None


def _hidden_hint(count: int) -> str:
    return f"{count:,} hidden: stage tracker --status hidden"


def _no_location_hint(count: int, command: str, filters: "JobFilters", untracked: bool) -> str:
    from shlex import quote

    typed = [
        (flag, value)
        for flag, value in (
            ("--term", filters.term),
            ("--role", filters.role and filters.role.value),
            ("--language", filters.language and filters.language.value),
            ("--source", filters.source),
            ("--company", filters.company),
        )
        if value
    ]
    flags = "".join(f" {flag} {quote(value)}" for flag, value in typed)
    flags += " --untracked" if untracked else ""
    verb = "has" if count == 1 else "have"
    return f"{count:,} more {verb} no location given: {command} --mine{flags} --location unknown"


async def _closed_since_you_looked(repository: Any) -> str:
    from stage.cli.selection import read
    from stage.domain.text import counted

    selection = read()
    if selection is None or not selection.ids:
        return ""
    gone = await repository.closed_among(selection.ids)
    if not gone:
        return ""
    verb = "has" if gone == 1 else "have"
    return f"{counted(gone, 'posting')} from your last listing {verb} closed."


async def _company_hint(company: str | None, repository: Any) -> str:
    if not company:
        return ""
    from stage.domain.text import did_you_mean, escape_markup
    from stage.services.query import company_match

    stored, near = await company_match(repository, company)
    if stored:
        return ""
    said = f'Stage stores no posting from an employer named "{escape_markup(company)[:60]}".'
    return said if near is None else f"{said} {did_you_mean(escape_markup(near)[:60])}"


def _window(last: int | None) -> int | None:
    from stage.domain import DEFAULT_WINDOW_DAYS

    if last is None:
        return DEFAULT_WINDOW_DAYS
    return last or None


@app.command(
    help="Browse postings, your applications and stats in a full screen",
    rich_help_panel="Everyday",
)
def tui(
    db: DatabaseOption = None,
) -> None:
    from stage.tui.app import launch, summarize

    target = _database(db)
    summary = run_async(summarize(target))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _refuse(
            "[red]stage tui needs a terminal.[/red] Run it in a terminal window, or use "
            "[bold]stage list[/bold] for plain output."
        )
    launch(target, summary)


@app.command("list", help="Browse recent open postings, newest first", rich_help_panel="Everyday")
def list_postings(
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    mine: MineOption = False,
    untracked: UntrackedOption = False,
    show_all: AllOption = False,
    last: LastDaysOption = None,
    only_new: NewOption = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1),
            help="Maximum number of postings to show (--all shows every match)",
        ),
    ] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.selection import remember
    from stage.cli.serialize import emit, jobs_to_json
    from stage.services.query import JobListing, count_hidden, list_jobs
    from stage.services.tracker import TrackerView
    from stage.storage import open_repository

    with _refusing(InvalidOptionError):
        filters, preferences = _filters(
            location=location,
            term=term,
            role=role,
            language=language,
            source=source,
            company=company,
            limit=None if show_all else limit,
            mine=mine,
            as_json=as_json,
        )

    async def run() -> tuple[JobListing, str, str, str, TrackerView, int]:
        async with open_repository(_database(db)) as repository:
            scoped, view, first_sync = await _scoped(repository, filters, untracked, only_new)
            found = await list_jobs(repository, scoped, window_days=_window(last))
            hidden = await count_hidden(repository, scoped, view.hidden, window_days=_window(last))
            hint = "" if found.jobs else await _company_hint(filters.company, repository)
            closed = await _closed_since_you_looked(repository)
            note = FIRST_SYNC_NOTE if first_sync and found.last_sync_at else ""
            return found, hint, closed, note, view, hidden

    listing, hint, closed, note, view, hidden = run_async(run())
    if view.problem:
        _print_warning(view.problem, as_json)

    if as_json:
        emit(jobs_to_json(listing.jobs))
        return
    numbered = remember(tuple(job.id for job in listing.jobs), listing.last_sync_at)

    from stage.cli.render import plain, terminal
    from stage.cli.render.postings import render_jobs

    console = terminal()
    if closed:
        console.print(f"[dim]{closed}[/dim]")
    if note:
        console.print(f"[dim]{note}[/dim]")
    if preferences is not None:
        console.print(plain(preferences.describe(filters), "dim"))
    render_jobs(
        console,
        listing.jobs,
        total_matching=listing.total_matching,
        window_days=listing.window_days,
        last_sync_at=listing.last_sync_at,
        numbered=numbered,
        hint=hint,
        marks=view.marks(listing.jobs),
    )
    if listing.no_location:
        again = "stage list" + (f" --last {last}" if last is not None else "")
        again += " --new" if only_new else ""
        console.print(
            plain(_no_location_hint(listing.no_location, again, filters, untracked), "dim")
        )
    if hidden:
        console.print(plain(_hidden_hint(hidden), "dim"))


@app.command(help="Search titles, employers, and descriptions", rich_help_panel="Everyday")
def search(
    words: Annotated[
        list[str],
        typer.Argument(
            metavar="WORDS",
            click_type=WORD,
            help="Words to find, all of them, each as the start of a word. Accents are optional",
        ),
    ],
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    mine: MineOption = False,
    untracked: UntrackedOption = False,
    show_all: AllOption = False,
    last: Annotated[
        int | None,
        typer.Option(
            "--last",
            metavar="DAYS",
            click_type=_count(0, 3650),
            help="Only postings first seen in the last DAYS days, every stored posting by default",
        ),
    ] = None,
    only_new: NewOption = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1),
            help="Maximum number of postings to show (--all shows every match)",
        ),
    ] = 50,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.selection import remember
    from stage.cli.serialize import emit, jobs_to_json
    from stage.services.query import JobListing, count_hidden
    from stage.services.query import search_jobs as search_service
    from stage.services.tracker import TrackerView
    from stage.storage import open_repository

    query = " ".join(words)
    if not query.strip():
        _refuse(
            "[red]stage search needs words to find.[/red] For example "
            "[bold]stage search software intern[/bold]."
        )
    with _refusing(InvalidOptionError):
        filters, preferences = _filters(
            location=location,
            term=term,
            role=role,
            language=language,
            source=source,
            company=company,
            limit=None if show_all else limit,
            mine=mine,
            as_json=as_json,
        )

    async def run() -> tuple[JobListing, str, str, TrackerView, int]:
        async with open_repository(_database(db)) as repository:
            scoped, view, first_sync = await _scoped(repository, filters, untracked, only_new)
            found = await search_service(repository, query, scoped, window_days=last or None)
            hidden = await count_hidden(
                repository, scoped, view.hidden, window_days=last or None, query=query
            )
            hint = "" if found.jobs else await _company_hint(filters.company, repository)
            note = FIRST_SYNC_NOTE if first_sync and found.last_sync_at else ""
            return found, hint, note, view, hidden

    listing, hint, note, view, hidden = run_async(run())
    if view.problem:
        _print_warning(view.problem, as_json)

    if as_json:
        emit(jobs_to_json(listing.jobs))
        return
    numbered = remember(tuple(job.id for job in listing.jobs), listing.last_sync_at)

    from stage.cli.render import plain, terminal
    from stage.cli.render.postings import render_search

    console = terminal()
    if note:
        console.print(f"[dim]{note}[/dim]")
    if preferences is not None:
        console.print(plain(preferences.describe(filters), "dim"))
    render_search(console, listing, numbered=numbered, hint=hint, marks=view.marks(listing.jobs))
    if listing.no_location:
        again = f'stage search "{" ".join(listing.terms)}"' + (f" --last {last}" if last else "")
        again += " --new" if only_new else ""
        console.print(
            plain(_no_location_hint(listing.no_location, again, filters, untracked), "dim")
        )
    if hidden:
        console.print(plain(_hidden_hint(hidden), "dim"))


@app.command(help="Show every stored detail for postings", rich_help_panel="Everyday")
def show(
    postings: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="ROW...",
            click_type=WORD,
            help="Row numbers or ranges like 1-5, or full posting IDs",
        ),
    ] = None,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import terminal
    from stage.cli.selection import BadRangeError, StaleSelectionError, expand

    if not postings:
        _refuse(_needs_a_row("show"))
    with _refusing(BadRangeError):
        postings = expand(postings)
    from stage.cli.serialize import _posting_payload, emit, tracked_only_payload
    from stage.domain import dump
    from stage.domain.tracker import TrackedPosting
    from stage.services.query import PostingDetail, get_posting
    from stage.services.tracker import tracked_for, tracked_payload
    from stage.storage import open_repository

    async def run() -> tuple[list[str], list[PostingDetail | None], dict[str, TrackedPosting], str]:
        async with open_repository(_database(db)) as repository:
            targets: list[str] = []
            found: list[PostingDetail | None] = []
            for reference in postings:
                target = await _resolve_posting(reference, repository)
                targets.append(target)
                found.append(await get_posting(repository, target))
            tracked, problem = await tracked_for(repository, targets)
            return targets, found, tracked, problem

    with _refusing(StaleSelectionError):
        targets, details, tracked, problem = run_async(run())
    if problem:
        _print_warning(problem, as_json)
    absent = [
        detail is None and target not in tracked
        for target, detail in zip(targets, details, strict=True)
    ]

    if as_json:
        payloads = []
        for target, detail in zip(targets, details, strict=True):
            entry = tracked.get(target)
            info = tracked_payload(entry) if entry is not None else None
            if detail is not None:
                payloads.append(_posting_payload(detail, info))
            elif info is not None:
                payloads.append(tracked_only_payload(info))
        emit(dump(payloads[0]) if len(postings) == 1 and payloads else dump(payloads))
        if any(absent):
            from stage.cli.serialize import fail

            for reference, missing in zip(postings, absent, strict=True):
                if missing:
                    fail(f'No posting has the id "{reference[:80]}".')
            raise typer.Exit(code=1)
        return

    from stage.cli.render import rule
    from stage.cli.render.postings import render_posting, render_snapshot

    console = terminal()
    shown = 0
    for reference, target, detail in zip(postings, targets, details, strict=True):
        entry = tracked.get(target)
        if detail is None and entry is None:
            _print_missing(reference)
            continue
        if shown:
            console.print(rule())
        shown += 1
        if detail is not None:
            render_posting(console, detail, tracked=entry)
        elif entry is not None:
            render_snapshot(console, entry)
    if any(absent):
        raise typer.Exit(code=1)


@app.command(
    "open",
    help="Open apply pages, up to 50, and check each is still up",
    rich_help_panel="Everyday",
)
def open_posting(
    postings: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="ROW...",
            click_type=WORD,
            help="Row numbers or ranges like 1-5, or full posting IDs",
        ),
    ] = None,
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the apply links instead of opening a browser")
    ] = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import plain, quoted, terminal
    from stage.cli.selection import BadRangeError, StaleSelectionError, expand
    from stage.domain import MAX_OPEN_AT_ONCE, web_url
    from stage.services.query import get_posting
    from stage.services.tracker import tracked_for
    from stage.storage import open_repository

    console = terminal()

    if not postings:
        _refuse(_needs_a_row("open"))
    with _refusing(BadRangeError):
        postings = expand(postings)

    if not print_only and len(postings) > MAX_OPEN_AT_ONCE:
        first = postings[0]
        fewer = (
            f"Try [bold]stage open {first}-{int(first) + MAX_OPEN_AT_ONCE - 1}[/bold]"
            if first.isdecimal()
            else f"Open {MAX_OPEN_AT_ONCE} or fewer"
        )
        _refuse(
            f"[red]That would open {len(postings):,} tabs, and Stage opens at most "
            f"{MAX_OPEN_AT_ONCE} at once.[/red] {fewer}, "
            f"or add [bold]--print[/bold] to list all {len(postings):,} links."
        )

    async def run() -> list[tuple[str, str, str, bool] | None]:
        async with open_repository(_database(db)) as repository:
            targets = [await _resolve_posting(reference, repository) for reference in postings]
            tracked, _ = await tracked_for(repository, targets)
            found: list[tuple[str, str, str, bool] | None] = []
            for target in targets:
                detail = await get_posting(repository, target)
                entry = tracked.get(target)
                if detail is not None:
                    found.append((target, detail.job.company, detail.job.apply_url_raw, False))
                elif entry is not None:
                    found.append((target, entry.snapshot.company, entry.snapshot.apply_url, True))
                else:
                    found.append(None)
            return found

    with _refusing(StaleSelectionError):
        links = run_async(run())

    import webbrowser

    missing = False
    refused = False
    launched: list[str] = []
    for reference, link in zip(postings, links, strict=True):
        if link is None:
            _print_missing(reference)
            missing = True
            continue
        target, company, raw, kept = link

        url = web_url(raw)
        if url is None:
            _print_error(
                f"[red]Stage did not open {quoted(raw, 60)}.[/red] It opens only plain http "
                "and https links, and a posting's link comes from its board."
            )
            refused = True
            continue

        if print_only:
            console.print(plain(url), soft_wrap=True)
            continue

        if not webbrowser.open(url):
            _print_error(
                plain(
                    f"No browser is available, so {url} did not open. Add --print to list "
                    "links instead.",
                    "yellow",
                ),
                soft_wrap=True,
            )
            raise typer.Exit(code=1)
        source = " from your tracker" if kept else ""
        console.print(plain(f"Opened {company}{source}: {url}"), soft_wrap=True)
        launched.append(target)

    if launched:
        from stage.services.liveness import check_opened, gone_line

        checked = run_async(check_opened(_database(db), launched))
        for job in checked.gone:
            console.print(plain(gone_line(job), style="yellow"), soft_wrap=True)

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
        Path | None,
        typer.Option("--out", metavar="FILE", parser=output_path, help="Output file or directory"),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing output file")] = False,
    location: LocationOption = None,
    term: TermOption = None,
    role: RoleOption = None,
    language: LanguageOption = None,
    source: SourceOption = None,
    company: CompanyOption = None,
    mine: MineOption = False,
    untracked: UntrackedOption = False,
    show_all: ExportAllOption = False,
    last: LastDaysOption = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            metavar="N",
            click_type=_count(1),
            help="Maximum number of postings to export, every match by default",
        ),
    ] = None,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.render import plain, terminal
    from stage.cli.render.postings import render_export
    from stage.domain import DEFAULT_WINDOW_DAYS, ExportFormat
    from stage.services.export import ExportError, ExportResult, export_jobs
    from stage.services.query import count_hidden, count_without_location
    from stage.services.tracker import TrackerView
    from stage.storage import open_repository

    console = terminal()

    with _refusing(InvalidOptionError):
        export_format = _require_enum(fmt, ExportFormat, "--format")
        filters, preferences = _filters(
            location=location,
            term=term,
            role=role,
            language=language,
            source=source,
            company=company,
            limit=None if show_all else limit,
            mine=mine,
        )

    async def run() -> tuple[ExportResult, int | None, TrackerView, int]:
        async with open_repository(_database(db)) as repository:
            scoped, view = await _tracked(repository, filters, untracked)
            result = await export_jobs(
                repository,
                scoped,
                fmt=export_format,
                destination=out,
                window_days=_window(last),
                force=force,
            )
            left_out = await count_without_location(repository, scoped, window_days=_window(last))
            hidden = await count_hidden(repository, scoped, view.hidden, window_days=_window(last))
            return result, left_out, view, hidden

    with _refusing(ExportError, soft_wrap=True):
        result, left_out, view, hidden = run_async(run())
    if view.problem:
        _print_warning(view.problem, False)

    if not result.count:
        widen = (
            "Relax a filter, or widen the window with [bold]--last[/bold]."
            if last and last < DEFAULT_WINDOW_DAYS
            else "Relax a filter."
        )
        if preferences is not None:
            console.print(plain(preferences.describe(filters), "dim"))
        held = "an empty list" if result.fmt is ExportFormat.JSON else "headers only"
        console.print(
            f"[yellow]Nothing matched, so {result.path.name} holds {held}.[/yellow] {widen}"
        )
    else:
        if preferences is not None:
            console.print(plain(preferences.describe(filters), "dim"))
        render_export(console, result)
    if left_out:
        again = f"stage export --format {export_format.value}"
        again += f" --last {last}" if last is not None else ""
        console.print(plain(_no_location_hint(left_out, again, filters, untracked), "dim"))
    if hidden:
        console.print(plain(_hidden_hint(hidden), "dim"))
