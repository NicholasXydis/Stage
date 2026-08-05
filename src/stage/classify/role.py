
from dataclasses import dataclass
from functools import lru_cache

from stage.domain import RoleCategory
from stage.lexicon import fold, role_lexicon, source_role_categories


@lru_cache(maxsize=1)
def _by_first_token() -> dict[str, tuple[tuple[str, str], ...]]:
    index: dict[str, list[tuple[str, str]]] = {}
    for category, phrases in role_lexicon().items():
        for phrase in phrases:
            index.setdefault(phrase.split(" ", 1)[0], []).append((category, phrase))
    return {token: tuple(entries) for token, entries in index.items()}


@dataclass(frozen=True, slots=True)
class RoleVerdict:
    role: RoleCategory = RoleCategory.UNKNOWN
    matched: tuple[str, ...] = ()
    ambiguous: bool = False


def classify_role(title: str, description: str = "", source_category: str = "") -> RoleVerdict:
    index = _by_first_token()
    declared = source_role_categories().get(fold(source_category)) if source_category else None
    for text in (title, description):
        folded = fold(text)
        if not folded:
            continue
        padded = f" {folded} "
        hits: dict[str, list[str]] = {}
        for token in set(folded.split()):
            for category, phrase in index.get(token, ()):
                if f" {phrase} " in padded:
                    hits.setdefault(category, []).append(phrase)
        if not hits:
            continue
        hits = {
            category: sorted(set(found), key=len, reverse=True)
            for category, found in hits.items()
        }

        best = max(len(phrases[0]) for phrases in hits.values())
        winners = [category for category, phrases in hits.items() if len(phrases[0]) == best]
        matched = tuple(sorted({phrase for phrases in hits.values() for phrase in phrases}))
        if len(winners) != 1:
            return RoleVerdict(role=RoleCategory.UNKNOWN, matched=matched, ambiguous=True)
        return RoleVerdict(role=RoleCategory(winners[0]), matched=matched)
    if declared is not None:
        return RoleVerdict(role=RoleCategory(declared), matched=(fold(source_category),))
    return RoleVerdict()
