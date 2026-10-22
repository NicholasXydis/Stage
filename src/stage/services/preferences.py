import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stage.domain.preferences import (
    MAX_COMPANIES,
    ChosenCompany,
    PreferenceError,
    Preferences,
    check_overlap,
    clean_text,
    parse_about,
    parse_companies,
    parse_locations,
    parse_roles,
    parse_terms,
)
from stage.storage import AsyncRepository

FILE_NAME = "preferences.json"
FORMAT = 1
DAMAGED = (
    "Your saved preferences could not be read, so they were ignored. "
    "Run stage prefs to save them again."
)


@dataclass(frozen=True, slots=True)
class LoadedPreferences:
    preferences: Preferences
    problems: tuple[str, ...] = ()
    damaged: bool = False


def preferences_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / FILE_NAME


def load_preferences(path: Path | None = None) -> LoadedPreferences:
    target = path or preferences_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoadedPreferences(Preferences())
    except (OSError, UnicodeDecodeError):
        return _damaged()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _damaged()
    if not isinstance(payload, dict):
        return _damaged()
    return _from_payload(payload, target.name)


def save_preferences(preferences: Preferences, path: Path | None = None) -> Path:
    from stage.paths import restrict_permissions

    target = path or preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_payload(preferences), indent=2, ensure_ascii=False) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=".preferences.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        restrict_permissions(temporary)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def clear_preferences(path: Path | None = None) -> bool:
    target = path or preferences_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def revise_preferences(
    current: Preferences,
    known: Mapping[str, str],
    *,
    locations: Sequence[str] | None = None,
    roles: Sequence[str] | None = None,
    terms: Sequence[str] | None = None,
    follow: Sequence[str] | None = None,
    avoid: Sequence[str] | None = None,
    about: str | None = None,
) -> Preferences:
    from stage.lexicon import fold

    revised = Preferences(
        locations=current.locations if locations is None else parse_locations(locations),
        roles=current.roles if roles is None else parse_roles(roles),
        terms=current.terms if terms is None else parse_terms(terms),
        follow=current.follow if follow is None else parse_companies(follow, fold, known),
        avoid=current.avoid if avoid is None else parse_companies(avoid, fold, known),
        about=current.about if about is None else parse_about(about),
    )
    check_overlap(revised.follow, revised.avoid)
    return revised


async def stored_companies(repository: AsyncRepository) -> dict[str, str]:
    from stage.lexicon import fold

    return {fold(name): name for name in await repository.company_names()}


def unseen_companies(preferences: Preferences, known: Mapping[str, str]) -> list[str]:
    from difflib import get_close_matches

    if not known:
        return []
    warnings: list[str] = []
    for company in (*preferences.follow, *preferences.avoid):
        if company.folded in known:
            continue
        near = get_close_matches(company.folded, list(known), n=1, cutoff=0.75)
        hint = f" Did you mean {known[near[0]]!r}?" if near else ""
        warnings.append(f"Saved {company.name!r}, but Stage has no postings from it yet.{hint}")
    return warnings


def _damaged() -> LoadedPreferences:
    return LoadedPreferences(Preferences(), (DAMAGED,), damaged=True)


def _payload(preferences: Preferences) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "locations": [location.value for location in preferences.locations],
        "roles": [role.value for role in preferences.roles],
        "terms": list(preferences.terms),
        "follow": [{"name": e.name, "folded": e.folded} for e in preferences.follow],
        "avoid": [{"name": e.name, "folded": e.folded} for e in preferences.avoid],
        "about": preferences.about,
    }


def _from_payload(payload: dict[str, Any], label: str) -> LoadedPreferences:
    problems: list[str] = []

    def field[T](name: str, parse: Callable[[list[str]], T], empty: T) -> T:
        value = payload.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"{label}: {name} was ignored because it is not a list.")
            return empty
        try:
            return parse(value)
        except PreferenceError as exc:
            problems.append(f"{label}: {name} was ignored. {exc}")
            return empty

    about_value = payload.get("about", "")
    about = ""
    if isinstance(about_value, str):
        try:
            about = parse_about(about_value)
        except PreferenceError as exc:
            problems.append(f"{label}: about was ignored. {exc}")
    else:
        problems.append(f"{label}: about was ignored because it is not text.")

    follow = _companies(payload.get("follow", []), "follow", label, problems)
    avoid = _companies(payload.get("avoid", []), "avoid", label, problems)
    avoided = {company.folded for company in avoid}
    both = [company.name for company in follow if company.folded in avoided]
    if both:
        problems.append(
            f"{label}: {', '.join(both)} is in both follow and avoid, so it is avoided."
        )
    preferences = Preferences(
        locations=field("locations", parse_locations, ()),
        roles=field("roles", parse_roles, ()),
        terms=field("terms", parse_terms, ()),
        follow=tuple(company for company in follow if company.folded not in avoided),
        avoid=avoid,
        about=about,
    )
    return LoadedPreferences(preferences, tuple(problems))


def _companies(value: Any, name: str, label: str, problems: list[str]) -> tuple[ChosenCompany, ...]:
    if not isinstance(value, list):
        problems.append(f"{label}: {name} was ignored because it is not a list.")
        return ()
    found: dict[str, ChosenCompany] = {}
    for entry in value:
        company = _company(entry)
        if company is None:
            problems.append(f"{label}: an entry in {name} was skipped because it has no name.")
            continue
        found.setdefault(company.folded, company)
    if len(found) > MAX_COMPANIES:
        problems.append(f"{label}: {name} keeps only its first {MAX_COMPANIES} employers.")
    return tuple(found.values())[:MAX_COMPANIES]


def _company(entry: Any) -> ChosenCompany | None:
    if isinstance(entry, str):
        entry = {"name": entry}
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not clean_text(name):
        return None
    folded = entry.get("folded")
    if not isinstance(folded, str) or not folded:
        from stage.lexicon import fold

        folded = fold(name)
    return ChosenCompany(name=clean_text(name), folded=folded) if folded else None


__all__ = [
    "DAMAGED",
    "FILE_NAME",
    "LoadedPreferences",
    "clear_preferences",
    "load_preferences",
    "preferences_path",
    "revise_preferences",
    "save_preferences",
    "stored_companies",
    "unseen_companies",
]
