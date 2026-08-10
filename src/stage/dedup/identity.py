from dataclasses import dataclass
from enum import StrEnum

from stage.domain import (
    SOURCE_PRIORITY,
    Job,
    Language,
    location_agrees,
    source_rank,
    term_agrees,
)
from stage.lexicon import fold, role_lexicon

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


def _title_tokens(job: Job) -> frozenset[str]:
    return frozenset(fold(job.title_raw).split())


def _company_matches(left: Job, right: Job) -> bool:
    return fold(left.company) == fold(right.company)


def would_merge(left: Job, right: Job) -> MatchResult:
    if left.board_key == right.board_key:
        return MatchResult()

    if left.apply_url_canonical and left.apply_url_canonical == right.apply_url_canonical:
        return MatchResult(MatchKind.URL, left.apply_url_canonical)

    if not _company_matches(left, right):
        return MatchResult()

    if not location_agrees(left.location, right.location):
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


def _rank(job: Job) -> tuple[int, str]:
    return source_rank(job.source, job.id)
