import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from stage.domain import UNKNOWN_TERM
from stage.lexicon import fold, term_lexicon

_YEAR = re.compile(r"^(20[2-3]\d)$")
_SHORT_YEAR = re.compile(r"^(\d{2})$")
_PROXIMITY = 2
_GRADUATION_REACH = 3
_GRADUATION_FORMS = frozenset({"graduating", "graduation", "graduated", "diplomation"})
_GRADUATION_DATES = frozenset({"year", "date"})
_GRADUATION_LINKS = frozenset({"in", "by"})
_BODY_REACH = 8
_BODY_GRADUATION = frozenset({"graduating", "graduation", "grad", "grads", "enrolled"})
_TITLE_GRADUATES = frozenset({"graduates", "grads", "graduating", "graduation", "graduated"})
_SEASON_ENDS = {
    "winter": (1, 4, 30),
    "spring": (0, 6, 30),
    "summer": (0, 8, 31),
    "fall": (0, 12, 31),
}
_SHORT_YEAR_SPAN = range(-1, 3)
_MONTHS = {
    **dict.fromkeys(("january", "jan", "janvier", "janv"), 1),
    **dict.fromkeys(("april", "apr", "avril", "avr"), 4),
    **dict.fromkeys(("may", "mai"), 5),
    **dict.fromkeys(("august", "aug", "aout"), 8),
    **dict.fromkeys(("september", "sept", "sep", "septembre"), 9),
    **dict.fromkeys(("december", "dec", "decembre"), 12),
}
_MONTH_TERMS = {(1, 4): "winter", (5, 8): "summer", (9, 12): "fall"}
_RANGE_LINKS = frozenset({"to", "a", "au", "through", "thru", "until"})
_SHORT_CODES = {"h": "winter", "e": "summer", "a": "fall", "p": "spring"}
_SHORT_CODE = re.compile(r"^([heap])(\d{2})$")
_INTERNSHIP_WORDS = frozenset({"stage", "stagiaire", "stagiaires", "coop", "intern", "internship"})
_CODE_ALONE = re.compile(
    r"(?:^|[(\[|,/\u2013\u2014]|\s-)\s*([heap]\d{2})\s*(?:$|[)\]|,/\u2013\u2014]|-\s)",
    re.IGNORECASE,
)
_SEASON_LINKS = frozenset({"and", "or", "et", "ou"})


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
        if candidate - pivot_year in _SHORT_YEAR_SPAN:
            return candidate
    return None


def season_end(season: str, year: int) -> datetime | None:
    end = _SEASON_ENDS.get(season)
    if end is None:
        return None
    later, month, day = end
    return datetime(year + later, month, day, 23, 59, tzinfo=UTC)


def ended_before(term: str, moment: datetime | None) -> bool:
    season, _, year = term.partition("-")
    end = season_end(season, int(year)) if year.isdigit() else None
    if end is None or moment is None:
        return False
    return end < (moment if moment.tzinfo else moment.replace(tzinfo=UTC))


def _blocked(tokens: Sequence[str], index: int, blocked: frozenset[str]) -> bool:
    if index + 1 < len(tokens) and f"{tokens[index]} {tokens[index + 1]}" in blocked:
        return True
    return index > 0 and f"{tokens[index - 1]} {tokens[index]}" in blocked


def _names_a_graduation(
    tokens: Sequence[str], index: int, words: frozenset[str], *, body: bool
) -> bool:
    if body:
        near = tokens[max(0, index - _GRADUATION_REACH) : index]
        far = tokens[max(0, index - _BODY_REACH) : index]
        return any(token in words for token in near) or any(t in _BODY_GRADUATION for t in far)
    behind = index - 1
    while behind >= 0 and tokens[behind] in _GRADUATION_LINKS:
        behind -= 1
    if behind < 0:
        return False
    if tokens[behind] in _GRADUATION_DATES:
        return behind > 0 and tokens[behind - 1] in words
    return tokens[behind] in _GRADUATION_FORMS


