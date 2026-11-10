import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType
from urllib.parse import urlsplit

from stage.domain import (
    SOURCE_PRIORITY,
    Job,
    Language,
    LocationBucket,
    RemoteScope,
    location_agrees,
    term_agrees,
    terms_conflict,
)
from stage.domain.text import WORKDAY_DOMAINS, counts_locations, is_workday_opening
from stage.lexicon import (
    division_qualifiers,
    employer_aliases,
    fold,
    generic_company_tokens,
    name_root_tokens,
    role_lexicon,
)
from stage.normalize.urls import ZAPPLY_HOST, named_posting, zapply_opening

__all__ = [
    "SOURCE_PRIORITY",
    "MatchKind",
    "MatchResult",
    "employer_acronym",
    "employer_key",
    "job_opening",
    "opening_key",
    "title_canonical",
    "would_merge",
]


class MatchKind(StrEnum):
    NONE = "none"
    URL = "url"
    SAME_LANGUAGE = "same-language"
    CROSS_LANGUAGE = "cross-language"


@dataclass(frozen=True, slots=True)
class MatchResult:
    kind: MatchKind = MatchKind.NONE
    evidence: str = ""

    def __bool__(self) -> bool:
        return self.kind is not MatchKind.NONE


def title_canonical(title: str) -> str:
    folded = fold(title)
    if not folded:
        return ""
    padded = f" {folded} "
    hits: list[tuple[int, str]] = []
    for category, phrases in role_lexicon().items():
        matched = [phrase for phrase in phrases if f" {phrase} " in padded]
        if matched:
            hits.append((max(len(phrase) for phrase in matched), category))
    if not hits:
        return ""
    best = max(length for length, _ in hits)
    winners = sorted(category for length, category in hits if length == best)
    return winners[0] if len(winners) == 1 else ""


_SAME_WORD = {
    "engineering": "engineer",
    "development": "developer",
    "internship": "intern",
    "interns": "intern",
    "internships": "intern",
    "student": "intern",
    "undergrad": "undergraduate",
    "jan": "january",
    "feb": "february",
    "mar": "march",
    "apr": "april",
    "jun": "june",
    "jul": "july",
    "aug": "august",
    "sep": "september",
    "sept": "september",
    "oct": "october",
    "nov": "november",
    "dec": "december",
}
_JOINED = re.compile(r"\b(?:(?P<round>year round)|(?P<site>on site|in office)|(?P<coop>coop))\b")
_FISCAL = re.compile(r"fy[0-9]{2}")
_COUNTRIES = frozenset({"canada", "usa", "us"})


