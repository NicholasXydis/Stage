from dataclasses import dataclass
from functools import lru_cache

from stage.domain import RoleCategory
from stage.lexicon import fold, role_lexicon, source_role_categories

GENERAL_CS = RoleCategory.GENERAL_CS.value
SWE = RoleCategory.SWE.value
TECHNOLOGY_NOT_DISCIPLINE = frozenset({"gpu", "fpga", "soc", "asic", "rtos", "cuda"})
OUTRANKED_BY_A_LEADING_QUALIFIER = (SWE,)


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


def _leading_specialist(folded: str, hits: dict[str, list[str]]) -> str | None:
    outranked = [category for category in OUTRANKED_BY_A_LEADING_QUALIFIER if category in hits]
    if not outranked or len(hits) < 2:
        return None
    if len(outranked) > 1:
        return min(outranked, key=lambda c: min(folded.find(p) for p in hits[c]))
    generic = min(folded.find(phrase) for category in outranked for phrase in hits[category])
    ahead = {
        category: min(
            folded.find(phrase) for phrase in phrases if phrase not in TECHNOLOGY_NOT_DISCIPLINE
        )
        for category, phrases in hits.items()
        if category not in outranked
        and any(phrase not in TECHNOLOGY_NOT_DISCIPLINE for phrase in phrases)
    }
    ahead = {category: at for category, at in ahead.items() if 0 <= at < generic}
    if len(ahead) != 1:
        return None
    return next(iter(ahead))


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
        if len(hits) > 1:
            hits.pop(GENERAL_CS, None)
        hits = {
            category: sorted(set(found), key=len, reverse=True) for category, found in hits.items()
        }

        leading = _leading_specialist(folded, hits) if text is title else None
        if leading is not None:
            matched = tuple(sorted({phrase for phrases in hits.values() for phrase in phrases}))
            return RoleVerdict(role=RoleCategory(leading), matched=matched)

        best = max(len(phrases[0]) for phrases in hits.values())
        winners = [category for category, phrases in hits.items() if len(phrases[0]) == best]
        matched = tuple(sorted({phrase for phrases in hits.values() for phrase in phrases}))
        if len(winners) != 1:
            winner = min(
                winners,
                key=lambda category: (
                    min(folded.find(phrase) for phrase in hits[category] if len(phrase) == best),
                    category,
                ),
            )
            return RoleVerdict(role=RoleCategory(winner), matched=matched)
        return RoleVerdict(role=RoleCategory(winners[0]), matched=matched)
    if declared is not None:
        return RoleVerdict(role=RoleCategory(declared), matched=(fold(source_category),))
    return RoleVerdict()
