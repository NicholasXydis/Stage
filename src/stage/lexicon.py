import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from stage.paths import lexicon_dir

COMPANY_TOKENS_FILE = "company_tokens.yaml"
LOCATIONS_FILE = "locations.yaml"
INCLUSIVE_SUFFIXES_FILE = "inclusive_suffixes.yaml"
TERMS_FILE = "terms.yaml"
INTERNSHIP_FILE = "internship.yaml"
WORKDAY_FACETS_FILE = "workday_facets.yaml"
ROLES_FILE = "roles.yaml"
ELIGIBILITY_FILE = "eligibility.yaml"
LANGUAGE_FILE = "language.yaml"


def _bare(raw: str) -> str:
    decomposed = unicodedata.normalize("NFKD", raw)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z]+", "", stripped.casefold())


@lru_cache(maxsize=1)
def feminine_suffixes() -> frozenset[str]:
    payload, source = _load(INCLUSIVE_SUFFIXES_FILE)
    raw = payload.get("feminine_suffixes")
    if not isinstance(raw, list) or not raw:
        raise LexiconError(f"{source}: 'feminine_suffixes' must be a non-empty list")
    suffixes: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise LexiconError(f"{source}: every suffix must be a string")
        bare = _bare(entry)
        if not bare:
            raise LexiconError(f"{source}: {entry!r} normalizes to nothing")
        if bare == "s":
            raise LexiconError(
                f"{source}: 's' cannot be a feminine suffix — it would strip the plural "
                "from 'Engineer(s)'"
            )
        suffixes.add(bare)
    return frozenset(suffixes)


LONGEST_WORD_STEM = 30

_INCLUSIVE_MIDDOT = re.compile(rf"([a-z]{{3,{LONGEST_WORD_STEM}}})\s*·\s*([a-z]{{1,6}})")
_INCLUSIVE_TIGHT = re.compile(rf"([a-z]{{3,{LONGEST_WORD_STEM}}})[.(]([a-z]{{1,6}})\)?")


def _collapse_inclusive(text: str) -> str:

    suffixes = feminine_suffixes()

    def replace(match: re.Match[str]) -> str:
        if match.group(2) in suffixes:
            return match.group(1)
        return match.group(0)

    return _INCLUSIVE_TIGHT.sub(replace, _INCLUSIVE_MIDDOT.sub(replace, text))


_COMBINING = dict.fromkeys(range(0x0300, 0x0370))


@lru_cache(maxsize=8)
def fold(raw: str) -> str:
    decomposed = unicodedata.normalize("NFKD", raw.replace("&", " and "))
    collapsed = _collapse_inclusive(decomposed.translate(_COMBINING).casefold())
    return " ".join(re.sub(r"[^0-9a-z]+", " ", collapsed).split())


class LexiconError(Exception):
    pass


def _not_a_string(source: str, key: str, entry: object) -> str:
    if isinstance(entry, bool):
        word = "on/off" if entry else "no/off"
        return (
            f"{source}: {key!r} contains the boolean {entry!r}, which is YAML 1.1 reading a "
            f"bareword like {word} as a truth value — quote it in the YAML"
        )
    return f"{source}: every {key!r} entry must be a string, found {entry!r}"


def _folded_set(payload: dict[str, Any], key: str, source: str) -> frozenset[str]:
    raw = payload.get(key)
    if not isinstance(raw, list) or not raw:
        raise LexiconError(f"{source}: {key!r} must be a non-empty list")
    tokens: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise LexiconError(_not_a_string(source, key, entry))
        folded = fold(entry)
        if not folded:
            raise LexiconError(f"{source}: {entry!r} folds to nothing")
        tokens.update(folded.split())
    return frozenset(tokens)


def _folded_phrases(payload: dict[str, Any], key: str, source: str) -> frozenset[str]:
    raw = payload.get(key)
    if not isinstance(raw, list) or not raw:
        raise LexiconError(f"{source}: {key!r} must be a non-empty list")
    phrases: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise LexiconError(_not_a_string(source, key, entry))
        folded = fold(entry)
        if not folded:
            raise LexiconError(f"{source}: {entry!r} folds to nothing")
        phrases.add(folded)
    return frozenset(phrases)


