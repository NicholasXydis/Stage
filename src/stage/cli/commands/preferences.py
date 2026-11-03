import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from stage.cli.options import (
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    _database,
    _print_error,
    _print_failure,
    _print_warning,
    _refuse,
    _validated_term,
    _with_db,
    app,
    ask,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from stage.domain.preferences import Preferences

KEEP_OR_CLEAR = "Press Enter to keep a value, or type - to clear it."
SHOWN_IN_FULL = 72


@app.command(
    "prefs",
    help="Save the places, roles and terms that --mine uses",
    rich_help_panel="Everyday",
)
def prefs(
    location: Annotated[
        list[str] | None,
        typer.Option(
            "--location",
            metavar="PLACE",
            help='montreal, canada or usa. Repeat the flag or separate with commas, "" clears',
        ),
    ] = None,
    role: Annotated[
        list[str] | None,
        typer.Option(
            "--role",
            metavar="ROLE",
            help=(
                "swe, security, data, ml-ai, quant, infra, embedded, general-cs. Repeat the "
                'flag or separate with commas, "" clears'
            ),
        ),
    ] = None,
    term: Annotated[
        list[str] | None,
        typer.Option("--term", metavar="TERM", help='Terms such as summer-2027, "" clears'),
    ] = None,
    follow: Annotated[
        list[str] | None,
        typer.Option(
            "--follow",
            metavar="NAME",
            help='Employers to include from any place, one per flag, "" clears the list',
        ),
    ] = None,
    avoid: Annotated[
        list[str] | None,
        typer.Option(
            "--avoid", metavar="NAME", help='Employers to leave out, one per flag, "" clears'
        ),
    ] = None,
    about: Annotated[
        str | None,
        typer.Option(
            "--about", metavar="TEXT", help="A short note about you, kept on this machine"
        ),
    ] = None,
    show: Annotated[
        bool, typer.Option("--show", help="Print the saved preferences and change nothing")
    ] = False,
    clear: Annotated[bool, typer.Option("--clear", help="Delete every saved preference")] = False,
    as_json: JsonOption = False,
    db: DatabaseOption = None,
) -> None:
    from stage.cli.serialize import emit, preferences_to_json
    from stage.domain.preferences import PreferenceError
    from stage.services.preferences import (
        clear_preferences,
        load_preferences,
        preferences_lock,
    )

    changing = bool(location or role or term or follow or avoid) or about is not None
    if clear and (changing or show):
        _refuse("[red]--clear cannot be combined with other options.[/red]")
    if show and changing:
        _refuse("[red]--show cannot be combined with options that change preferences.[/red]")
    if clear:
        try:
            removed = clear_preferences()
        except OSError as exc:
            _could_not(exc, "delete")
        if as_json:
            emit(preferences_to_json(None, cleared=removed))
            return
        _say("Preferences cleared." if removed else "No preferences were saved.")
        return

    loaded = load_preferences()
    for problem in loaded.problems:
        _print_warning(problem, as_json)
    current = loaded.preferences

    for flag, values in (
        ("--location", location),
        ("--role", role),
        ("--term", term),
        ("--follow", follow),
        ("--avoid", avoid),
    ):
        if values and len(values) > 1 and any(not value.strip() for value in values):
            _refuse(f'[red]{flag} "" clears the list, so give it on its own.[/red]')
    if changing:
        known = _stored_companies(db) if follow or avoid else {}
        try:
            with preferences_lock():
                current = load_preferences().preferences
                updated = _updated(current, known, location, role, term, follow, avoid, about)
                _finish(updated, known, db, as_json, before=current)
        except (PreferenceError, InvalidOptionError) as exc:
            _print_failure(exc)
            raise typer.Exit(code=2) from exc
        except OSError as exc:
            _could_not(exc, "save")
        return

    if not (show or as_json) and _interactive():
        known = _stored_companies(db)
        _say(KEEP_OR_CLEAR, "dim")
        _finish(_guided(current, known, _ask, _warn), known, db, as_json, before=current)
        return

    if as_json:
        emit(preferences_to_json(current))
        return
    from stage.cli.render import terminal
    from stage.cli.render.postings import render_preferences

    render_preferences(terminal(), current, saved=False, counts=_open_counts(current, db))


def _updated(
    current: "Preferences",
    known: "Mapping[str, str]",
    location: list[str] | None,
    role: list[str] | None,
    term: list[str] | None,
    follow: list[str] | None,
    avoid: list[str] | None,
    about: str | None,
) -> "Preferences":
    from stage.services.preferences import revise_preferences

    return revise_preferences(
        current,
        known,
        locations=location or None,
        roles=role or None,
        terms=_terms(term) if term else None,
        follow=follow or None,
        avoid=avoid or None,
        about=about,
        commas=False,
    )


def _terms(values: Sequence[str]) -> tuple[str, ...]:
    from stage.domain.preferences import parse_terms, split_values

    chosen = split_values(values)
    for value in chosen:
        _validated_term(value, offer_unknown=False)
    return parse_terms(chosen)


def _guided(
    current: "Preferences",
    known: "Mapping[str, str]",
    ask: Callable[[str, str], str],
    warn: Callable[[str], None],
) -> "Preferences":
    from stage.domain.preferences import (
        CHOOSABLE_LOCATIONS,
        CHOOSABLE_ROLES,
        COMPANY_SEPARATOR,
        ChosenCompany,
        PreferenceError,
        Preferences,
        check_overlap,
        parse_about,
        parse_companies,
        parse_locations,
        parse_roles,
    )
    from stage.lexicon import fold

    def step[T](question: str, kept: T, shown: str, parse: Callable[[list[str]], T]) -> T:
        while True:
            answer = ask(question, shown).strip()
            if answer == shown.strip():
                return kept
            try:
                return parse(["" if answer == "-" else answer])
            except (PreferenceError, InvalidOptionError) as exc:
                warn(str(exc))

    def avoided(values: list[str]) -> tuple[ChosenCompany, ...]:
        chosen = parse_companies(values, fold, known)
        check_overlap(follow, chosen)
        return chosen

    places = ", ".join(location.value for location in CHOOSABLE_LOCATIONS)
    kinds = ", ".join(role.value for role in CHOOSABLE_ROLES)
    locations = step(
        f"Locations ({places})",
        current.locations,
        ", ".join(location.value for location in current.locations),
        parse_locations,
    )
    roles = step(
        f"Roles ({kinds})",
        current.roles,
        ", ".join(role.value for role in current.roles),
        parse_roles,
    )
    terms = step("Terms (such as summer-2027)", current.terms, ", ".join(current.terms), _terms)
    follow = step(
        "Employers to follow, included from any place",
        current.follow,
        COMPANY_SEPARATOR.join(e.name for e in current.follow),
        lambda values: parse_companies(values, fold, known),
    )
    kept_avoid = tuple(e for e in current.avoid if e.folded not in {f.folded for f in follow})
    avoid = step(
        "Employers to leave out",
        kept_avoid,
        COMPANY_SEPARATOR.join(e.name for e in kept_avoid),
        avoided,
    )
    about = step(
        "A short note about you (optional)",
        current.about,
        current.about,
        lambda v: parse_about(v[0]),
    )
    return Preferences(locations, roles, terms, follow, avoid, about)


def _finish(
    preferences: "Preferences",
    known: "Mapping[str, str]",
    db: "Path | None",
    as_json: bool,
    *,
    before: "Preferences | None" = None,
) -> None:
    from stage.cli.serialize import emit, preferences_to_json
    from stage.services.preferences import save_preferences, unseen_companies

    try:
        path = save_preferences(preferences)
    except OSError as exc:
        _could_not(exc, "save")
    for warning in unseen_companies(preferences, known, before):
        _print_warning(warning, as_json)
    if as_json:
        emit(preferences_to_json(preferences))
        return
    from stage.cli.render import plain, terminal
    from stage.cli.render.postings import render_preferences

    console = terminal()
    render_preferences(console, preferences, saved=True, counts=_open_counts(preferences, db))
    console.print(plain(f"Stored in {path}", "dim"), soft_wrap=True)


def _could_not(exc: OSError, verb: str) -> NoReturn:
    from stage.cli.render import plain
    from stage.domain.text import plain_error
    from stage.services.preferences import preferences_path

    reason = exc.strerror or plain_error(str(exc))
    _print_error(
        plain(
            f"Stage could not {verb} {preferences_path()} ({reason}). Check that the folder "
            "is yours and writable.",
            "red",
        ),
        soft_wrap=True,
    )
    raise typer.Exit(code=1) from exc


def _stored_companies(db: "Path | None") -> dict[str, str]:
    from stage.services.preferences import stored_companies

    database = _database(db)
    if not database.exists():
        return {}
    return _unless_unreadable(database, lambda: _with_db(database, stored_companies), {}, warn=True)


def _open_counts(preferences: "Preferences", db: "Path | None") -> dict[str, int] | None:

    wanted = {company.folded for company in (*preferences.follow, *preferences.avoid)}
    database = _database(db)
    if not wanted or not database.exists():
        return None
    return _unless_unreadable(
        database,
        lambda: _with_db(database, lambda repository: repository.open_counts_by_company(wanted)),
        None,
        warn=False,
    )


def _unless_unreadable[T](database: "Path", read: Callable[[], T], fallback: T, *, warn: bool) -> T:
    import sqlite3

    from stage.storage.migrations import SchemaVersionError

    try:
        return read()
    except (sqlite3.Error, SchemaVersionError) as exc:
        if warn:
            from stage.cli.options import database_problem
            from stage.cli.render import plain

            problem = (
                str(exc) if isinstance(exc, SchemaVersionError) else database_problem(exc, database)
            )
            said = f"{problem} Stage saves your preferences without checking employer names."
            _print_error(plain(said, "yellow"), soft_wrap=True)
        return fallback


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(question: str, shown: str) -> str:
    if len(shown) > SHOWN_IN_FULL:
        question = f"{question} (Enter keeps the saved one)"
    return ask(
        question,
        default=shown,
        show_default=0 < len(shown) <= SHOWN_IN_FULL,
        stopped="Stopped, so your preferences were not changed.",
    )


def _warn(message: str) -> None:
    _say(message, "red")


def _say(message: str, style: str = "") -> None:
    from stage.cli.render import plain, terminal

    terminal().print(plain(message, style))
