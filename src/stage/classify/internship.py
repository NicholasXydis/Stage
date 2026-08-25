from dataclasses import dataclass

from stage.domain import Language
from stage.lexicon import fold, internship_lexicon
from stage.normalize import detect_language


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


def _structured_hit(employment_type: str, values: frozenset[str]) -> str:
    hits = _phrase_hits(fold(employment_type), values)
    return max(hits, key=len) if hits else ""


def screen_internship(
    title: str, employment_type: str = "", category: str = ""
) -> InternshipVerdict:
    folded_title = fold(title)
    lexicon = internship_lexicon()

    disqualifier = _blocked_positions(folded_title, lexicon.disqualifiers)
    if disqualifier:
        return InternshipVerdict(is_internship=False, disqualified_by=disqualifier)

    excluded = _structured_hit(category, lexicon.structured_excluded)
    if excluded:
        return InternshipVerdict(is_internship=False, disqualified_by=excluded)

    blocked = _blocked_positions(folded_title, lexicon.blocked_bigrams)
    matched = _phrase_hits(folded_title, lexicon.markers)
    if {"stage", "stages"}.intersection(matched) and detect_language(title).language not in {
        Language.FR,
        Language.BILINGUAL,
    }:
        matched = [phrase for phrase in matched if phrase not in {"stage", "stages"}]
    if blocked and not [phrase for phrase in matched if f" {phrase} " not in f" {blocked} "]:
        return InternshipVerdict(is_internship=False, disqualified_by=blocked)

    structured = _structured_hit(employment_type, lexicon.structured_internship)
    if structured and not matched:
        senior = _blocked_positions(folded_title, lexicon.structured_only_blocked)
        if senior:
            return InternshipVerdict(is_internship=False, disqualified_by=senior)

    evidence = tuple(matched) + ((structured,) if structured else ())
    return InternshipVerdict(is_internship=bool(evidence), matched=evidence)
