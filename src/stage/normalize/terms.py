
import re
from collections.abc import Sequence
from dataclasses import dataclass

from stage.domain import UNKNOWN_TERM
from stage.lexicon import fold, term_lexicon

_YEAR = re.compile(r"^(20[2-3]\d)$")
_SHORT_YEAR = re.compile(r"^(\d{2})$")
_PROXIMITY = 2


@dataclass(frozen=True, slots=True)
class ResolvedTerm:

    term: str = UNKNOWN_TERM
    season: str = ""
    year: int | None = None
    evidence: tuple[str, ...] = ()
    conflict: bool = False


def _year_from(token: str, pivot_year: int | None) -> int | None:
    full = _YEAR.match(token)
    if full:
        return int(full.group(1))
    short = _SHORT_YEAR.match(token)
    if short and pivot_year is not None:
        candidate = pivot_year - pivot_year % 100 + int(short.group(1))
        if abs(candidate - pivot_year) <= 5:
            return candidate
    return None


def _blocked(tokens: Sequence[str], index: int, blocked: frozenset[str]) -> bool:
    if index + 1 < len(tokens) and f"{tokens[index]} {tokens[index + 1]}" in blocked:
        return True
    return index > 0 and f"{tokens[index - 1]} {tokens[index]}" in blocked


def scan(text: str, pivot_year: int | None = None) -> list[tuple[str, int | None, str]]:
    lexicon = term_lexicon()
    tokens = fold(text).split()
    if not tokens:
        return []

    surface: dict[str, str] = {}
    for name, phrases in lexicon.seasons.items():
        for phrase in phrases:
            surface[phrase] = name

    found: list[tuple[str, int | None, str]] = []
    for index, token in enumerate(tokens):
        matched = surface.get(token)
        if matched is None or _blocked(tokens, index, lexicon.blocked_bigrams):
            continue
        season = matched
        year: int | None = None
        phrase = token
        for offset in range(1, _PROXIMITY + 2):
            ahead = index + offset
            if ahead >= len(tokens):
                break
            candidate = _year_from(tokens[ahead], pivot_year)
            if candidate is not None:
                year, phrase = candidate, " ".join(tokens[index : ahead + 1])
                break
            if tokens[ahead] not in lexicon.fillers:
                break
        if year is None:
            behind = index - 1
            if behind >= 0:
                candidate = _year_from(tokens[behind], pivot_year)
                if candidate is not None:
                    year, phrase = candidate, f"{tokens[behind]} {token}"
        found.append((season, year, phrase))
    return found


def _terms_in(text: str, pivot_year: int | None) -> tuple[set[str], set[str], list[str]]:
    terms: set[str] = set()
    seasons: set[str] = set()
    evidence: list[str] = []
    for season, year, phrase in scan(text, pivot_year):
        seasons.add(season)
        evidence.append(phrase)
        if year is not None:
            terms.add(f"{season}-{year}")
    return terms, seasons, evidence


def _structured(values: Sequence[str], season: str, pivot_year: int | None) -> tuple[
    set[str], set[str], list[str]
]:
    terms: set[str] = set()
    seasons: set[str] = set()
    evidence: list[str] = []
    for value in [*values, season]:
        if not value or value.strip().lower() in {"n/a", "na", "none", "null", "unknown"}:
            continue
        found, found_seasons, phrases = _terms_in(value, pivot_year)
        terms |= found
        seasons |= found_seasons
        evidence.extend(phrases)
    return terms, seasons, evidence


def resolve_term(
    *,
    title: str = "",
    description: str = "",
    structured_terms: Sequence[str] = (),
    structured_season: str = "",
    pivot_year: int | None = None,
) -> ResolvedTerm:
    authored = (
        _structured(structured_terms, structured_season, pivot_year),
        _terms_in(title, pivot_year),
    )
    evidence: list[str] = []
    seasons: set[str] = set()
    speaking: list[set[str]] = []
    for terms, found_seasons, phrases in authored:
        evidence.extend(phrases)
        seasons |= found_seasons
        if terms:
            speaking.append(terms)

    if not speaking:
        body_terms, body_seasons, body_phrases = _terms_in(description, pivot_year)
        evidence.extend(body_phrases)
        seasons |= body_seasons
        if body_terms:
            speaking.append(body_terms)

    unique = tuple(dict.fromkeys(evidence))
    season = next(iter(sorted(seasons))) if len(seasons) == 1 else ""

    if not speaking:
        return ResolvedTerm(season=season, evidence=unique)

    conflict = len({frozenset(terms) for terms in speaking}) > 1
    chosen = speaking[0]
    if conflict or len(chosen) != 1:
        return ResolvedTerm(season=season, evidence=unique, conflict=conflict)

    term = next(iter(chosen))
    resolved_season, _, year = term.partition("-")
    return ResolvedTerm(
        term=term,
        season=resolved_season,
        year=int(year),
        evidence=unique,
    )
