import re

_TERM = re.compile(r"^[0-9a-z]+$")

MAX_TERM_LENGTH = 48
MAX_TERMS = 32


def search_terms(query: str) -> tuple[str, ...]:
    from stage.lexicon import fold

    found = [term[:MAX_TERM_LENGTH] for term in fold(query).split() if _TERM.match(term)]
    return tuple(found[:MAX_TERMS])


def match_expression(terms: tuple[str, ...]) -> str:
    return " ".join(f'"{term}"*' for term in terms)


__all__ = [
    "MAX_TERMS",
    "MAX_TERM_LENGTH",
    "match_expression",
    "search_terms",
]
