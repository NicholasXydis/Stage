import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from difflib import get_close_matches

from stage.domain.enums import UNKNOWN_TERM, LocationBucket, RoleCategory
from stage.domain.filters import JobFilters

MAX_ABOUT = 2_000
MAX_COMPANIES = 50
MAX_COMPANY_NAME = 120
MAX_TERMS = 12
MAX_COMMAS_IN_A_NAME = 3

CHOOSABLE_LOCATIONS = (LocationBucket.MONTREAL, LocationBucket.CANADA, LocationBucket.USA)
CHOOSABLE_ROLES = tuple(
    role for role in RoleCategory if role not in (RoleCategory.HARDWARE, RoleCategory.UNKNOWN)
)
ALWAYS_ROLES = frozenset({RoleCategory.GENERAL_CS, RoleCategory.UNKNOWN})
COVERED_BY = {LocationBucket.CANADA: (LocationBucket.MONTREAL,)}
COMPANY_SEPARATOR = "; "

_TERM = re.compile(r"^[a-z]+-\d{4}$")
_SEPARATOR = re.compile(r"[,;]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


class PreferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChosenCompany:
    name: str
    folded: str


@dataclass(frozen=True, slots=True)
class Preferences:
    locations: tuple[LocationBucket, ...] = ()
    roles: tuple[RoleCategory, ...] = ()
    terms: tuple[str, ...] = ()
    follow: tuple[ChosenCompany, ...] = ()
    avoid: tuple[ChosenCompany, ...] = ()
    about: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.narrows or self.about)

    @property
    def narrows(self) -> bool:
        return bool(self.locations or self.roles or self.terms or self.follow or self.avoid)

    def apply(self, filters: JobFilters) -> JobFilters:
        named = filters.company is None
        placed = filters.location is None and bool(self.locations)
        return replace(
            filters,
            locations=self._locations() if placed else frozenset(),
            roles=self._roles() if filters.role is None else frozenset(),
            terms=self._terms() if filters.term is None else frozenset(),
            avoid_companies=frozenset(e.folded for e in self.avoid) if named else frozenset(),
            follow_companies=frozenset(e.folded for e in self.follow)
            if named and placed
            else frozenset(),
        )

    def describe(self, filters: JobFilters) -> str:
        parts: list[str] = []
        placed = filters.location is None and bool(self.locations)
        if placed:
            parts.append(", ".join(location.value for location in self.locations))
        if self.roles and filters.role is None:
            parts.append(", ".join(role.value for role in self.roles))
        if self.terms and filters.term is None:
            parts.append(", ".join(self.terms))
        if filters.company is None:
            if self.follow and placed:
                parts.append("following " + COMPANY_SEPARATOR.join(e.name for e in self.follow))
            if self.avoid:
                parts.append("leaving out " + COMPANY_SEPARATOR.join(e.name for e in self.avoid))
        if not parts:
            return "Mine: the filters you typed replace your saved preferences"
        return "Mine: " + " | ".join(parts)

    def _locations(self) -> frozenset[LocationBucket]:
        covered = {extra for chosen in self.locations for extra in COVERED_BY.get(chosen, ())}
        return frozenset(self.locations) | covered

    def _roles(self) -> frozenset[RoleCategory]:
        return frozenset(self.roles) | ALWAYS_ROLES if self.roles else frozenset()

    def _terms(self) -> frozenset[str]:
        return frozenset(self.terms) | {UNKNOWN_TERM} if self.terms else frozenset()


def _unknown(label: str, value: str, choices: list[str]) -> PreferenceError:
    near = get_close_matches(value.lower(), choices, n=1, cutoff=0.6)
    if near:
        return PreferenceError(f"Unknown {label} {value!r}. Did you mean {near[0]!r}?")
    return PreferenceError(f"Unknown {label} {value!r}. Choose from: {', '.join(choices)}")


def _pieces(values: Iterable[str]) -> list[str]:
    return [piece.strip() for value in values for piece in _SEPARATOR.split(value) if piece.strip()]


def split_values(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(_pieces(values)))


