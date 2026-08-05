
from dataclasses import dataclass

from stage.lexicon import fold, internship_lexicon


@dataclass(frozen=True, slots=True)
class InternshipVerdict:
    is_internship: bool = False
    matched: tuple[str, ...] = ()
    disqualified_by: str = ""


def _phrase_hits(folded: str, phrases: frozenset[str]) -> list[str]:
    padded = f" {folded} "
    return sorted(phrase for phrase in phrases if f" {phrase} " in padded)


def _blocked_positions(folded: str, blocked: frozenset[str]) -> str:
    padded = f" {folded} "
    for phrase in sorted(blocked):
        if f" {phrase} " in padded:
            return phrase
    return ""


def screen_internship(title: str) -> InternshipVerdict:
    folded_title = fold(title)
    lexicon = internship_lexicon()

    disqualifier = _blocked_positions(folded_title, lexicon.disqualifiers)
    if disqualifier:
        return InternshipVerdict(is_internship=False, disqualified_by=disqualifier)

    blocked = _blocked_positions(folded_title, lexicon.blocked_bigrams)
    matched = _phrase_hits(folded_title, lexicon.markers)
    if blocked and not [
        phrase for phrase in matched if f" {phrase} " not in f" {blocked} "
    ]:
        return InternshipVerdict(is_internship=False, disqualified_by=blocked)

    return InternshipVerdict(is_internship=bool(matched), matched=tuple(matched))
