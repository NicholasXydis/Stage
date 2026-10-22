from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from stage.cli.options import (
    WORD,
    DatabaseOption,
    JsonOption,
    _database,
    _needs_a_row,
    _no_such_posting,
    _print_failure,
    _refuse,
    _resolve_posting,
    app,
    run_async,
)

if TYPE_CHECKING:
    from stage.domain import ExportFormat
    from stage.services.tracker import ChangeReport, TrackerListing

PANEL = "Applications"

RowsArgument = Annotated[
    list[str] | None,
    typer.Argument(
        metavar="ROW...",
        click_type=WORD,
        help="Row numbers or ranges like 1-5, or full posting IDs",
    ),
]
OnOption = Annotated[
    str | None,
    typer.Option(
        "--on", metavar="DATE", help="The day it happened, like 2026-10-20; today by default"
    ),
]
NoteOption = Annotated[
    str | None,
    typer.Option(
        "--note", metavar="TEXT", help="A short note kept on this machine; empty clears it"
    ),
]


@app.command(help="Keep postings to come back to", rich_help_panel=PANEL)
def save(postings: RowsArgument = None, note: NoteOption = None, db: DatabaseOption = None) -> None:
    from stage.domain.tracker import ApplicationStatus

    _change("save", postings, ApplicationStatus.SAVED, None, note, db, forward_only=True)


@app.command(help="Mark postings as applied, today or on a date you give", rich_help_panel=PANEL)
def applied(
    postings: RowsArgument = None,
    on: OnOption = None,
    note: NoteOption = None,
    db: DatabaseOption = None,
) -> None:
    from stage.domain.tracker import ApplicationStatus

    _change("applied", postings, ApplicationStatus.APPLIED, on, note, db, forward_only=True)


@app.command(
    help="Never show postings again; stage untrack brings them back", rich_help_panel=PANEL
)
def hide(postings: RowsArgument = None, db: DatabaseOption = None) -> None:
    from stage.domain.tracker import ApplicationStatus

    _change("hide", postings, ApplicationStatus.HIDDEN, None, None, db)


@app.command(help="Set the status, date or note of tracked postings", rich_help_panel=PANEL)
def track(
    postings: RowsArgument = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            metavar="STATUS",
            help="saved, applied, interviewing, offer, rejected, withdrawn or hidden",
        ),
    ] = None,
    on: OnOption = None,
    note: NoteOption = None,
    db: DatabaseOption = None,
) -> None:
    from stage.domain.tracker import TrackerError, parse_status

    try:
        chosen = parse_status(status) if status is not None else None
    except TrackerError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    if chosen is None and on is None and note is None:
        _refuse(
            "[red]stage track needs --status, --on or --note.[/red] For example "
            "[bold]stage track 3 --status interviewing[/bold]."
        )
    _change("track", postings, chosen, on, note, db)


@app.command(help="Stop tracking postings; hidden ones show again", rich_help_panel=PANEL)
def untrack(postings: RowsArgument = None, db: DatabaseOption = None) -> None:
    from stage.cli.render import render_removed, terminal
    from stage.cli.selection import StaleSelectionError
    from stage.services.tracker import RemoveReport, TrackerProblemError, remove_tracked
    from stage.storage import open_repository

    references = _references("untrack", postings)

    async def run() -> tuple[list[str], RemoveReport]:
        async with open_repository(_database(db)) as repository:
            ids = [await _resolve_posting(reference, repository) for reference in references]
            return ids, await remove_tracked(repository, ids)

    try:
        ids, report = run_async(run())
    except StaleSelectionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    except TrackerProblemError as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc
    if report.missing:
        _refuse_missing(report.missing)
    labels = _labels(references, ids)
    named = tuple(labels.get(posting_id, posting_id) for posting_id in report.not_tracked)
    render_removed(terminal(), replace(report, not_tracked=named))


@app.command(help="Your tracked postings, latest change first", rich_help_panel=PANEL)
def tracker(
    status: Annotated[
        str | None,
        typer.Option("--status", metavar="STATUS", help="Only postings with this status"),
    ] = None,
    stats: Annotated[
        bool, typer.Option("--stats", help="Counts per status, recent applications and replies")
    ] = False,
    export: Annotated[
        str | None,
        typer.Option("--export", metavar="FORMAT", help="Write the list to a file: csv, json, md"),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", metavar="FILE", help="Where --export writes")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing --out file")] = False,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.options import InvalidOptionError, _require_enum
    from stage.domain import ExportFormat
    from stage.domain.tracker import TrackerError, parse_status
    from stage.services.tracker import FORMATS, TrackerProblemError, tracker_listing
    from stage.storage import open_repository

    if stats and (export or status):
        _refuse(
            "[red]--stats counts every tracked posting, so use it without --status or "
            "--export.[/red]"
        )
    if export and as_json:
        _refuse("[red]--export writes a file, so use it without --json.[/red]")
    if (out or force) and not export:
        _refuse("[red]--out and --force only apply with --export.[/red]")
    try:
        chosen = parse_status(status) if status is not None else None
        fmt = _require_enum(export, ExportFormat, "--export") if export else None
    except (TrackerError, InvalidOptionError) as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    if fmt is not None and fmt not in FORMATS:
        _refuse("[red]The tracker exports as csv, json or md.[/red]")

    async def run() -> "TrackerListing":
        async with open_repository(_database(db)) as repository:
            return await tracker_listing(repository, chosen)

    try:
        listing = run_async(run())
    except TrackerProblemError as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc

    if stats:
        _stats(listing, as_json)
        return
    if fmt is not None:
        _export(listing, fmt, out, force)
        return

    from stage.cli.selection import remember

    numbered = remember(tuple(row.entry.posting_id for row in listing.rows), listing.last_sync_at)
    if as_json:
        from stage.cli.serialize import emit
        from stage.services.tracker import tracker_json

        emit(tracker_json(listing.rows))
        return
    from stage.cli.render import render_tracker, terminal

    render_tracker(terminal(), listing, status=chosen, numbered=numbered)