def parse_locations(values: Iterable[str]) -> tuple[LocationBucket, ...]:
    allowed = {location.value: location for location in CHOOSABLE_LOCATIONS}
    chosen: list[LocationBucket] = []
    for value in split_values(values):
        location = allowed.get(value.lower())
        if value.lower() == LocationBucket.UNKNOWN.value:
            raise PreferenceError(
                "Postings with no location given are left out of --mine; "
                "stage list --mine --location unknown shows them."
            )
        if location is None:
            raise _unknown("location", value, list(allowed))
        chosen.append(location)
    return tuple(dict.fromkeys(chosen))


def parse_roles(values: Iterable[str]) -> tuple[RoleCategory, ...]:
    allowed = {role.value: role for role in CHOOSABLE_ROLES}
    chosen: list[RoleCategory] = []
    for value in split_values(values):
        role = allowed.get(value.lower())
        if role is None:
            raise _unknown("role", value, list(allowed))
        chosen.append(role)
    return tuple(dict.fromkeys(chosen))


def parse_terms(values: Iterable[str]) -> tuple[str, ...]:
    chosen: list[str] = []
    for value in split_values(values):
        term = value.lower()
        if term == UNKNOWN_TERM:
            continue
        if not _TERM.match(term):
            raise PreferenceError(f"Unknown term {value!r}. Terms look like summer-2027.")
        chosen.append(term)
    unique = tuple(dict.fromkeys(chosen))
    if len(unique) > MAX_TERMS:
        raise PreferenceError(f"At most {MAX_TERMS} terms can be saved.")
    return unique


def parse_companies(
    values: Iterable[str],
    fold: Callable[[str], str],
    known: Mapping[str, str] | None = None,
) -> tuple[ChosenCompany, ...]:
    stored = known or {}
    chosen: dict[str, ChosenCompany] = {}
    for value in _company_names(_pieces(values), fold, stored):
        name = clean_text(value)
        if len(name) > MAX_COMPANY_NAME:
            raise PreferenceError(f"Employer names can be at most {MAX_COMPANY_NAME} characters.")
        folded = fold(name)
        if not folded:
            raise PreferenceError(
                f"{value!r} has no letters or digits, so it cannot match an employer."
            )
        chosen.setdefault(folded, ChosenCompany(name=stored.get(folded, name), folded=folded))
        if len(chosen) > MAX_COMPANIES:
            raise PreferenceError(f"At most {MAX_COMPANIES} employers can be saved in each list.")
    return tuple(chosen.values())


def _company_names(
    pieces: list[str], fold: Callable[[str], str], known: Mapping[str, str]
) -> Iterator[str]:
    start = 0
    while start < len(pieces):
        widest = min(len(pieces), start + 1 + MAX_COMMAS_IN_A_NAME) if known else start + 1
        end = next(
            (
                stop
                for stop in range(widest, start + 1, -1)
                if fold(", ".join(pieces[start:stop])) in known
            ),
            start + 1,
        )
        yield ", ".join(pieces[start:end])
        start = end


def check_overlap(follow: Iterable[ChosenCompany], avoid: Iterable[ChosenCompany]) -> None:
    avoided = {company.folded for company in avoid}
    both = [company.name for company in follow if company.folded in avoided]
    if both:
        raise PreferenceError(
            f"{COMPANY_SEPARATOR.join(both)} cannot be both followed and avoided."
        )


def clean_text(value: str) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())


def parse_about(value: str) -> str:
    about = clean_text(value)
    if len(about) > MAX_ABOUT:
        raise PreferenceError(f"The note about you can be at most {MAX_ABOUT:,} characters.")
    return about


__all__ = [
    "ALWAYS_ROLES",
    "CHOOSABLE_LOCATIONS",
    "CHOOSABLE_ROLES",
    "COMPANY_SEPARATOR",
    "MAX_ABOUT",
    "MAX_COMPANIES",
    "ChosenCompany",
    "PreferenceError",
    "Preferences",
    "check_overlap",
    "clean_text",
    "parse_about",
    "parse_companies",
    "parse_locations",
    "parse_roles",
    "parse_terms",
    "split_values",
]
