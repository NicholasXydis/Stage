import re
import unicodedata
from collections.abc import Mapping
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
                f"{source}: 's' cannot be a feminine suffix, because it would strip the plural "
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
_GLUED_TERM = re.compile(
    r"\b(summer|fall|autumn|winter|spring|ete|hiver|automne|printemps)(20[0-9]{2})\b"
)


_LANGUAGES = (
    (re.compile(r"(?<![0-9a-z])c\s?\+\+"), " cpp "),
    (re.compile(r"(?<![0-9a-z])([cf])#(?![0-9])"), r" \1sharp "),
    (re.compile(r"(?<![0-9a-z])(asp|vb)?\.net(?![0-9a-z])"), r" \1 dotnet "),
)
_FOLD_CACHED = 256


def fold(raw: str) -> str:
    return _fold_short(raw) if len(raw) < _FOLD_CACHED else _fold_long(raw)


def fold_for_search(raw: str) -> str:
    return _fold(raw, languages=False)


@lru_cache(maxsize=65536)
def _fold_short(raw: str) -> str:
    return _fold(raw, languages=True)


@lru_cache(maxsize=8)
def _fold_long(raw: str) -> str:
    return _fold(raw, languages=True)


def _fold(raw: str, *, languages: bool) -> str:
    decomposed = unicodedata.normalize("NFKD", raw.replace("&", " and "))
    collapsed = _collapse_inclusive(decomposed.translate(_COMBINING).casefold())
    for pattern, spelled in _LANGUAGES if languages else ():
        collapsed = pattern.sub(spelled, collapsed)
    return _GLUED_TERM.sub(r"\1 \2", " ".join(re.sub(r"[^0-9a-z]+", " ", collapsed).split()))


def parse_yaml(text: str) -> Any:
    if hasattr(yaml, "CSafeLoader"):
        return yaml.load(text, Loader=yaml.CSafeLoader)
    return yaml.safe_load(text)


class LexiconError(Exception):
    pass


