import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from stage.cli.options import (
    DatabaseOption,
    InvalidOptionError,
    JsonOption,
    _database,
    _print_failure,
    _print_warning,
    _refuse,
    _validated_term,
    app,
    run_async,
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
            help="montreal, canada or usa; repeat the flag or separate with commas",
        ),
    ] = None,
    role: Annotated[
        list[str] | None,
        typer.Option(
            "--role",
            metavar="ROLE",
            help="swe, security, data, ml-ai, quant, infra, embedded, general-cs",
        ),
    ] = None,
    term: Annotated[
        list[str] | None,
        typer.Option("--term", metavar="TERM", help="Terms such as summer-2027"),
    ] = None,
    follow: Annotated[
        list[str] | None,
        typer.Option(
            "--follow",
            metavar="NAME",
            help="Employers to include from any place; repeat the flag for more",
        ),
    ] = None,
    avoid: Annotated[
        list[str] | None,
        typer.Option(
            "--avoid", metavar="NAME", help="Employers to leave out; repeat the flag for more"
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
    from stage.services.preferences import clear_preferences, load_preferences

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

    if changing:
        known = _stored_employers(db) if follow or avoid else {}
        try:
            updated = _updated(current, known, location, role, term, follow, avoid, about)
        except (PreferenceError, InvalidOptionError) as exc:
            _print_failure(exc)
            raise typer.Exit(code=2) from exc
        _finish(updated, known, db, as_json)
        return

    if not (show or as_json) and _interactive():
        known = _stored_employers(db)
        _say(KEEP_OR_CLEAR, "dim")
        _finish(_guided(current, known, _ask, _warn), known, db, as_json)
        return

    if as_json:
        emit(preferences_to_json(current))
        return
    from stage.cli.render import render_preferences, terminal

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
    from stage.domain.preferences import (
        Preferences,
        check_overlap,
        parse_about,
        parse_employers,
        parse_locations,
        parse_roles,
    )
    from stage.lexicon import fold

    updated = Preferences(
        locations=parse_locations(location) if location else current.locations,
        roles=parse_roles(role) if role else current.roles,
        terms=_terms(term) if term else current.terms,
        follow=parse_employers(follow, fold, known) if follow else current.follow,
        avoid=parse_employers(avoid, fold, known) if avoid else current.avoid,
        about=parse_about(about) if about is not None else current.about,
    )
    check_overlap(updated.follow, updated.avoid)
    return updated


def _terms(values: Sequence[str]) -> tuple[str, ...]:
    from stage.domain.preferences import parse_terms, split_values

    chosen = split_values(values)
    for value in chosen:
        _validated_term(value)
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
        EMPLOYER_SEPARATOR,
        Employer,
        PreferenceError,
        Preferences,
        check_overlap,
        parse_about,
        parse_employers,
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

    def avoided(values: list[str]) -> tuple[Employer, ...]:
        chosen = parse_employers(values, fold, known)
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
        EMPLOYER_SEPARATOR.join(e.name for e in current.follow),
        lambda values: parse_employers(values, fold, known),
    )
    kept_avoid = tuple(e for e in current.avoid if e.key not in {f.key for f in follow})
    avoid = step(
        "Employers to leave out",
        kept_avoid,
        EMPLOYER_SEPARATOR.join(e.name for e in kept_avoid),
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
    preferences: "Preferences", known: "Mapping[str, str]", db: "Path | None", as_json: bool
) -> None:
    from stage.cli.serialize import emit, preferences_to_json
    from stage.services.preferences import save_preferences

    try:
        path = save_preferences(preferences)
    except OSError as exc:
        _could_not(exc, "save")
    for warning in _unseen_employers(preferences, known):
        _print_warning(warning, as_json)
    if as_json:
        emit(preferences_to_json(preferences))
        return
    from stage.cli.render import render_preferences, terminal

    render_preferences(
        terminal(), preferences, saved=True, path=path, counts=_open_counts(preferences, db)
    )


def _could_not(exc: OSError, verb: str) -> NoReturn:
    from stage.services.preferences import preferences_path

    reason = exc.strerror or type(exc).__name__
    _print_failure(OSError(f"Could not {verb} {preferences_path()}: {reason}"))
    raise typer.Exit(code=1) from exc


def _stored_employers(db: "Path | None") -> dict[str, str]:
    from stage.lexicon import fold
    from stage.storage import open_repository

    database = _database(db)
    if not database.exists():
        return {}

    async def names() -> list[str]:
        async with open_repository(database) as repository:
            return list(await repository.company_names())

    return {fold(name): name for name in run_async(names())}


def _open_counts(preferences: "Preferences", db: "Path | None") -> dict[str, int] | None:
    from stage.storage import open_repository

    keys = {employer.key for employer in (*preferences.follow, *preferences.avoid)}
    database = _database(db)
    if not keys or not database.exists():
        return None

    async def counts() -> dict[str, int]:
        async with open_repository(database) as repository:
            return await repository.open_counts_by_company(keys)

    return run_async(counts())


def _unseen_employers(preferences: "Preferences", known: "Mapping[str, str]") -> list[str]:
    from difflib import get_close_matches

    if not known:
        return []
    warnings: list[str] = []
    for employer in (*preferences.follow, *preferences.avoid):
        if employer.key in known:
            continue
        near = get_close_matches(employer.key, list(known), n=1, cutoff=0.75)
        hint = f" Did you mean {known[near[0]]!r}?" if near else ""
        warnings.append(f"Saved {employer.name!r}, but Stage has no postings from it yet.{hint}")
    return warnings


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(question: str, shown: str) -> str:
    if len(shown) > SHOWN_IN_FULL:
        question = f"{question} (Enter keeps the saved one)"
    return str(typer.prompt(question, default=shown, show_default=0 < len(shown) <= SHOWN_IN_FULL))


def _warn(message: str) -> None:
    _say(message, "red")


def _say(message: str, style: str = "") -> None:
    from stage.cli.render import plain, terminal

    terminal().print(plain(message, style))