def _stats(listing: "TrackerListing", as_json: bool) -> None:
    if as_json:
        from stage.cli.serialize import emit
        from stage.domain import dump
        from stage.services.tracker import stats_payload

        emit(dump(stats_payload(listing.stats)))
        return
    from stage.cli.render import render_tracker_stats, terminal

    render_tracker_stats(terminal(), listing.stats)


def _export(listing: "TrackerListing", fmt: "ExportFormat", out: Path | None, force: bool) -> None:
    from stage.cli.render import plain, terminal
    from stage.services.export import ExportError
    from stage.services.tracker import export_tracker

    try:
        written = export_tracker(listing.rows, fmt, destination=out, force=force)
    except ExportError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    count = len(listing.rows)
    terminal().print(
        plain(f"Wrote {count} tracked posting{'' if count == 1 else 's'} to {written}")
    )


def _references(command: str, postings: list[str] | None) -> list[str]:
    from stage.cli.render import terminal
    from stage.cli.selection import BadRangeError, expand

    if not postings:
        terminal().print(_needs_a_row(command))
        raise typer.Exit(code=2)
    try:
        return expand(postings)
    except BadRangeError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc


def _refuse_missing(missing: tuple[str, ...]) -> NoReturn:
    from stage.cli.render import terminal

    console = terminal()
    for reference in missing:
        console.print(_no_such_posting(reference))
    console.print("[red]Nothing was changed.[/red]")
    raise typer.Exit(code=1)


def _change(
    command: str,
    postings: list[str] | None,
    status: str | None,
    on_text: str | None,
    note_text: str | None,
    db: Path | None,
    *,
    forward_only: bool = False,
) -> None:
    from datetime import datetime

    from stage.cli.render import render_changes, terminal
    from stage.cli.selection import StaleSelectionError
    from stage.domain.tracker import ApplicationStatus, TrackerError, parse_day, parse_note
    from stage.services.tracker import ChangeReport, TrackerProblemError, record_status
    from stage.storage import open_repository

    references = _references(command, postings)
    chosen = ApplicationStatus(status) if status is not None else None
    try:
        on = parse_day(on_text, datetime.now().astimezone().date()) if on_text is not None else None
        note = parse_note(note_text) if note_text is not None else None
    except TrackerError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc

    async def run() -> tuple[list[str], ChangeReport]:
        async with open_repository(_database(db)) as repository:
            ids = [await _resolve_posting(reference, repository) for reference in references]
            report = await record_status(
                repository, ids, chosen, on=on, note=note, forward_only=forward_only
            )
            return ids, report

    try:
        ids, report = run_async(run())
    except StaleSelectionError as exc:
        _print_failure(exc)
        raise typer.Exit(code=2) from exc
    except TrackerProblemError as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc
    if report.missing:
        _refuse_missing(report.missing)
    if report.untracked:
        labels = _labels(references, ids)
        _refuse_untracked([labels.get(posting_id, posting_id) for posting_id in report.untracked])
    console = terminal()
    render_changes(console, _headline(command, report, postings or []), report)
    if forward_only and report.unchanged and chosen is not None:
        console.print(
            "[dim]Quick commands never move a status back; "
            f"[bold]stage track ROW --status {chosen.value}[/bold] does.[/dim]"
        )


def _labels(references: list[str], ids: list[str]) -> dict[str, str]:
    return {
        posting_id: f"Row {reference}" if reference.isdecimal() else reference
        for reference, posting_id in zip(references, ids, strict=True)
    }


def _refuse_untracked(labels: list[str]) -> NoReturn:
    from stage.cli.render import terminal, truncate

    listed = truncate(", ".join(labels), 80)
    verb = "is" if len(labels) == 1 else "are"
    terminal().print(
        f"[red]{listed} {verb} not tracked yet, so there is no status to keep.[/red] "
        "Add [bold]--status saved[/bold], or use [bold]stage save[/bold]. Nothing was changed."
    )
    raise typer.Exit(code=1)


def _headline(command: str, report: "ChangeReport", typed: list[str]) -> str:
    count = len(report.changed)
    noun = "posting" if count == 1 else "postings"
    if not count:
        return "Nothing to change."
    if command == "save":
        return f"Saved {count} {noun}."
    if command == "applied":
        days = sorted({change.entry.on.isoformat() for change in report.changed})
        return f"Marked {count} {noun} applied on {', '.join(days)}."
    if command == "hide":
        pronoun = "it" if count == 1 else "them"
        return f"Hid {count} {noun}. stage untrack {' '.join(typed)} brings {pronoun} back."
    return f"Updated {count} {noun}."