def _not_a_string(source: str, key: str, entry: object) -> str:
    if isinstance(entry, bool):
        word = "on/off" if entry else "no/off"
        return (
            f"{source}: {key!r} contains the boolean {entry!r}, which is YAML 1.1 reading a "
            f"bareword like {word} as a truth value. Quote it in the YAML"
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


def _folded_aliases(payload: dict[str, Any], key: str, source: str) -> dict[str, str]:
    raw = payload.get(key)
    if not isinstance(raw, dict) or not raw:
        raise LexiconError(f"{source}: {key!r} must be a non-empty mapping")
    aliases: dict[str, str] = {}
    for canonical, spellings in raw.items():
        if not isinstance(canonical, str) or not canonical.strip():
            raise LexiconError(_not_a_string(source, key, canonical))
        if not isinstance(spellings, list) or not spellings:
            raise LexiconError(f"{source}: {canonical!r} must list at least one spelling")
        for entry in spellings:
            if not isinstance(entry, str):
                raise LexiconError(_not_a_string(source, key, entry))
            folded = fold(entry)
            if not folded:
                raise LexiconError(f"{source}: {entry!r} folds to nothing")
            aliases[folded] = canonical
    return aliases


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


def _phrase_map(payload: dict[str, Any], key: str, source: str) -> dict[str, frozenset[str]]:
    raw = payload.get(key) or {}
    if not isinstance(raw, dict):
        raise LexiconError(f"{source}: {key!r} must be a mapping")
    mapped: dict[str, frozenset[str]] = {}
    for name, phrases in raw.items():
        if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
            raise LexiconError(f"{source}: {key}.{name} must be a list of strings")
        mapped[fold(str(name))] = frozenset(fold(phrase) for phrase in phrases)
    return mapped


def _optional_phrases(payload: dict[str, Any], key: str, source: str) -> frozenset[str]:
    return _folded_phrases(payload, key, source) if payload.get(key) else frozenset()


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
    usa_ambiguous: frozenset[str]
    usa_cities: frozenset[str]
    usa_regions: frozenset[str]
    usa_codes: frozenset[str]
    usa_country: frozenset[str]
    international: frozenset[str]
    international_cities: frozenset[str]
    country_codes: frozenset[str]
    city_aliases: dict[str, str]
    region_codes: dict[str, str]
    remote: frozenset[str]
    hybrid: frozenset[str]


def _load(filename: str) -> tuple[dict[str, Any], str]:
    path = lexicon_dir() / filename
    if not path.exists():
        raise LexiconError(f"lexicon not found at {path}")
    try:
        payload = parse_yaml(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise LexiconError(f"{path} is not valid UTF-8 YAML: {exc}") from exc
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
            f"{source}: {', '.join(sorted(overlap))} are listed as loanwords and also as "
            "language evidence. A loanword counts for neither side"
        )
    return LanguageLexicon(french=french, english=english, loanwords=loanwords)


@dataclass(frozen=True, slots=True)
class InternshipLexicon:
    markers: frozenset[str]
    blocked_bigrams: frozenset[str]
    disqualifiers: frozenset[str]
    structured_internship: frozenset[str]
    structured_excluded: frozenset[str]
    structured_only_blocked: frozenset[str]


@lru_cache(maxsize=1)
def internship_lexicon() -> InternshipLexicon:
    payload, source = _load(INTERNSHIP_FILE)
    return InternshipLexicon(
        markers=_folded_phrases(payload, "markers", source),
        blocked_bigrams=_folded_phrases(payload, "blocked_bigrams", source),
        disqualifiers=_folded_phrases(payload, "disqualifiers", source),
        structured_internship=_folded_phrases(payload, "structured_internship", source),
        structured_excluded=_folded_phrases(payload, "structured_excluded", source),
        structured_only_blocked=_folded_phrases(payload, "structured_only_blocked", source),
    )


@dataclass(frozen=True, slots=True)
class QualifiedTitles:
    titles: frozenset[str]
    needs: frozenset[str]
    categories: frozenset[str] = frozenset()
    described_by: frozenset[str] = frozenset()


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
    graduate_by_convention: frozenset[str]
    graduate_by_employer: Mapping[str, frozenset[str]]
    undergraduate_by_employer: Mapping[str, frozenset[str]]
    non_cs_exceptions: frozenset[str] = frozenset()
    qualified_titles: tuple[QualifiedTitles, ...] = ()


def _qualified_titles(payload: dict[str, Any], source: str) -> tuple[QualifiedTitles, ...]:
    raw = payload.get("qualified_titles")
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise LexiconError(f"{source}: 'qualified_titles' must be a mapping of families")
    families: list[QualifiedTitles] = []
    for name, entry in raw.items():
        where = f"{source} qualified_titles.{name}"
        if not isinstance(entry, dict):
            raise LexiconError(f"{where} must hold 'titles' and 'needs'")
        families.append(
            QualifiedTitles(
                titles=_folded_phrases(entry, "titles", where),
                needs=_folded_phrases(entry, "needs", where),
                categories=_optional_phrases(entry, "categories", where),
                described_by=_optional_phrases(entry, "described_by", where),
            )
        )
    return tuple(families)


@lru_cache(maxsize=1)
def eligibility_lexicon() -> EligibilityLexicon:
    payload, source = _load(ELIGIBILITY_FILE)
    if not isinstance(payload.get("degree_required"), dict):
        raise LexiconError(f"{source}: 'degree_required' must be a mapping")
    degrees = _phrase_map(payload, "degree_required", source)
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
        graduate_by_convention=_folded_phrases(payload, "graduate_by_convention", source),
        graduate_by_employer=_phrase_map(payload, "graduate_by_employer", source),
        undergraduate_by_employer=_phrase_map(payload, "undergraduate_by_employer", source),
        non_cs_exceptions=_optional_phrases(payload, "non_cs_exceptions", source),
        qualified_titles=_qualified_titles(payload, source),
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


@lru_cache(maxsize=1)
def role_pairs() -> dict[str, tuple[tuple[str, str], ...]]:
    payload, source = _load(ROLES_FILE)
    raw = payload.get("paired", {})
    if not isinstance(raw, dict):
        raise LexiconError(f"{source}: 'paired' must map role categories to phrase pairs")
    pairs: dict[str, tuple[tuple[str, str], ...]] = {}
    for category, entries in raw.items():
        if category not in ROLE_CATEGORIES:
            raise LexiconError(f"{source}: {category!r} is not a role category")
        found: list[tuple[str, str]] = []
        for entry in entries if isinstance(entries, list) else [entries]:
            parts = [fold(part) for part in entry] if isinstance(entry, list) else []
            if len(parts) != 2 or not all(parts):
                raise LexiconError(f"{source}: paired.{category} entries are [phrase, phrase]")
            found.append((parts[0], parts[1]))
        pairs[category] = tuple(found)
    return pairs


@dataclass(frozen=True, slots=True)
class TermLexicon:
    seasons: dict[str, frozenset[str]]
    fillers: frozenset[str]
    blocked_bigrams: frozenset[str]
    graduation_words: frozenset[str]


@lru_cache(maxsize=1)
def term_lexicon() -> TermLexicon:
    payload, source = _load(TERMS_FILE)
    return TermLexicon(
        seasons={season: _folded_phrases(payload, season, source) for season in SEASONS},
        fillers=_folded_phrases(payload, "fillers", source),
        blocked_bigrams=_folded_phrases(payload, "blocked_bigrams", source),
        graduation_words=_folded_phrases(payload, "graduation_words", source),
    )


@lru_cache(maxsize=1)
def accent_dropped_words() -> dict[str, str]:
    payload, _ = _load(LOCATIONS_FILE)
    found: dict[str, str] = {}
    for key in ("montreal", "canada_cities", "canada_regions", "usa_cities", "usa_regions"):
        for name in payload.get(key) or ():
            for word in str(name).replace("-", " ").split():
                stripped = word.encode("ascii", "ignore").decode().lower()
                if not word.isascii() and len(stripped) >= 4:
                    found[stripped] = word
    return found


@lru_cache(maxsize=1)
def location_lexicon() -> LocationLexicon:
    payload, source = _load(LOCATIONS_FILE)
    return LocationLexicon(
        montreal=_folded_phrases(payload, "montreal", source),
        montreal_ambiguous=_folded_phrases(payload, "montreal_ambiguous", source),
        canada_cities=_folded_phrases(payload, "canada_cities", source),
        canada_ambiguous=_folded_phrases(payload, "canada_ambiguous", source),
        usa_ambiguous=_folded_phrases(payload, "usa_ambiguous", source),
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
        country_codes=_folded_phrases(payload, "country_codes", source),
        city_aliases=_folded_aliases(payload, "city_aliases", source),
        region_codes=(
            _folded_aliases(payload, "region_codes", source) if "region_codes" in payload else {}
        ),
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


@lru_cache(maxsize=1)
def employer_spellings() -> dict[str, str]:
    payload, source = _load(COMPANY_TOKENS_FILE)
    if not payload.get("employer_spellings"):
        return {}
    return _folded_aliases(payload, "employer_spellings", source)


@lru_cache(maxsize=1)
def employer_aliases() -> dict[str, str]:
    payload, source = _load(COMPANY_TOKENS_FILE)
    if not payload.get("employer_aliases"):
        return {}
    named = _folded_aliases(payload, "employer_aliases", source)
    return {spelling: fold(canonical) for spelling, canonical in named.items()}
