import os
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from typer._click.types import IntRange, StringParamType
from typer.core import TyperGroup

if TYPE_CHECKING:
    from rich.text import Text

    from stage.companies import PersonalBoards
    from stage.domain import JobFilters
    from stage.domain.preferences import Preferences
    from stage.storage import AsyncRepository

STOPPED = "Stopped before it finished."


def run_async[T](coroutine: Coroutine[Any, Any, T], *, stopped: str = STOPPED) -> T:
    import asyncio

    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        _print_error(f"[yellow]{stopped}[/yellow]")
        raise typer.Exit(code=130) from None


def ask(question: str, *, default: str, show_default: bool, stopped: str) -> str:
    try:
        return str(typer.prompt(question, default=default, show_default=show_default))
    except typer.Abort as exc:
        interrupted = isinstance(exc.__context__, KeyboardInterrupt)
        typer.echo(err=True)
        _print_error(f"[yellow]{stopped}[/yellow]")
        raise typer.Exit(code=130 if interrupted else 1) from None


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
                f"{self.min:,} or more"
                if self.max is None or self.max >= MAX_COUNT
                else f"between {self.min:,} and {self.max:,}"
            )
            self.fail(f"{value} is out of range. Give a number {bounds}.", param, ctx)


WORD = _Word()
MAX_COUNT = 1_000_000_000


def output_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if raw.strip() in ("", "-"):
        raise typer.BadParameter("name a file or a folder. Stage does not write exports to stdout.")
    if raw.endswith(("/", os.sep)) and not path.is_dir():
        raise typer.BadParameter(f"the folder {path} does not exist. Create it first.")
    return path


def database_file(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not raw.strip() or path.is_dir():
        raise typer.BadParameter("name a database file, not a folder.")
    if not path.parent.is_dir():
        raise typer.BadParameter(f"the folder {path.parent} does not exist. Create it first.")
    return path


def _count(minimum: int, maximum: int | None = MAX_COUNT) -> _Count:
    return _Count(min=minimum, max=maximum)


def _parse_enum[E: StrEnum](value: str | None, enum: Iterable[E], flag: str) -> E | None:
    if value is None:
        return None
    return _require_enum(value, enum, flag)


UNMATCHABLE_VALUES = {
    "--role": frozenset({"hardware"}),
    "--location": frozenset({"international"}),
}
_NOUNS = {
    "--lang": "language",
    "--exclude": "platform",
    "--expect-size": "size",
    "--export": "format",
}


def _require_enum[E: StrEnum](value: str, enum: Iterable[E], flag: str) -> E:
    choices = list(enum)
    for choice in choices:
        if choice.value == value.strip().lower():
            return choice
    hidden = UNMATCHABLE_VALUES.get(flag, frozenset())
    shown = [choice.value for choice in choices if choice.value not in hidden]
    if not value.strip():
        raise InvalidOptionError(f"{flag} needs a value. Choose from: {', '.join(shown)}.")
    raise InvalidOptionError(_unknown(_NOUNS.get(flag, flag.lstrip("-")), value, shown))


def _unknown(noun: str, value: str, choices: Iterable[str]) -> str:
    from stage.domain.text import unknown_choice

    return unknown_choice(noun, value, list(choices))


class _Banner(TyperGroup):
    def format_help(self, ctx: Any, formatter: Any) -> None:
        from stage.cli.render import splash, terminal

        console = terminal()
        splash(console)
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
    _INVOKED[:] = [context.invoked_subcommand or ""]
    context.call_on_close(_INVOKED.clear)
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())
        raise typer.Exit


