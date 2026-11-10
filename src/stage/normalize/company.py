from functools import lru_cache

from stage.domain.text import plain_company
from stage.lexicon import employer_spellings, fold


@lru_cache(maxsize=8192)
def shown_company(name: str) -> str:
    plain = plain_company(name)
    return employer_spellings().get(fold(plain), plain)
