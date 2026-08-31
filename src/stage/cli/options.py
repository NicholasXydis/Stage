import sys
from collections.abc import Callable, Coroutine, Iterable
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from typer._click.types import IntRange, StringParamType
from typer.core import TyperGroup

if TYPE_CHECKING:
    from rich.console import Console

    from stage.domain import DiscoveryEvent, JobFilters

    ProgressCallback = Callable[[DiscoveryEvent], None]


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    import asyncio

    return asyncio.run(coroutine)


class InvalidOptionError(Exception):
    pass


class _Word(StringParamType):
    name = "text"

    def get_metavar(self, *_args: Any, **_kwargs: Any) -> str:
        return ""


class _Count(IntRange):
    def get_metavar(self, *_args: Any, **_kwargs: Any) -> str:
        return "N"

    def _describe_range(self) -> str:
        return ""

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        try:
            return super().convert(value, param, ctx)
        except typer.BadParameter:
            try:
                int(value)
            except (TypeError, ValueError):
                self.fail(f"{value} is not a whole number.", param, ctx)
            bounds = (
                f"{self.min} or more"
                if self.max is None
                else f"at most {self.max}"
                if self.min is None
                else f"between {self.min} and {self.max}"
            )
            self.fail(f"{value} is out of range; expected a number {bounds}.", param, ctx)


WORD = _Word()


def _count(minimum: int, maximum: int | None = None) -> _Count:
    return _Count(min=minimum, max=maximum)


def _parse_enum[E: StrEnum](value: str | None, enum: type[E], flag: str) -> E | None:
    if value is None:
        return None
    return _require_enum(value, enum, flag)


UNMATCHABLE_VALUES = {
    "--role": frozenset({"hardware"}),
    "--location": frozenset({"international"}),
}


def _require_enum[E: StrEnum](value: str, enum: type[E], flag: str) -> E:
    try:
        return enum(value)
    except ValueError as exc:
        hidden = UNMATCHABLE_VALUES.get(flag, frozenset())
        options = ", ".join(v for v in enum.__members__.values() if v not in hidden)
        raise InvalidOptionError(f"{flag} must be one of: {options}") from exc


class _Banner(TyperGroup):
    def format_help(self, ctx: Any, formatter: Any) -> None:
        from stage.banner import banner
        from stage.cli.render import terminal

        console = terminal()
        console.print(f"[bold cyan]{banner(console.width)}[/bold cyan]")
        super().format_help(ctx, formatter)


app = typer.Typer(
    cls=_Banner,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Aggregates CS internship postings into a local SQLite database.",
    epilog="Run stage help for a guide with examples.",
)
schedule_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run syncs automatically in the background",
)
app.add_typer(schedule_app, name="schedule", rich_help_panel="Keeping current")


def _tidy_builtin_option_help() -> None:
    from typer._click import decorators
    from typer.completion import _install_completion_placeholder_function
    from typer.models import OptionInfo

    for default in _install_completion_placeholder_function.__defaults__ or ():
        if not isinstance(default, OptionInfo) or not default.help:
            continue
        if "copy it" in default.help:
            default.help = "Show completion for the current shell"
        default.help = default.help.rstrip(".")

    build = decorators.help_option

    def without_a_trailing_stop(param_decls: list[str]) -> Any:
        decorate = build(param_decls)

        def apply(command: Any) -> Any:
            decorated = decorate(command)
            for param in decorated.params:
                text = getattr(param, "help", None)
                if text:
                    param.help = text.rstrip(".")  # type: ignore[attr-defined]
            return decorated

        return apply

    decorators.help_option = without_a_trailing_stop


_tidy_builtin_option_help()


def _version(value: bool) -> None:
    if not value:
        return
    from stage import __version__

    typer.echo(f"stage {__version__}")
    raise typer.Exit


@app.callback(invoke_without_command=True)
def _root(
    context: typer.Context,
    _version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the installed version and exit",
            callback=_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())
        raise typer.Exit