RegistryOption = Annotated[
    Path | None,
    typer.Option(
        "--registry", metavar="DIR", help="Use this company registry folder instead of the default"
    ),
]
DatabaseOption = Annotated[
    Path | None,
    typer.Option(
        "--db",
        metavar="FILE",
        parser=database_file,
        help="Use this SQLite database instead of the default",
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")]
RepairOption = Annotated[
    bool,
    typer.Option("--repair", help="Fix the integrity findings that can be fixed safely"),
]
LocationOption = Annotated[
    list[str] | None,
    typer.Option(
        "--location",
        metavar="PLACE",
        help="Filter by location: montreal, canada (the rest of Canada), usa, unknown",
    ),
]
TermOption = Annotated[
    list[str] | None,
    typer.Option("--term", metavar="TERM", help="Filter by term, such as summer-2027"),
]
RoleOption = Annotated[
    list[str] | None,
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
    list[str] | None,
    typer.Option("--lang", metavar="LANG", help="Filter by language: en, fr, bilingual, unknown"),
]
SourceOption = Annotated[
    list[str] | None,
    typer.Option(
        "--source", metavar="SOURCE", help="Filter by source, such as greenhouse, lever or simplify"
    ),
]
CompanyOption = Annotated[
    list[str] | None,
    typer.Option("--company", metavar="NAME", help="One employer, in any case"),
]
AllOption = Annotated[
    bool,
    typer.Option("--all", help="Show every match instead of the first page"),
]
ExportAllOption = Annotated[
    bool,
    typer.Option("--all", help="Export every match even when --limit is set"),
]
NewOption = Annotated[
    bool,
    typer.Option("--new", help="Only postings the last sync brought in"),
]
UntrackedOption = Annotated[
    bool,
    typer.Option("--untracked", help="Only postings you have not saved, applied to or hidden"),
]
MineOption = Annotated[
    bool,
    typer.Option("--mine", help="Only postings that fit the preferences saved with stage prefs"),
]
LastDaysOption = Annotated[
    int | None,
    typer.Option(
        "--last",
        metavar="DAYS",
        click_type=_count(0, 3650),
        help="Only postings first seen in the last DAYS days (Stage keeps 14, the default)",
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


_OPENED: list[Path] = []
_INVOKED: list[str] = []


READS_ONLY = frozenset(
    {
        "list",
        "search",
        "show",
        "open",
        "export",
        "quarantine",
        "stats",
        "sources",
        "coverage",
        "doctor",
        "tui",
    }
)


def _database(explicit: Path | None) -> Path:
    from stage.paths import database_path

    path = explicit.expanduser() if explicit else database_path()
    if explicit is not None and not path.exists() and _command_name() in READS_ONLY:
        from stage.storage.problems import no_database

        _print_error(no_database(path), soft_wrap=True)
        raise typer.Exit(code=2)
    _OPENED[:] = [path]
    return path


def _command_name() -> str:
    return _INVOKED[0] if _INVOKED else ""


async def _opened[T](db: Path | None, use: "Callable[[AsyncRepository], Awaitable[T]]") -> T:
    from stage.storage import open_repository

    async with open_repository(_database(db)) as repository:
        return await use(repository)


def _with_db[T](db: Path | None, use: "Callable[[AsyncRepository], Awaitable[T]]") -> T:
    return run_async(_opened(db, use))


def _lock_path(explicit: Path | None) -> Path:
    from stage.runlock import run_lock_path

    return run_lock_path(_database(explicit))


def _print_error(message: "str | Text", *, soft_wrap: bool = False) -> None:
    from stage.cli.render import terminal

    terminal(stderr=True).print(message, soft_wrap=soft_wrap)


def _print_failure(exc: BaseException, *, soft_wrap: bool = False) -> None:
    from stage.cli.render import failure

    _print_error(failure(exc), soft_wrap=soft_wrap)


def _warn_unread_boards(personal: "PersonalBoards") -> None:
    if not personal.problem:
        return
    from stage.cli.render import plain

    _print_error(
        plain(f"Your board list is ignored until it can be read: {personal.problem}", "yellow")
    )


def _print_missing(posting: str) -> None:
    _print_error(_no_such_posting(posting))


def _validated_term(value: str | None, *, offer_unknown: bool = True) -> str | None:
    if value is None:
        return None
    from stage.domain import UNKNOWN_TERM
    from stage.lexicon import SEASONS

    head, _, year = value.lower().partition("-")
    if value.lower() == UNKNOWN_TERM or (head in SEASONS and year.isdigit() and len(year) == 4):
        return value.lower()
    from stage.domain.text import truncate

    examples = [f"{season}-2027" for season in SEASONS]
    if offer_unknown:
        examples.append(UNKNOWN_TERM)
    raise InvalidOptionError(
        f'Unknown term "{truncate(value, 60)}". Terms look like {", ".join(examples[:-1])} '
        f"or {examples[-1]}."
    )


def _validated_source(value: str | None) -> str | None:
    if value is None:
        return None
    from stage.sources import get_adapters, get_feeds

    names = sorted({*get_adapters(), *get_feeds()})
    if value.strip().lower() in names:
        return value.strip().lower()
    raise InvalidOptionError(_unknown("source", value, names))


def _filters(
    *,
    location: list[str] | None,
    term: list[str] | None,
    role: list[str] | None,
    language: list[str] | None,
    source: list[str] | None,
    company: list[str] | None,
    limit: int | None,
    mine: bool = False,
    as_json: bool = False,
) -> "tuple[JobFilters, Preferences | None]":
    from stage.domain import JobFilters, Language, LocationBucket, RoleCategory

    filters = JobFilters(
        location=_parse_enum(_one(location, "--location"), LocationBucket, "--location"),
        term=_validated_term(_one(term, "--term")),
        role=_parse_enum(_one(role, "--role"), RoleCategory, "--role"),
        language=_parse_enum(_one(language, "--lang"), Language, "--lang"),
        source=_validated_source(_one(source, "--source")),
        company=_one(company, "--company"),
        limit=limit,
    )
    return _mine(filters, as_json) if mine else (filters, None)


def _one(values: list[str] | None, flag: str, *, mine: bool = True) -> str | None:
    if not values:
        return None
    if any(not value.strip() for value in values):
        raise InvalidOptionError(f"{flag} needs a value. Leave the flag out to match everything.")
    several = len({" ".join(value.casefold().split()) for value in values}) > 1
    if several and not mine:
        raise InvalidOptionError(f"{flag} takes one value. Run the command once for each.")
    if several:
        advice = (
            "Follow several with stage prefs --follow"
            if flag == "--company"
            else "Save several with stage prefs"
        )
        raise InvalidOptionError(f"{flag} takes one value. {advice}, then add --mine.")
    return values[0]


def _mine(filters: "JobFilters", as_json: bool) -> "tuple[JobFilters, Preferences]":
    from stage.services.preferences import load_preferences

    loaded = load_preferences()
    if loaded.damaged:
        _refuse(
            "[red]Your saved preferences could not be read.[/red] Run [bold]stage prefs[/bold] "
            "to save them again, or [bold]stage prefs --clear[/bold]."
        )
    for problem in loaded.problems:
        _print_warning(problem, as_json)
    preferences = loaded.preferences
    if preferences.is_empty:
        _refuse(
            "[red]No preferences saved yet.[/red] Run [bold]stage prefs[/bold] to choose "
            "places, roles and terms."
        )
    if not preferences.narrows:
        held = "followed employers" if preferences.follow else "a note about you"
        _refuse(
            f"[red]Your saved preferences only hold {held}, so --mine would show "
            "everything.[/red] Add places, roles or terms with [bold]stage prefs[/bold]."
        )
    return preferences.apply(filters), preferences


def _refuse(message: "str | Text") -> NoReturn:
    _print_error(message)
    raise typer.Exit(code=2)


@contextmanager
def _refusing(*errors: type[Exception], code: int = 2, soft_wrap: bool = False) -> Iterator[None]:
    try:
        yield
    except errors as exc:
        _print_failure(exc, soft_wrap=soft_wrap)
        raise typer.Exit(code=code) from exc


def _print_warning(message: str, as_json: bool) -> None:
    from stage.cli.render import error_text, plain, terminal

    if as_json:
        from stage.cli.serialize import fail

        fail(error_text(message))
        return
    terminal().print(plain(error_text(message), "yellow"))


async def _resolve_posting(reference: str, repository: Any) -> str:
    from stage.cli.selection import resolve

    if not reference.isdecimal():
        return reference
    return resolve(int(reference), await repository.last_sync_at())


def _needs_a_row(command: str) -> str:
    return (
        f"[red]stage {command} needs a row number or a posting id.[/red] Run "
        "[bold]stage list[/bold] or [bold]stage search[/bold] first, then "
        f"[bold]stage {command} 1[/bold] acts on the first row."
    )


def _no_such_posting(posting: str) -> str:
    from stage.cli.render import quoted

    if posting.isdecimal():
        return (
            f"[red]Row {posting} of the last listing is no longer stored.[/red] Its posting "
            "was purged or rejected since. Run [bold]stage list[/bold] again."
        )
    return (
        f"[red]No posting has the id {quoted(posting, 80)}.[/red] Ids look like "
        "[bold]greenhouse:acme:1234[/bold]. Find one with [bold]stage list --json[/bold] "
        "or [bold]stage search[/bold]."
    )


_HELP_GUIDE = (
    "Stage aggregates CS internship postings into a local SQLite database.\n"
    "\nStart here:\n"
    "  stage sync                          Fetch and save current postings\n"
    "  stage tui                           Browse everything in a full screen\n"
    "  stage list                          Recent open postings, newest first\n"
    "  stage list --new                    Only what the last sync brought in\n"
    '  stage search "machine learning"     Search titles, employers, descriptions\n'
    "  stage show 3                        Inspect row 3 of the last listing\n"
    "  stage open 1-5 9                    Open a range of rows in a browser\n"
    "  stage export --format csv           Save every match to a file\n"
    "\nFilters, on list, search, and export:\n"
    "  stage list --role swe --last 3      Filters combine\n"
    "  stage search python --lang en       English postings only\n"
    "  stage list --all                    Every match, not just the first page\n"
    "  stage list --last 1                 Only the last day\n"
    "  stage prefs                         Save the places, roles, terms you want\n"
    "  stage list --mine                   Only postings that fit them\n"
    "\n"
    "  [--role --location --term --lang --source --company --mine --untracked --last]\n"
    "\nYour applications:\n"
    "  stage save 3                        Keep a posting to come back to\n"
    "  stage applied 1-4                   Mark postings as applied today\n"
    "  stage hide 7                        Hide a posting from every list\n"
    "  stage tracker                       Everything you saved or applied to\n"
    "  stage list --untracked              Only postings you have not acted on\n"
    "\nAI apps:\n"
    "  stage connect                       Let Claude, Cursor and others use Stage\n"
    "  stage connect --status              See which apps are connected\n"
    "  stage connect --allow-actions       Let them also open, export and sync\n"
    "  stage disconnect cursor             Take Stage out of one app\n"
    "\nHealth and maintenance:\n"
    "  stage doctor                        Database and source health\n"
    "  stage stats                         Sync history and totals\n"
    "  stage schedule enable               Sync in the background\n"
    "  stage schedule notify URL           Post new postings to Discord\n"
    "  stage quarantine                    Review rejected postings\n"
    "  stage coverage --unregistered       Employers seen but not tracked\n"
    "\nDiscovery:\n"
    "  stage discover --url URL            Read the platform from a careers page link\n"
    "  stage discover NAME                 Find a company board by name\n"
    "\nLearn any command:\n"
    "  stage --help                        Every command, grouped\n"
    "  stage help COMMAND                  Every option, and what each takes\n"
    "  stage --install-completion          Tab-complete, after a new terminal\n"
)


@app.command(
    "help",
    help="A guided tour, or everything one command can do",
    rich_help_panel="Everyday",
)
def show_help(
    context: typer.Context,
    topic: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="COMMAND",
            click_type=WORD,
            help="Command to explain, such as sync or schedule status",
        ),
    ] = None,
) -> None:
    root = context.parent or context
    if topic:
        from typer._click import Context
        from typer.core import TyperGroup

        group: Any = root.command
        shown = root
        for depth, name in enumerate(topic):
            if not isinstance(group, TyperGroup):
                above = " ".join(topic[:depth])
                raise typer.BadParameter(
                    f"stage {above} has no subcommands. Run stage help {above}.",
                    param_hint="COMMAND",
                )
            command = group.commands.get(name)
            if command is None:
                raise typer.BadParameter(
                    _unknown("command", name, group.commands), param_hint="COMMAND"
                )
            shown = Context(command, info_name=name, parent=shown)
            group = command
        typer.echo(shown.command.get_help(shown))
        return
    _print_guide()


def _print_guide() -> None:
    from stage.cli.render import splash, terminal

    console = terminal()
    splash(console)
    console.print()
    typer.echo(_HELP_GUIDE)


def database_problem(exc: Exception, path: Path | None = None) -> str:
    from stage.storage.problems import database_problem as worded

    return worded(exc, path or (_OPENED[-1] if _OPENED else _database(None)))


def _cannot_run(exc: Exception) -> str | None:
    import sqlite3

    from stage.lexicon import LexiconError
    from stage.storage.migrations import SchemaVersionError

    if isinstance(exc, SchemaVersionError):
        from stage.cli.render import error_text

        return error_text(str(exc))
    if isinstance(exc, LexiconError):
        hint = (
            " Unset STAGE_LEXICON to use the built-in lists."
            if "STAGE_LEXICON" in os.environ
            else ""
        )
        return f"Stage could not read its word lists: {exc}.{hint}"
    if isinstance(exc, sqlite3.DatabaseError):
        return database_problem(exc)
    if isinstance(exc, OSError):
        target = getattr(exc, "filename", None)
        where = f"use {target}" if target else "reach a file it needs"
        return (
            f"Stage could not {where} ({exc.strerror or exc}). Check that the path exists and "
            "that you can write there."
        )
    return None


def main() -> None:
    if sys.platform == "win32":
        from stage.cli.serialize import configure_terminal_output

        configure_terminal_output(sys.stdout, sys.platform)
        configure_terminal_output(sys.stderr, sys.platform)
    try:
        app()
    except Exception as exc:
        said = _cannot_run(exc)
        if said is None:
            raise
        typer.echo(said, err=True)
        raise SystemExit(2) from None
