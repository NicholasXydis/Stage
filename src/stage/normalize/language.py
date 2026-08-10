import unicodedata
from dataclasses import dataclass

from stage.domain import Language
from stage.lexicon import fold, language_lexicon

_MIN_EVIDENCE = 2

_DOMINANCE = 2

_ACCENTED = frozenset("àâäçéèêëîïôöùûüÿœæ")


@dataclass(frozen=True, slots=True)
class DetectedLanguage:
    language: Language = Language.UNKNOWN
    french_hits: tuple[str, ...] = ()
    english_hits: tuple[str, ...] = ()


def _accent_count(raw: str) -> int:
    lowered = unicodedata.normalize("NFC", raw).casefold()
    return sum(1 for char in lowered if char in _ACCENTED)


def detect_language(title: str, description: str = "") -> DetectedLanguage:
    lexicon = language_lexicon()
    tokens = fold(title).split()
    if len(tokens) < 3 and description:
        tokens = tokens + fold(description).split()[:40]

    french = tuple(sorted({token for token in tokens if token in lexicon.french}))
    english = tuple(sorted({token for token in tokens if token in lexicon.english}))
    fr_score = len(french) + (1 if _accent_count(title) and french else 0)
    en_score = len(english)

    if fr_score >= _MIN_EVIDENCE and en_score >= _MIN_EVIDENCE:
        if fr_score >= en_score * _DOMINANCE:
            return DetectedLanguage(Language.FR, french, english)
        if en_score >= fr_score * _DOMINANCE:
            return DetectedLanguage(Language.EN, french, english)
        return DetectedLanguage(Language.BILINGUAL, french, english)
    if fr_score >= _MIN_EVIDENCE and en_score < _MIN_EVIDENCE:
        return DetectedLanguage(Language.FR, french, english)
    if en_score >= _MIN_EVIDENCE and fr_score < _MIN_EVIDENCE:
        return DetectedLanguage(Language.EN, french, english)
    return DetectedLanguage(Language.UNKNOWN, french, english)