RegistryOption = Annotated[
    Path | None,
    typer.Option(
        "--registry", metavar="FILE", help="Use this company registry instead of the default"
    ),
]
DatabaseOption = Annotated[
    Path | None,
    typer.Option("--db", metavar="FILE", help="Use this SQLite database instead of the default"),
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
        metavar="PLACE",
        help="Filter by location: montreal, canada, usa, unknown",
    ),
]
TermOption = Annotated[
    str | None,
    typer.Option("--term", metavar="TERM", help="Filter by term, such as summer-2027"),
]
RoleOption = Annotated[
    str | None,
    typer.Option(
        "--role",
        metavar="ROLE",
        help=(
            "Filter by role: swe, security, data, ml-ai, quant, infra, embedded, "
            "general-cs, unknown"
        ),
    ),
]
LanguageOption = Annotated[
    str | None,
    typer.Option("--lang", metavar="LANG", help="Filter by language: en, fr, bilingual, unknown"),
]
SourceOption = Annotated[
    str | None,
    typer.Option(
        "--source", metavar="SOURCE", help="Filter by source adapter, such as greenhouse or lever"
    ),
]
CompanyOption = Annotated[
    str | None,
    typer.Option("--company", metavar="NAME", help="Filter by exact employer name"),
]
AllOption = Annotated[
    bool,
    typer.Option("--all", help="Show every match instead of the first page"),
]
NewOption = Annotated[
    bool,
    typer.Option("--new", help="Only postings that appeared since the sync before last"),
]
LastDaysOption = Annotated[
    int | None,
    typer.Option(
        "--last",
        metavar="DAYS",
        click_type=_count(0, 3650),
        help="Look back this many days instead of the default 14; 0 for no limit",
    ),
]
StaleDaysOption = Annotated[
    int | None,
    typer.Option(
        "--stale-days",
        metavar="DAYS",
        click_type=_count(1, 3650),
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
    from stage.cli.render import failure, terminal

    terminal().print(failure(exc))


def _print_missing(posting: str) -> None:
    from stage.cli.render import terminal

    terminal().print(_no_such_posting(posting))


def _validated_term(value: str | None) -> str | None:
    if value is None:
        return None
    from stage.domain import UNKNOWN_TERM
    from stage.lexicon import SEASONS

    head, _, year = value.lower().partition("-")
    if value.lower() == UNKNOWN_TERM or (head in SEASONS and year.isdigit() and len(year) == 4):
        return value.lower()
    seasons = ", ".join(f"{season}-2027" for season in SEASONS)
    raise InvalidOptionError(f"--term must look like {seasons}, or {UNKNOWN_TERM}")


def _validated_source(value: str | None) -> str | None:
    if value is None:
        return None
    from stage.sources import get_adapters

    names = sorted(get_adapters())
    if value in names:
        return value
    raise InvalidOptionError(_did_you_mean(value, names, "source"))


def _filters(
    *,
    location: str | None,
    term: str | None,
    role: str | None,
    language: str | None,
    source: str | None,
    company: str | None,
    limit: int | None,
) -> "JobFilters":
    from stage.domain import (
        JobFilters,
        Language,
        LocationBucket,
        RoleCategory,
    )

    return JobFilters(
        location=_parse_enum(location, LocationBucket, "--location"),
        term=_validated_term(term),
        role=_parse_enum(role, RoleCategory, "--role"),
        language=_parse_enum(language, Language, "--lang"),
        source=_validated_source(source),
        company=company,
        limit=limit,
    )


async def _resolve_posting(reference: str, repository: Any) -> str:
    from stage.cli.selection import resolve

    if not reference.isdigit():
        return reference
    return resolve(int(reference), await repository.last_sync_at())


def _did_you_mean(value: str, choices: "Iterable[str]", label: str = "command") -> str:
    from difflib import get_close_matches

    options = list(choices)
    near = get_close_matches(value.lower(), options, n=1, cutoff=0.6)
    if near:
        return f"Unknown {label} {value!r}. Did you mean {near[0]!r}?"
    return f"Unknown {label} {value!r}. Choose from: {', '.join(options)}"


def _needs_a_row(command: str) -> str:
    return (
        f"[red]stage {command} needs a row number or a posting id.[/red] Run "
        "[bold]stage list[/bold] or [bold]stage search[/bold] first, then "
        f"[bold]stage {command} 1[/bold] acts on the first row."
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
    show_all: bool = False,
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
    adopted = outcome.adopted if show_all else outcome.adopted[:20]
    for row in adopted:
        console.print(plain(f"  + {row.company.name} — {row.job_count} job(s)"))
    if len(outcome.adopted) > len(adopted):
        console.print(
            plain(f"  … and {len(outcome.adopted) - len(adopted)} more; --all lists every row")
        )
    if outcome.review:
        console.print(
            plain("Boards with postings whose platform publishes no name. Decide these by hand:")
        )
        review = outcome.review if show_all else outcome.review[:40]
        for candidate in review:
            mark = "slug is distinctive" if candidate.distinctive else "slug is generic, check it"
            console.print(
                plain(
                    f"  ? {candidate.company} ({candidate.label}) — "
                    f"{candidate.job_count} job(s), {mark}"
                )
            )
        if len(outcome.review) > len(review):
            console.print(
                plain(f"  … and {len(outcome.review) - len(review)} more; --all lists every row")
            )
    refused = outcome.refused if show_all else outcome.refused[:10]
    for company, board, reason in refused:
        console.print(plain(f"  - {company} ({board}): {reason}"))
    if len(outcome.refused) > len(refused):
        console.print(
            plain(f"  … and {len(outcome.refused) - len(refused)} more; --all lists every row")
        )
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
    "Stage aggregates CS internship postings into a local SQLite database.\n"
    "\nStart here:\n"
    "  stage sync                          Fetch and save current postings\n"
    "  stage tui                           Browse everything in a full screen\n"
    "  stage list                          Recent open postings, newest first\n"
    "  stage list --new                    Only what appeared since the last sync\n"
    '  stage search "machine learning"     Search titles, employers, bodies\n'
    "  stage show 3                        Inspect row 3 of the last listing\n"
    "  stage open 3 5 9                    Open those rows in a browser\n"
    "  stage export --format csv           Save every match to a file\n"
    "\nFilters, on list, search, and export:\n"
    "  stage list --role swe --location montreal\n"
    '  stage search "python" --term summer-2027 --lang en\n'
    "  stage export --format csv --role ml-ai --last 90 --all\n"
    "  stage list --last 0                 Ignore the 14-day window entirely\n"
    "  [--role --location --term --lang --source --company --new --all --last]\n"
    "\nHealth and maintenance:\n"
    "  stage doctor                        Database and source health\n"
    "  stage stats                         Sync history and totals\n"
    "  stage schedule enable               Sync in the background\n"
    "  stage schedule notify URL           Post new postings to Discord\n"
    "  stage quarantine                    Review rejected postings\n"
    "  stage coverage --unregistered       Employers seen but not tracked\n"
    "\nDiscovery:\n"
    "  stage discover --url URL            Read the platform from a careers page\n"
    "  stage discover NAME                 Find a company board by name\n"
    "\nLearn any command:\n"
    "  stage --help                        Every command, grouped\n"
    "  stage help COMMAND                  Every option, and what each takes\n"
    "  stage --install-completion          Tab-complete, after a new terminal\n"
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
        stale = report.stale_interpreter[index] if index < len(report.stale_interpreter) else ""
        if enabled and stale:
            console.print(
                f"    [red]interpreter is missing: {stale}[/red] — this run will fail; "
                "re-run stage schedule enable"
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


@app.command(
    "help",
    help="A guided tour, or everything one command can do",
    rich_help_panel="Everyday",
)
def show_help(
    context: typer.Context,
    topic: Annotated[
        str | None,
        typer.Argument(metavar="COMMAND", click_type=WORD, help="Command name to explain"),
    ] = None,
) -> None:
    root = context.parent or context
    if topic is not None:
        from typer.core import TyperGroup

        group = root.command
        if not isinstance(group, TyperGroup):
            raise typer.BadParameter(f"Unknown command {topic!r}", param_hint="topic")
        command = group.commands.get(topic)
        if command is None:
            raise typer.BadParameter(_did_you_mean(topic, group.commands), param_hint="topic")
        from typer._click import Context

        typer.echo(command.get_help(Context(command, info_name=topic, parent=root)))
        return
    _print_guide()


def _print_guide() -> None:
    from stage.banner import banner
    from stage.cli.render import terminal

    console = terminal()
    console.print(f"[bold cyan]{banner(console.width)}[/bold cyan]")
    console.print()
    typer.echo(_HELP_GUIDE)


def main() -> None:
    import sqlite3

    from stage.cli.serialize import configure_terminal_output
    from stage.storage.migrations import SchemaVersionError

    configure_terminal_output(sys.stdout, sys.platform)
    try:
        app()
    except SchemaVersionError as exc:
        typer.echo(str(exc), err=True)
        raise SystemExit(2) from None
    except sqlite3.DatabaseError as exc:
        typer.echo(
            f"That database cannot be read ({exc}). Point --db at a Stage database, "
            "or remove the file to start a new one.",
            err=True,
        )
        raise SystemExit(2) from None
    except OSError as exc:
        target = getattr(exc, "filename", None)
        location = f" at {target}" if target else ""
        typer.echo(f"Cannot use that location{location}: {exc.strerror or exc}", err=True)
        raise SystemExit(2) from None
