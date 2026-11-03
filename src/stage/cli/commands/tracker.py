from collections.abc import Coroutine
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from stage.cli.options import (
    WORD,
    DatabaseOption,
    JsonOption,
    _database,
    _needs_a_row,
    _no_such_posting,
    _print_error,
    _refuse,
    _refusing,
    _resolve_posting,
    _with_db,
    app,
    output_path,
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
        "--on", metavar="DATE", help="The day it happened, like 2026-10-20, today by default"
    ),
]
NoteOption = Annotated[
    str | None,
    typer.Option(
        "--note", metavar="TEXT", help="A short note kept on this machine (empty clears it)"
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


@app.command(help="Hide postings from every list until stage untrack", rich_help_panel=PANEL)
def hide(postings: RowsArgument = None, db: DatabaseOption = None) -> None:
    from stage.domain.tracker import ApplicationStatus

    _change("hide", postings, ApplicationStatus.HIDDEN, None, None, db)


@app.command(help="Set the status, date or note of postings", rich_help_panel=PANEL)
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

    with _refusing(TrackerError):
        chosen = parse_status(status) if status is not None else None
    if chosen is None and on is None and note is None:
        _refuse(
            "[red]stage track needs --status, --on or --note.[/red] For example "
            "[bold]stage track 3 --status interviewing[/bold]."
        )
    _change("track", postings, chosen, on, note, db)


@app.command(help="Stop tracking postings, which also unhides them", rich_help_panel=PANEL)
def untrack(postings: RowsArgument = None, db: DatabaseOption = None) -> None:
    from stage.cli.render import terminal
    from stage.cli.render.tracker import render_removed
    from stage.services.tracker import RemoveReport, remove_tracked
    from stage.storage import open_repository

    references = _references("untrack", postings)

    async def run() -> tuple[list[str], RemoveReport]:
        async with open_repository(_database(db)) as repository:
            ids = [await _resolve_posting(reference, repository) for reference in references]
            return ids, await remove_tracked(repository, ids)

    ids, report = _run_tracked(run())
    if report.missing:
        _refuse_missing(report.missing)
    labels = _labels(references, ids)
    named = tuple(labels.get(posting_id, posting_id) for posting_id in report.not_tracked)
    render_removed(terminal(), replace(report, not_tracked=named))


@app.command(help="Your tracked postings, latest change first", rich_help_panel=PANEL)
def tracker(
    status: Annotated[
        list[str] | None,
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
        Path | None,
        typer.Option("--out", metavar="FILE", parser=output_path, help="Where --export writes"),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing --out file")] = False,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.options import InvalidOptionError, _one
    from stage.cli.render import plain
    from stage.domain import ExportFormat
    from stage.domain.tracker import TrackerError, parse_status
    from stage.services.tracker import FORMATS, TrackerProblemError, tracker_listing

    if stats and (export or status):
        _refuse(
            "[red]--stats counts every tracked posting, so use it without --status or "
            "--export.[/red]"
        )
    if export and as_json:
        _refuse("[red]--export writes a file, so use it without --json.[/red]")
    if (out or force) and not export:
        _refuse("[red]--out and --force only apply with --export.[/red]")
    names = [choice.value for choice in FORMATS]
    if export is not None and export.strip().lower() not in names:
        _refuse(plain(f'The tracker exports csv, json or md, not "{export[:20]}".', "red"))
    with _refusing(TrackerError, InvalidOptionError):
        wanted = _one(status, "--status", mine=False)
        chosen = parse_status(wanted) if wanted is not None else None
    fmt = None if export is None else ExportFormat(export.strip().lower())

    with _refusing(TrackerProblemError, code=1, soft_wrap=True):
        listing = _with_db(db, lambda repository: tracker_listing(repository, chosen))

    if stats:
        _stats(listing, as_json)
        return
    if fmt is not None:
        _export(listing, fmt, out, force)
        return

    from stage.cli.selection import remember

    if as_json:
        from stage.cli.serialize import emit
        from stage.services.tracker import tracker_json

        emit(tracker_json(listing.rows))
        return
    numbered = remember(tuple(row.entry.posting_id for row in listing.rows), listing.last_sync_at)
    from stage.cli.render import terminal
    from stage.cli.render.tracker import render_tracker

    render_tracker(terminal(), listing, status=chosen, numbered=numbered)


def _stats(listing: "TrackerListing", as_json: bool) -> None:
    if as_json:
        from stage.cli.serialize import emit
        from stage.domain import dump
        from stage.services.tracker import stats_payload

        emit(dump(stats_payload(listing.stats)))
        return
    from stage.cli.render import terminal
    from stage.cli.render.tracker import render_tracker_stats

    render_tracker_stats(terminal(), listing.stats)


def _export(listing: "TrackerListing", fmt: "ExportFormat", out: Path | None, force: bool) -> None:
    from stage.cli.render import plain, terminal
    from stage.domain.text import counted
    from stage.services.export import ExportError
    from stage.services.tracker import export_tracker

    with _refusing(ExportError, soft_wrap=True):
        written = export_tracker(listing.rows, fmt, destination=out, force=force)
    terminal().print(
        plain(f"Wrote {counted(len(listing.rows), 'tracked posting')} to {written}"),
        soft_wrap=True,
    )


def _run_tracked[T](coroutine: Coroutine[Any, Any, T]) -> T:
    from stage.cli.selection import StaleSelectionError
    from stage.services.tracker import TrackerProblemError

    with _refusing(StaleSelectionError), _refusing(TrackerProblemError, code=1, soft_wrap=True):
        return run_async(coroutine)


def _references(command: str, postings: list[str] | None) -> list[str]:
    from stage.cli.selection import BadRangeError, expand

    if not postings:
        _refuse(_needs_a_row(command))
    with _refusing(BadRangeError):
        return expand(postings)


def _refuse_missing(missing: tuple[str, ...]) -> NoReturn:

    for reference in missing:
        _print_error(_no_such_posting(reference))
    _print_error("[red]Nothing was changed.[/red]")
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

    from stage.cli.render import terminal
    from stage.cli.render.tracker import render_changes
    from stage.domain.tracker import ApplicationStatus, TrackerError, parse_day, parse_note
    from stage.services.tracker import ChangeReport, record_status
    from stage.storage import open_repository

    references = _references(command, postings)
    chosen = ApplicationStatus(status) if status is not None else None
    with _refusing(TrackerError):
        on = parse_day(on_text, datetime.now().astimezone().date()) if on_text is not None else None
        note = parse_note(note_text) if note_text is not None else None

    async def run() -> tuple[list[str], ChangeReport]:
        async with open_repository(_database(db)) as repository:
            ids = [await _resolve_posting(reference, repository) for reference in references]
            report = await record_status(
                repository, ids, chosen, on=on, note=note, forward_only=forward_only
            )
            return ids, report

    ids, report = _run_tracked(run())
    if report.missing:
        _refuse_missing(report.missing)
    if report.untracked:
        labels = _labels(references, ids)
        _refuse_untracked([labels.get(posting_id, posting_id) for posting_id in report.untracked])
    console = terminal()
    render_changes(console, _headline(command, report, postings or []), report)
    if forward_only and report.unchanged and chosen is not None:
        console.print(
            "[dim]Quick commands never move a status back, but "
            f"[bold]stage track ROW --status {chosen.value}[/bold] does.[/dim]"
        )


def _labels(references: list[str], ids: list[str]) -> dict[str, str]:
    return {
        posting_id: f"Row {reference}" if reference.isdecimal() else reference
        for reference, posting_id in zip(references, ids, strict=True)
    }


def _refuse_untracked(labels: list[str]) -> NoReturn:
    from stage.cli.render import truncate

    listed = truncate(", ".join(labels), 80)
    verb = "is" if len(labels) == 1 else "are"
    _print_error(
        f"[red]{listed} {verb} not tracked yet, so there is no status to keep.[/red] "
        "Add [bold]--status saved[/bold], or use [bold]stage save[/bold]. Nothing was changed."
    )
    raise typer.Exit(code=1)


def _headline(command: str, report: "ChangeReport", typed: list[str]) -> str:
    count = len(report.changed)
    noun = "posting" if count == 1 else "postings"
    if not count:
        return "Nothing to change."
    if all(
        change.previous is not None
        and change.before is change.entry.status
        and change.previous.on == change.entry.on
        for change in report.changed
    ):
        return f"Updated the note on {count} {noun}."
    if command == "save":
        return f"Saved {count} {noun}."
    if command == "applied":
        days = sorted({change.entry.on.isoformat() for change in report.changed})
        return f"Marked {count} {noun} applied on {', '.join(days)}."
    if command == "hide":
        pronoun = "it" if count == 1 else "them"
        if any(reference.replace("-", "").isdigit() for reference in typed):
            return (
                f"Hid {count} {noun}. To bring {pronoun} back, run stage tracker --status "
                "hidden, then stage untrack with that list's rows."
            )
        return f"Hid {count} {noun}. stage untrack {' '.join(typed)} brings {pronoun} back."
    return f"Updated {count} {noun}."
