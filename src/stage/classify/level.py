import re
from dataclasses import dataclass

from stage.classify.internship import screen_internship
from stage.domain import JobLevel
from stage.lexicon import LevelLexicon, fold, level_lexicon

NO_MARKER = "no early-career marker in title"

_GROUPED = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FUSED_LEVEL = re.compile(r"l([1-5])")
_ASSOCIATE_BETWEEN = 3
_NEW_GRAD_BETWEEN = 5
_TRAILING = 2
_RANGE_JOINERS = frozenset({"to", "and", "or", "through"})


@dataclass(frozen=True, slots=True)
class LevelVerdict:
    level: JobLevel = JobLevel.UNKNOWN
    matched_phrase: str = ""

    @property
    def rejected(self) -> bool:
        return self.level is JobLevel.UNKNOWN


def _longest_hit(padded: str, phrases: frozenset[str]) -> str:
    hits = [phrase for phrase in phrases if f" {phrase} " in padded]
    return max(hits, key=lambda phrase: (len(phrase), phrase)) if hits else ""


def _scrub(folded: str, blocked: frozenset[str]) -> tuple[str, str]:
    padded, removed = f" {folded} ", ""
    for phrase in sorted(blocked, key=lambda phrase: (-len(phrase), phrase)):
        needle = f" {phrase} "
        if needle in padded:
            removed = removed or phrase
            padded = padded.replace(needle, " ")
    return " ".join(padded.split()), removed


def _tokens(title: str) -> list[str]:
    tokens: list[str] = []
    for token in fold(_GROUPED.sub(" ", title)).split():
        fused = _FUSED_LEVEL.fullmatch(token)
        tokens.extend(("l", fused[1]) if fused else (token,))
    return tokens


def _is_range(tokens: list[str], start: int, lexicon: LevelLexicon) -> bool:
    while start < len(tokens) and tokens[start] in _RANGE_JOINERS:
        start += 1
    numerals = lexicon.entry_levels | lexicon.seniority_levels
    return start < len(tokens) and tokens[start] in numerals


def _numbered(title: str, lexicon: LevelLexicon) -> tuple[str, str]:
    tokens = _tokens(title)
    entry = ""
    for index, token in enumerate(tokens):
        nearby = tokens[index + 1 : index + 3]
        if token in lexicon.level_words and nearby and nearby[0] in lexicon.seniority_levels:
            return f"{token} {nearby[0]}", ""
        if token not in lexicon.role_nouns or not nearby:
            continue
        place = 1 + (nearby[0] in lexicon.level_words)
        if place > len(nearby):
            continue
        value = nearby[place - 1]
        phrase = " ".join(tokens[index : index + 1 + place])
        if value in lexicon.seniority_levels:
            return phrase, ""
        if value in lexicon.entry_levels and not _is_range(tokens, index + 1 + place, lexicon):
            entry = entry or phrase
    return "", entry


def _attached(folded: str, words: frozenset[str], nouns: frozenset[str], between: int) -> str:
    tokens = folded.split()
    for index, token in enumerate(tokens):
        if token not in words:
            continue
        for offset, other in enumerate(tokens[index + 1 : index + 2 + between]):
            if other in nouns:
                return " ".join(tokens[index : index + offset + 2])
    return ""


def _trailing(folded: str, words: frozenset[str], nouns: frozenset[str]) -> str:
    tokens = folded.split()
    for index, token in enumerate(tokens):
        if token not in nouns:
            continue
        for offset, other in enumerate(tokens[index + 1 : index + 1 + _TRAILING]):
            if other in words:
                return " ".join(tokens[index : index + offset + 2])
    return ""


def classify_early_career(title: str) -> LevelVerdict:
    lexicon = level_lexicon()
    folded = fold(title)

    senior = _longest_hit(f" {folded} ", lexicon.seniority)
    ranked, level_phrase = _numbered(title, lexicon)
    if senior or ranked:
        return LevelVerdict(matched_phrase=senior or ranked)

    scrubbed, blocked = _scrub(folded, lexicon.blocked)
    padded = f" {scrubbed} "

    if phrase := (
        _longest_hit(padded, lexicon.new_grad)
        or _attached(scrubbed, lexicon.new_grad_words, lexicon.role_nouns, _NEW_GRAD_BETWEEN)
        or _trailing(scrubbed, lexicon.new_grad_words, lexicon.role_nouns)
    ):
        return LevelVerdict(JobLevel.NEW_GRAD, phrase)
    if phrase := (
        _longest_hit(padded, lexicon.entry)
        or _attached(scrubbed, lexicon.associate_words, lexicon.role_nouns, _ASSOCIATE_BETWEEN)
        or level_phrase
    ):
        return LevelVerdict(JobLevel.ENTRY, phrase)
    return LevelVerdict(matched_phrase=blocked)


def screen_level(title: str, employment_type: str = "", category: str = "") -> LevelVerdict:
    internship = screen_internship(title, employment_type, category)
    if internship.is_internship:
        return LevelVerdict(JobLevel.INTERNSHIP, ", ".join(internship.matched))
    verdict = classify_early_career(title)
    if verdict.rejected and not verdict.matched_phrase and not internship.disqualified_by:
        hint = _longest_hit(f" {fold(employment_type)} ", level_lexicon().structured_new_grad)
        if hint:
            return LevelVerdict(JobLevel.NEW_GRAD, hint)
    if verdict.rejected and not verdict.matched_phrase:
        return LevelVerdict(matched_phrase=internship.disqualified_by or NO_MARKER)
    return verdict
