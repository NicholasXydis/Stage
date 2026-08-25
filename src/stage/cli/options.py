import sys
from collections.abc import Callable, Coroutine
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from rich.console import Console

    from stage.domain import DiscoveryEvent, JobFilters

    ProgressCallback = Callable[[DiscoveryEvent], None]


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
RepairOption = Annotated[
    bool,
    typer.Option("--repair", help="Fix the integrity findings that can be fixed safely"),
]
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


def _no_such_posting(posting: str) -> str:
    from stage.cli.render import quoted

    return (
        f"[red]No posting with id {quoted(posting, 80)}.[/red] Ids look like "
        "[bold]greenhouse:acme:1234[/bold] — find one with [bold]stage list --json[/bold] "
        "or [bold]stage search[/bold]."
    )


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
    progress: "ProgressCallback | None",
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
                if progress is not None:
                    progress(event)
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
    "  stage schedule enable              Enable six-hourly syncs and weekly discovery\n"
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
    for index, (action, enabled, installed) in enumerate(report.actions):
        state = "enabled" if enabled else "disabled"
        console.print(f"  {action.label}: {state} — {action.cadence} at {action.time} local time")
        needs_update = index < len(report.needs_update) and report.needs_update[index]
        if enabled and not needs_update and not action.cadence.startswith(installed):
            needs_update = True
        if enabled and needs_update:
            console.print(
                "    [yellow]needs a scheduler update — run stage schedule enable[/yellow]"
            )
        run = report.states[index] if index < len(report.states) else None
        _render_scheduled_run(console, run)
    console.print(f"Logs: {report.log_dir}")


def _render_scheduled_run(console: Any, run: dict[str, object] | None) -> None:
    if run is None:
        return
    phase = run.get("phase")
    if phase == "finished":
        outcome = run.get("outcome")
        outcome_name = outcome if isinstance(outcome, str) else ""
        message = {
            "success": "completed successfully",
            "partial": "completed with attention",
            "failure": "failed",
        }.get(outcome_name, "finished")
        console.print(f"    [green]{message}[/green] {_schedule_time(run.get('finished_at'))}")
    elif phase == "blocked":
        console.print(f"    [yellow]another run holds the lock[/yellow] — {run.get('error', '')}")
    elif phase == "unresponsive":
        console.print(
            "    [yellow]run is unresponsive[/yellow] — "
            f"last heartbeat {_schedule_time(run.get('heartbeat_at'))}"
        )
    elif phase == "waiting":
        expected = _schedule_time(run.get("starts_after"))
        console.print(f"    [cyan]waiting to start[/cyan] — expected {expected}")
    elif isinstance(phase, str) and phase in {"syncing", "discovering", "checking"}:
        label = {
            "syncing": "syncing",
            "discovering": "discovering",
            "checking": "checking health",
        }[phase]
        console.print(f"    [cyan]{label}[/cyan] — started {_schedule_time(run.get('started_at'))}")
    else:
        return
    progress = run.get("progress")
    if isinstance(progress, dict) and phase not in {"blocked", "unresponsive"}:
        complete = progress.get("complete", 0)
        total = progress.get("total", 0)
        if complete or total:
            source = progress.get("source")
            company = progress.get("company")
            detail = " / ".join(str(value) for value in (source, company) if value)
            suffix = f" — {detail}" if detail else ""
            console.print(f"      progress: {complete}/{total}{suffix}")
    if phase == "finished" and isinstance(run.get("exit_code"), int):
        console.print(f"      exit code: {run['exit_code']}")


def _schedule_time(value: object) -> str:
    if not isinstance(value, str):
        return "unknown time"
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return value


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
