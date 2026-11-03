import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from stage.domain import (
    SOURCE_PRIORITY,
    Job,
    Language,
    location_agrees,
    term_agrees,
)
from stage.domain.text import is_workday_opening
from stage.lexicon import (
    division_qualifiers,
    employer_aliases,
    fold,
    generic_company_tokens,
    name_root_tokens,
    role_lexicon,
)

__all__ = [
    "SOURCE_PRIORITY",
    "MatchKind",
    "MatchResult",
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
}


def _title_tokens(job: Job) -> frozenset[str]:
    return frozenset(_SAME_WORD.get(word, word) for word in fold(job.title_raw).split())


def _qualified_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    if not shorter or longer[: len(shorter)] != shorter:
        return False
    if not set(longer[len(shorter) :]) <= division_qualifiers():
        return False
    return bool(set(shorter) - generic_company_tokens())


_LABEL = re.compile(r"\s*\([^()]*\)\s*$")


def _company_matches(left: Job, right: Job) -> bool:
    if fold(left.company) == fold(right.company):
        return True
    first, second = _root(left.company), _root(right.company)
    if first == second:
        return True
    core = _core(first)
    if core == _core(second) and set(core) - generic_company_tokens():
        return True
    shorter, longer = sorted((first, second), key=len)
    return _qualified_prefix(shorter, longer)


def _root(name: str) -> tuple[str, ...]:
    joined = " ".join(name_root_tokens(_LABEL.sub("", name)))
    return tuple(employer_aliases().get(joined, joined).split())


def _core(tokens: tuple[str, ...]) -> tuple[str, ...]:
    qualifiers = division_qualifiers()
    end = len(tokens)
    while end > 1 and tokens[end - 1] in qualifiers:
        end -= 1
    return tokens[:end]


def would_merge(left: Job, right: Job) -> MatchResult:
    url = left.apply_url_canonical
    same_board = left.board_key == right.board_key
    if url and url == right.apply_url_canonical and (not same_board or is_workday_opening(url)):
        return MatchResult(MatchKind.URL, url)
    if same_board or _two_openings(url, right.apply_url_canonical):
        return MatchResult()

    if not _company_matches(left, right):
        return MatchResult()

    if not location_agrees(left.location, right.location) or _different_cities(left, right):
        return MatchResult()

    left_tokens, right_tokens = _title_tokens(left), _title_tokens(right)
    if left_tokens and left_tokens == right_tokens:
        return MatchResult(MatchKind.SAME_LANGUAGE, " ".join(sorted(left_tokens)))

    return _cross_language(left, right)


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
            return frozenset()
        shown = fold(_PLACE_PARTS.split(display_location(part), maxsplit=1)[0])
        if shown and shown not in _regions():
            found.add(shown)
    return frozenset(found)


def _different_cities(left: Job, right: Job) -> bool:
    one, two = _cities(left.location_raw or ""), _cities(right.location_raw or "")
    if not one or not two:
        return False
    return not any(a in b or b in a for a in one for b in two)


def _two_openings(left: str, right: str) -> bool:
    return left != right and is_workday_opening(left) and is_workday_opening(right)