def _graduates_after(
    tokens: Sequence[str],
    last: int,
    words: frozenset[str],
    seasons: Mapping[str, str],
    pivot_year: int | None,
    *,
    body: bool,
) -> bool:
    following = tokens[last + 1] if last + 1 < len(tokens) else ""
    if body or following not in _TITLE_GRADUATES:
        return body and following in words
    ahead = last + 2
    while ahead < len(tokens) and tokens[ahead] in _GRADUATION_LINKS:
        ahead += 1
    if ahead == len(tokens):
        return True
    return tokens[ahead] not in seasons and _year_from(tokens[ahead], pivot_year) is None


def scan(
    text: str, pivot_year: int | None = None, *, body: bool = False, guard: bool = True
) -> list[tuple[str, int | None, str]]:
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
        if guard and _names_a_graduation(tokens, index, lexicon.graduation_words, body=body):
            continue
        season = matched
        year: int | None = None
        phrase = token
        last = index
        for offset in range(1, _PROXIMITY + 2):
            ahead = index + offset
            if ahead >= len(tokens):
                break
            candidate = _year_from(tokens[ahead], pivot_year)
            if candidate is not None:
                year, phrase, last = candidate, " ".join(tokens[index : ahead + 1]), ahead
                following = tokens[ahead + 1] if ahead + 1 < len(tokens) else ""
                after = tokens[ahead + 2] if ahead + 2 < len(tokens) else ""
                spans = _year_from(following, candidate) == candidate + 1
                if season == "winter" and spans and after not in lexicon.graduation_words:
                    year, phrase, last = candidate + 1, f"{phrase} {following}", ahead + 1
                break
            if tokens[ahead] not in lexicon.fillers:
                break
        if year is None:
            behind = index - 1
            graduating_year = tokens[max(0, behind - 2) : behind] == ["class", "of"] or (
                behind > 0 and tokens[behind - 1] in lexicon.graduation_words
            )
            if behind >= 0 and not graduating_year:
                candidate = _year_from(tokens[behind], pivot_year)
                if candidate is not None:
                    year, phrase = candidate, f"{tokens[behind]} {token}"
        if guard and _graduates_after(
            tokens, last, lexicon.graduation_words, surface, pivot_year, body=body
        ):
            continue
        found.append((season, year, phrase))
    if not body:
        words = lexicon.graduation_words if guard else frozenset()
        springs = {year for season, year, _ in found if season == "spring"}
        found.extend(
            ("spring", year, phrase)
            if season == "winter" and (year in springs or None in springs)
            else (season, year, phrase)
            for season, year, phrase in _month_ranges(tokens, pivot_year, words)
        )
        found.extend(_short_codes(text, tokens, pivot_year))
    return found


def claimed_seasons(text: str, found: Sequence[tuple[str, int | None, str]]) -> set[str]:
    lexicon = term_lexicon()
    surface = {phrase: name for name, phrases in lexicon.seasons.items() for phrase in phrases}
    tokens = fold(text).split()
    spots = [
        (index, surface[token])
        for index, token in enumerate(tokens)
        if token in surface and not _blocked(tokens, index, lexicon.blocked_bigrams)
    ]
    claimed = {season for season, year, _ in found if year is not None}
    for (start, one), (end, other) in zip(spots, spots[1:], strict=False):
        between = tokens[start + 1 : end]
        if one != other and len(between) <= 1 and set(between) <= _SEASON_LINKS:
            claimed |= {one, other}
    return claimed


def _month_ranges(
    tokens: Sequence[str], pivot_year: int | None, words: frozenset[str]
) -> list[tuple[str, int | None, str]]:
    found: list[tuple[str, int | None, str]] = []
    for index, token in enumerate(tokens):
        start = _MONTHS.get(token)
        if start is None or _names_a_graduation(tokens, index, words, body=False):
            continue
        ahead = index + 1
        first_year = _year_from(tokens[ahead], pivot_year) if ahead < len(tokens) else None
        if first_year is not None:
            ahead += 1
        if ahead < len(tokens) and tokens[ahead] in _RANGE_LINKS:
            ahead += 1
        end = _MONTHS.get(tokens[ahead]) if ahead < len(tokens) else None
        season = _MONTH_TERMS.get((start, end)) if end is not None else None
        if season is None or ahead + 1 >= len(tokens):
            continue
        year = _year_from(tokens[ahead + 1], pivot_year)
        if year is None or (first_year is not None and first_year != year):
            continue
        found.append((season, year, " ".join(tokens[index : ahead + 2])))
    return found