@dataclass(frozen=True, slots=True)
class LocationLexicon:
    montreal: frozenset[str]
    montreal_ambiguous: frozenset[str]
    canada_cities: frozenset[str]
    canada_ambiguous: frozenset[str]
    canada_regions: frozenset[str]
    canada_codes: frozenset[str]
    canada_country: frozenset[str]
    canada_overrides: frozenset[str]
    usa_cities: frozenset[str]
    usa_regions: frozenset[str]
    usa_codes: frozenset[str]
    usa_country: frozenset[str]
    international: frozenset[str]
    international_cities: frozenset[str]
    remote: frozenset[str]
    hybrid: frozenset[str]


def _load(filename: str) -> tuple[dict[str, Any], str]:
    path = lexicon_dir() / filename
    if not path.exists():
        raise LexiconError(f"lexicon not found at {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LexiconError(f"{path}: expected a mapping")
    return payload, str(path)


SEASONS: tuple[str, ...] = ("summer", "fall", "winter", "spring")
ROLE_CATEGORIES: tuple[str, ...] = (
    "swe",
    "security",
    "data",
    "ml-ai",
    "quant",
    "infra",
    "embedded",
    "general-cs",
)


@dataclass(frozen=True, slots=True)
class LanguageLexicon:
    french: frozenset[str]
    english: frozenset[str]
    loanwords: frozenset[str]


@lru_cache(maxsize=1)
def language_lexicon() -> LanguageLexicon:
    payload, source = _load(LANGUAGE_FILE)
    loanwords = _folded_set(payload, "loanwords", source)
    french = _folded_set(payload, "french", source)
    english = _folded_set(payload, "english", source)
    overlap = (french | english) & loanwords
    if overlap:
        raise LexiconError(
            f"{source}: {sorted(overlap)} are listed as loanwords and also as language "
            "evidence — a loanword counts for neither side"
        )
    return LanguageLexicon(french=french, english=english, loanwords=loanwords)


@dataclass(frozen=True, slots=True)
class InternshipLexicon:
    markers: frozenset[str]
    blocked_bigrams: frozenset[str]
    disqualifiers: frozenset[str]
    structured_internship: frozenset[str]


@lru_cache(maxsize=1)
def internship_lexicon() -> InternshipLexicon:
    payload, source = _load(INTERNSHIP_FILE)
    return InternshipLexicon(
        markers=_folded_phrases(payload, "markers", source),
        blocked_bigrams=_folded_phrases(payload, "blocked_bigrams", source),
        disqualifiers=_folded_phrases(payload, "disqualifiers", source),
        structured_internship=_folded_phrases(payload, "structured_internship", source),
    )


@dataclass(frozen=True, slots=True)
class EligibilityLexicon:
    degree_required: dict[str, frozenset[str]]
    work_auth_excluded: frozenset[str]
    non_cs: frozenset[str]
    excluded_titles: frozenset[str]
    technical_title_exceptions: frozenset[str]
    phd_required: frozenset[str]
    phd_title_tokens: frozenset[str]
    degree_list_tokens: frozenset[str]
    undergraduate_tokens: frozenset[str]
    graduate_title_tokens: frozenset[str]


@lru_cache(maxsize=1)
def eligibility_lexicon() -> EligibilityLexicon:
    payload, source = _load(ELIGIBILITY_FILE)
    raw = payload.get("degree_required")
    if not isinstance(raw, dict):
        raise LexiconError(f"{source}: 'degree_required' must be a mapping")
    degrees: dict[str, frozenset[str]] = {}
    for level, phrases in raw.items():
        if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
            raise LexiconError(f"{source}: degree_required.{level} must be a list of strings")
        degrees[str(level)] = frozenset(fold(phrase) for phrase in phrases)
    return EligibilityLexicon(
        degree_required=degrees,
        work_auth_excluded=_folded_phrases(payload, "work_auth_excluded", source),
        non_cs=_folded_phrases(payload, "non_cs", source),
        excluded_titles=_folded_phrases(payload, "excluded_titles", source),
        technical_title_exceptions=_folded_phrases(payload, "technical_title_exceptions", source),
        phd_required=_folded_phrases(payload, "phd_required", source),
        phd_title_tokens=_folded_phrases(payload, "phd_title_tokens", source),
        degree_list_tokens=_folded_phrases(payload, "degree_list_tokens", source),
        undergraduate_tokens=_folded_phrases(payload, "undergraduate_tokens", source),
        graduate_title_tokens=_folded_phrases(payload, "graduate_title_tokens", source),
    )


@lru_cache(maxsize=1)
def workday_facet_lexicon() -> tuple[tuple[str, ...], frozenset[str]]:
    payload, source = _load(WORKDAY_FACETS_FILE)
    raw = payload.get("facet_parameters")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise LexiconError(f"{source}: 'facet_parameters' must be a list of strings")
    return tuple(raw), frozenset(_folded_phrases(payload, "intern_descriptors", source))


@lru_cache(maxsize=1)
def source_role_categories() -> dict[str, str]:
    payload, source = _load(ROLES_FILE)
    raw = payload.get("source_categories")
    if not isinstance(raw, dict):
        raise LexiconError(f"{source}: 'source_categories' must be a mapping")
    mapping: dict[str, str] = {}
    for category, labels in raw.items():
        if category not in ROLE_CATEGORIES:
            raise LexiconError(f"{source}: {category!r} is not a role category")
        for label in labels:
            mapping[fold(str(label))] = category
    return mapping


@lru_cache(maxsize=1)
def role_lexicon() -> dict[str, frozenset[str]]:
    payload, source = _load(ROLES_FILE)
    return {name: _folded_phrases(payload, name, source) for name in ROLE_CATEGORIES}


@dataclass(frozen=True, slots=True)
class TermLexicon:
    seasons: dict[str, frozenset[str]]
    fillers: frozenset[str]
    blocked_bigrams: frozenset[str]


@lru_cache(maxsize=1)
def term_lexicon() -> TermLexicon:
    payload, source = _load(TERMS_FILE)
    return TermLexicon(
        seasons={season: _folded_phrases(payload, season, source) for season in SEASONS},
        fillers=_folded_phrases(payload, "fillers", source),
        blocked_bigrams=_folded_phrases(payload, "blocked_bigrams", source),
    )


@lru_cache(maxsize=1)
def location_lexicon() -> LocationLexicon:
    payload, source = _load(LOCATIONS_FILE)
    return LocationLexicon(
        montreal=_folded_phrases(payload, "montreal", source),
        montreal_ambiguous=_folded_phrases(payload, "montreal_ambiguous", source),
        canada_cities=_folded_phrases(payload, "canada_cities", source),
        canada_ambiguous=_folded_phrases(payload, "canada_ambiguous", source),
        canada_regions=_folded_phrases(payload, "canada_regions", source),
        canada_codes=_folded_phrases(payload, "canada_codes", source),
        canada_country=_folded_phrases(payload, "canada_country", source),
        canada_overrides=_folded_phrases(payload, "canada_overrides", source),
        usa_cities=_folded_phrases(payload, "usa_cities", source),
        usa_regions=_folded_phrases(payload, "usa_regions", source),
        usa_codes=_folded_phrases(payload, "usa_codes", source),
        usa_country=_folded_phrases(payload, "usa_country", source),
        international=_folded_phrases(payload, "international", source),
        international_cities=_folded_phrases(payload, "international_cities", source),
        remote=_folded_phrases(payload, "remote", source),
        hybrid=_folded_phrases(payload, "hybrid", source),
    )


@lru_cache(maxsize=1)
def _company_tokens() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    payload, source = _load(COMPANY_TOKENS_FILE)
    return (
        _folded_set(payload, "generic", source),
        _folded_set(payload, "legal_suffixes", source),
        _folded_set(payload, "division_qualifiers", source),
    )


def generic_company_tokens() -> frozenset[str]:
    return _company_tokens()[0]


def company_legal_suffixes() -> frozenset[str]:
    return _company_tokens()[1]


def name_root_tokens(name: str) -> tuple[str, ...]:
    suffixes = company_legal_suffixes()
    tokens = [token for token in fold(name).split() if token]
    while len(tokens) > 1 and tokens[-1] in suffixes:
        tokens = tokens[:-1]
    return tuple(tokens)


def division_qualifiers() -> frozenset[str]:
    return _company_tokens()[2]