_YEAR = re.compile(r"20[0-9]{2}")
_SEASONS = frozenset({"summer", "fall", "autumn", "winter", "spring", "yearround"})
_MODES = frozenset({"onsite", "remote", "hybrid"})
_LEVELS = frozenset({"undergraduate", "graduate", "postgraduate", "masters", "phd"})
_MONTHS = frozenset(
    {
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)
_FILLER = frozenset({"and", "or", "in", "month", "months", "term"})
_CO_OP = frozenset({"co", "op"})
_PROGRAM = frozenset({"institute", "program", "programme", "university", "corporate"})
_ACRONYM = re.compile(r"\(([A-Z]{2,6})\)\s*$")
_TAG = re.compile(r"\(([A-Z]{2,6})\)")
_DEGREE_TAGS = frozenset({"ba", "bs", "bsc", "beng", "ma", "mba", "meng", "ms", "msc", "phd"})


_TEAM_TAIL = re.compile(r"(?:\s[-\u2013]\s|,\s)(?P<team>[^,\u2013-]+)$")


def _title_words(job: Job) -> frozenset[str]:
    title = job.title_raw
    tail = _TEAM_TAIL.search(title)
    root = employer_root(job.company)
    if (
        tail
        and root
        and fold(tail["team"]).split()[:1] == [root[0]]
        and fold(title[: tail.start()])
    ):
        title = title[: tail.start()]
    brands = _brands().get(" ".join(root), frozenset()) | (set(root) - generic_company_tokens())
    return frozenset(word for word in _words(_without_places(title)[0]) if word not in brands)


def _tags(title: str) -> frozenset[str]:
    return frozenset(tag.lower() for tag in _TAG.findall(title)) - _DEGREE_TAGS


def _joined(found: re.Match[str]) -> str:
    if found["coop"]:
        return "co op"
    return "yearround" if found["round"] else "onsite"


def _words(title: str) -> list[str]:
    return [_SAME_WORD.get(word, word) for word in _JOINED.sub(_joined, fold(title)).split()]


@lru_cache(maxsize=1)
def _brands() -> dict[str, frozenset[str]]:
    named: dict[str, set[str]] = {}
    for spelling, canonical in employer_aliases().items():
        if len(spelling.split()) == 1:
            named.setdefault(canonical, set()).add(spelling)
    return {canonical: frozenset(words) for canonical, words in named.items()}


@lru_cache(maxsize=8192)
def _without_places(title: str) -> tuple[str, frozenset[str], tuple[str, ...]]:
    from stage.normalize.location import place_tail

    named: set[str] = set()
    places: list[str] = []
    for _ in range(3):
        place = place_tail(title)
        head = title[: title.rindex(place)] if place else ""
        if not place or not fold(head.rstrip(" -\u2013")):
            break
        named.update(fold(place).split())
        places.append(place)
        title = head.rstrip(" -\u2013")
    return title, frozenset(named), tuple(places)


def _years(words: frozenset[str]) -> frozenset[str]:
    return frozenset(word for word in words if _YEAR.fullmatch(word))


def _numbers(words: frozenset[str]) -> frozenset[str]:
    return frozenset(word for word in words if word.isdigit() and not _YEAR.fullmatch(word))


def _same_title(left: Job, right: Job) -> str:
    first, second = _without_places(left.title_raw)[1], _without_places(right.title_raw)[1]
    if first and second and not (first.issubset(second) or second.issubset(first)):
        return ""
    one, two = _title_words(left), _title_words(right)
    places = frozenset(fold(f"{left.location_raw or ''} {right.location_raw or ''}").split())
    families = (_SEASONS, _MODES, _LEVELS, _COUNTRIES, places)
    named = [(family & one, family & two) for family in families]
    if any(a and b and not a & b for a, b in [(_years(one), _years(two)), *named]):
        return ""
    exact = [(_numbers(one), _numbers(two)), (_MONTHS & one, _MONTHS & two)]
    if any(a and b and a != b for a, b in exact):
        return ""
    ignored = (
        _years(one | two)
        | _numbers(one | two)
        | _SEASONS
        | _MODES
        | _LEVELS
        | _MONTHS
        | _FILLER
        | _PROGRAM
        | _COUNTRIES
        | {word for word in one | two if _FISCAL.fullmatch(word)}
        | places
        | first
        | second
    )
    if one >= _CO_OP and two >= _CO_OP:
        ignored |= {"intern"}
    tags = _tags(left.title_raw), _tags(right.title_raw)
    if bool(tags[0]) != bool(tags[1]):
        ignored |= tags[0] | tags[1]
    one, two = one - ignored, two - ignored
    if one and one == two:
        return " ".join(sorted(one))
    for job, other in ((left, two), (right, one)):
        head = _cut_head(job) - ignored
        if len(head) >= 5 and head <= other:
            return " ".join(sorted(head))
    return ""


def _cut_head(job: Job) -> frozenset[str]:
    if not job.title_raw.rstrip().endswith(("...", "\u2026")):
        return frozenset()
    return frozenset(_words(job.title_raw)[:-1])


def employer_acronym(name: str) -> str:
    found = _ACRONYM.search(name)
    return found[1].lower() if found else ""


def _qualified_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    if not shorter or longer[: len(shorter)] != shorter:
        return False
    if not set(longer[len(shorter) :]) <= division_qualifiers():
        return False
    return bool(set(shorter) - generic_company_tokens())


_LABEL = re.compile(r"\s*\([^()]*\)\s*$")


NOTHING_LEARNED: Mapping[str, str] = MappingProxyType({})


def _company_matches(left: Job, right: Job, learned: Mapping[str, str] = NOTHING_LEARNED) -> bool:
    if fold(left.company) == fold(right.company):
        return True
    known = learned.get(employer_name(left.company))
    if known is not None and known == learned.get(employer_name(right.company)):
        return True
    first, second = employer_root(left.company), employer_root(right.company)
    if first == second:
        return True
    acronyms = employer_acronym(left.company), employer_acronym(right.company)
    if (acronyms[0] and (acronyms[0],) == second) or (acronyms[1] and (acronyms[1],) == first):
        return True
    core = _core(first)
    if core == _core(second) and set(core) - generic_company_tokens():
        return True
    shorter, longer = sorted((first, second), key=len)
    return _qualified_prefix(shorter, longer)


@lru_cache(maxsize=8192)
def employer_root(name: str) -> tuple[str, ...]:
    tokens = name_root_tokens(_LABEL.sub("", name))
    joined = " ".join(tokens[1:] if len(tokens) > 1 and tokens[0] == "the" else tokens)
    return tuple(employer_aliases().get(joined, joined).split())


def employer_key(name: str) -> str:
    root = employer_root(name)
    return root[0] if root else fold(name).partition(" ")[0]


def employer_name(name: str) -> str:
    return " ".join(employer_root(name)) or fold(name)


def _core(tokens: tuple[str, ...]) -> tuple[str, ...]:
    qualifiers = division_qualifiers()
    end = len(tokens)
    while end > 1 and tokens[end - 1] in qualifiers:
        end -= 1
    return tokens[:end]


def would_merge(left: Job, right: Job, learned: Mapping[str, str] = NOTHING_LEARNED) -> MatchResult:
    url = left.apply_url_canonical
    same_board = left.board_key == right.board_key
    if url and url == right.apply_url_canonical and (not same_board or is_workday_opening(url)):
        return MatchResult(MatchKind.URL, url)
    opening, other = job_opening(left), job_opening(right)
    if opening and opening == other and not same_board:
        return MatchResult(MatchKind.URL, opening)
    if same_board or _two_openings(url, right.apply_url_canonical):
        return MatchResult()
    if opening and other:
        return MatchResult()

    if not _company_matches(left, right, learned) or terms_conflict(left.term, right.term):
        return MatchResult()

    if not _placeless_twin(left, right) and (
        not _places_agree(left, right) or _different_cities(left, right)
    ):
        return MatchResult()

    title = _same_title(left, right)
    if title:
        return MatchResult(MatchKind.SAME_LANGUAGE, title)

    return _cross_language(left, right)


def _placeless_twin(left: Job, right: Job) -> bool:
    if fold(left.title_raw) != fold(right.title_raw):
        return False
    for blank, placed in ((left, right), (right, left)):
        if (blank.location_raw or "").strip() or blank.location is not LocationBucket.UNKNOWN:
            continue
        if placed.location not in (LocationBucket.UNKNOWN, LocationBucket.INTERNATIONAL):
            return True
    return False


def _places_agree(left: Job, right: Job) -> bool:
    if location_agrees(left.location, right.location):
        return True
    scopes = {left.remote_scope, right.remote_scope}
    if None in scopes or LocationBucket.INTERNATIONAL in (left.location, right.location):
        return False
    return len(scopes - {RemoteScope.UNSPECIFIED}) <= 1


def _cross_language(left: Job, right: Job) -> MatchResult:
    pair = {left.language, right.language}
    if Language.EN not in pair or Language.FR not in pair:
        return MatchResult()
    if not term_agrees(left.term, right.term):
        return MatchResult()

    canonical = title_canonical(left.title_raw)
    if not canonical or canonical != title_canonical(right.title_raw):
        return MatchResult()
    return MatchResult(MatchKind.CROSS_LANGUAGE, canonical)


_PLACE_PARTS = re.compile(r"\s*(?:,|/|;|\(|\)|\||\s-\s)\s*")
_VAGUE_PLACE = frozenset(
    {
        "remote",
        "hybrid",
        "virtual",
        "anywhere",
        "various",
        "multiple",
        "location",
        "locations",
        "nationwide",
        "us",
        "usa",
        "united",
        "states",
        "america",
        "canada",
        "north",
        "onsite",
        "more",
        "area",
    }
)


@lru_cache(maxsize=1)
def _regions() -> frozenset[str]:
    from stage.lexicon import location_lexicon

    lexicon = location_lexicon()
    return frozenset(
        fold(name)
        for name in (
            lexicon.usa_regions | lexicon.usa_codes | lexicon.canada_regions | lexicon.canada_codes
        )
    )


@lru_cache(maxsize=4096)
def _cities(place: str) -> frozenset[str]:
    from stage.normalize.location import display_location

    found = set()
    for part in _PLACE_PARTS.split(place):
        folded = fold(part)
        words = folded.split()
        if not words or folded in _regions():
            continue
        if _VAGUE_PLACE.intersection(words) or any(word.isdigit() for word in words):
            continue
        shown = fold(_PLACE_PARTS.split(display_location(part), maxsplit=1)[0])
        if shown and shown not in _regions():
            found.add(shown)
    return frozenset(found)


_SHOWN_CITY = re.compile(r"(?P<city>[^,+]+), (?:[A-Z]{2}|Canada)(?: \+[0-9]+)?")


@lru_cache(maxsize=4096)
def _shown_city(place: str) -> frozenset[str]:
    from stage.normalize.location import display_location

    found = _SHOWN_CITY.fullmatch(display_location(place))
    city = fold(found["city"]) if found else ""
    if not city or city in _regions() or _VAGUE_PLACE.intersection(city.split()):
        return frozenset()
    return frozenset({city})


def _job_cities(job: Job) -> frozenset[str]:
    places = (job.location_raw or "", *_without_places(job.title_raw)[2])
    return frozenset().union(*(_cities(place) | _shown_city(place) for place in places))


def _different_cities(left: Job, right: Job) -> bool:
    one, two = _job_cities(left), _job_cities(right)
    if not one or not two:
        return False
    return not any(a in b or b in a for a in one for b in two)


def repost_key(company: str, title: str, place: str) -> str:
    words = " ".join(sorted(set(_words(title))))
    if not words or not fold(place) or counts_locations(place):
        return ""
    return "\n".join((employer_name(company), words, fold(place)))


def job_opening(job: Job) -> str:
    return opening_key(job.apply_url_canonical or job.apply_url_raw)


def _two_openings(left: str, right: str) -> bool:
    return left != right and is_workday_opening(left) and is_workday_opening(right)


_WORKDAY_REQUISITION = re.compile(r"_[a-z]*-?([0-9]{4,})[a-z]?(?:-[0-9]{1,2})?$")
_DIGITS = re.compile(r"[0-9]{4,}$")


@lru_cache(maxsize=16384)
def opening_key(url: str) -> str:
    try:
        parts = urlsplit(url.lower())
    except ValueError:
        return ""
    host = parts.hostname or ""
    if host == ZAPPLY_HOST:
        tenant, requisition = zapply_opening(parts.path)
        return f"{tenant}:{requisition}" if tenant else ""
    if not any(host.endswith(f".{domain}") for domain in WORKDAY_DOMAINS):
        board, posting = named_posting(url)
        return f"{board}{posting}" if posting else ""
    if "/job/" not in parts.path:
        return ""
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    found = _WORKDAY_REQUISITION.search(last)
    digits = _DIGITS.search(last.replace("-", "")) if is_workday_opening(url.lower()) else None
    requisition = found[1] if found else digits[0] if digits else ""
    return f"{host.split('.')[0]}:{requisition}" if requisition else ""