def _short_codes(
    text: str, tokens: Sequence[str], pivot_year: int | None
) -> list[tuple[str, int | None, str]]:
    joined = " ".join(tokens)
    if not (_INTERNSHIP_WORDS.intersection(tokens) or "co op" in joined) or pivot_year is None:
        return []
    alone = {match.lower() for match in _CODE_ALONE.findall(text)}
    found: list[tuple[str, int | None, str]] = []
    for index, token in enumerate(tokens):
        code = _SHORT_CODE.match(token)
        near = {*tokens[max(0, index - 1) : index], *tokens[index + 1 : index + 2]}
        beside = bool(near & _INTERNSHIP_WORDS) or tokens[max(0, index - 2) : index] == ["co", "op"]
        if code is None or not (beside or token in alone):
            continue
        year = _year_from(code.group(2), pivot_year)
        if year is not None:
            found.append((_SHORT_CODES[code.group(1)], year, token))
    return found


def _lone_year(tokens: Sequence[str], words: frozenset[str]) -> int | None:
    spots = [index for index, token in enumerate(tokens) if _YEAR.match(token)]
    if not spots or len({tokens[index] for index in spots}) > 1:
        return None
    for index in spots:
        before = tokens[max(0, index - _GRADUATION_REACH) : index]
        after = tokens[index + 1 : index + 2]
        if words.intersection([*before, *after]) or list(before[-2:]) == ["class", "of"]:
            return None
    return int(tokens[spots[0]])


def _terms_in(
    text: str, pivot_year: int | None, *, body: bool = False
) -> tuple[set[str], set[str], list[str]]:
    terms: set[str] = set()
    seasons: set[str] = set()
    evidence: list[str] = []
    for season, year, phrase in scan(text, pivot_year, body=body):
        seasons.add(season)
        evidence.append(phrase)
        if year is not None:
            terms.add(f"{season}-{year}")
    if not body and not terms and len(seasons) == 1:
        year = _lone_year(fold(text).split(), term_lexicon().graduation_words)
        if year is not None:
            terms.add(f"{next(iter(seasons))}-{year}")
    return terms, seasons, evidence


def _structured(
    values: Sequence[str], season: str, pivot_year: int | None, posted: datetime | None
) -> tuple[set[str], set[str], list[str]]:
    terms: set[str] = set()
    seasons: set[str] = set()
    evidence: list[str] = []
    for value in [*values, season]:
        if not value or value.strip().lower() in {"n/a", "na", "none", "null", "unknown"}:
            continue
        found, found_seasons, phrases = _terms_in(value, pivot_year)
        terms |= {term for term in found if not ended_before(term, posted)}
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
    posted: datetime | None = None,
) -> ResolvedTerm:
    titled = _terms_in(title, pivot_year)
    authored = (_structured(structured_terms, structured_season, pivot_year, posted), titled)
    if len(claimed_seasons(title, scan(title, pivot_year))) > 1:
        return ResolvedTerm(evidence=tuple(dict.fromkeys(titled[2])), conflict=True)
    evidence: list[str] = []
    seasons: set[str] = set()
    speaking: list[set[str]] = []
    for terms, found_seasons, phrases in authored:
        evidence.extend(phrases)
        seasons |= found_seasons
        if terms:
            speaking.append(terms)

    if not speaking:
        body_terms, body_seasons, body_phrases = _terms_in(description, None, body=True)
        body_terms = {term for term in body_terms if not ended_before(term, posted)}
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


def fits_title(term: str, title: str) -> bool:
    season, _, year = term.partition("-")
    return all(
        named == season and (when is None or str(when) == year) for named, when, _ in scan(title)
    )


def graduation_only(term: str, title: str, description: str, pivot_year: int | None) -> bool:
    season, _, year = term.partition("-")
    if not year.isdigit():
        return False

    def named(guard: bool) -> bool:
        found = scan(title, pivot_year, guard=guard) + scan(
            description, None, body=True, guard=guard
        )
        return any(name == season and named_year == int(year) for name, named_year, _ in found)

    return named(False) and not named(True)
