import re
from dataclasses import dataclass
from functools import lru_cache

from stage.domain import LocationBucket, RemoteScope
from stage.lexicon import LocationLexicon, fold, location_lexicon

_SEGMENT_SPLIT = re.compile(r"[;/•|\n]+")
_FIELD_SPLIT = re.compile(r"[,\-]+")
_SUBDIVISION_CODE = re.compile(r"[A-Z]{1,3}")
_COUNTRY_PREFIXES = frozenset({"can", "us", "usa"})


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    bucket: LocationBucket = LocationBucket.UNKNOWN
    remote_scope: RemoteScope | None = None


@dataclass(frozen=True, slots=True)
class _Segment:
    montreal: bool
    canada: bool
    usa: bool
    international: bool
    remote: bool


class _PhraseIndex:
    __slots__ = ("_by_first_token",)

    def __init__(self, categories: dict[str, frozenset[str]]) -> None:
        index: dict[str, list[tuple[str, str]]] = {}
        for category, phrases in categories.items():
            for phrase in phrases:
                index.setdefault(phrase.split(" ", 1)[0], []).append((category, phrase))
        self._by_first_token = {token: tuple(entries) for token, entries in index.items()}

    def hits(self, folded: str) -> set[tuple[str, str]]:
        padded = f" {folded} "
        found: set[tuple[str, str]] = set()
        for token in folded.split():
            for entry in self._by_first_token.get(token, ()):
                if f" {entry[1]} " in padded:
                    found.add(entry)
        return found


@lru_cache(maxsize=1)
def _index() -> _PhraseIndex:
    lexicon = location_lexicon()
    return _PhraseIndex(
        {
            "montreal": lexicon.montreal,
            "montreal_ambiguous": lexicon.montreal_ambiguous,
            "canada_cities": lexicon.canada_cities,
            "canada_ambiguous": lexicon.canada_ambiguous,
            "canada_regions": lexicon.canada_regions,
            "canada_country": lexicon.canada_country,
            "canada_overrides": lexicon.canada_overrides,
            "usa_ambiguous": lexicon.usa_ambiguous,
            "usa_cities": lexicon.usa_cities,
            "usa_regions": lexicon.usa_regions,
            "usa_country": lexicon.usa_country,
            "international": lexicon.international,
            "international_cities": lexicon.international_cities,
            "remote": lexicon.remote,
        }
    )


def _foreign_subdivision_before(fields: list[str], index: int, known: frozenset[str]) -> bool:
    for previous in reversed(fields[:index]):
        stripped = previous.strip()
        if not stripped:
            continue
        if not _SUBDIVISION_CODE.fullmatch(stripped):
            return False
        folded = fold(stripped)
        return folded not in known and folded not in _COUNTRY_PREFIXES
    return False


def _last_alphabetic_field(fields: list[str]) -> int:
    for index in reversed(range(len(fields))):
        if any(character.isalpha() for character in fields[index]):
            return index
    return -1


def _reads_as_country(fields: list[str], index: int, token: str, known: frozenset[str]) -> bool:
    if index != _last_alphabetic_field(fields):
        return False
    if fold(fields[index].strip()) != token:
        return False
    return _foreign_subdivision_before(fields, index, known)


def _code_hits(segment: str, lexicon: LocationLexicon) -> tuple[bool, bool]:
    canada = usa = False
    fields = _FIELD_SPLIT.split(segment)
    known = lexicon.canada_codes | lexicon.usa_codes
    for index, field in enumerate(fields):
        for token in fold(field).split():
            if token not in known:
                continue
            if not re.search(rf"\b{token.upper()}\b", segment):
                continue
            if _reads_as_country(fields, index, token, known):
                continue
            if token in lexicon.canada_codes:
                canada = True
            else:
                usa = True
    return canada, usa


_FOREIGN_ONLY_CODES = frozenset({"de", "es", "ie", "in", "it", "fr", "uk", "nl", "pl", "br", "cn"})


def _trails_a_foreign_code(segment: str) -> bool:
    lexicon = location_lexicon()
    fields = [field.strip() for field in _FIELD_SPLIT.split(segment) if field.strip()]
    if len(fields) < 2:
        return False
    known = lexicon.canada_codes | lexicon.usa_codes
    for field in fields[1:]:
        folded = fold(field)
        if folded in _FOREIGN_ONLY_CODES:
            return True
        if folded and len(folded) <= 3 and folded not in known and not folded.isdigit():
            return True
    return False


def _resolve_segment(segment: str, lexicon: LocationLexicon) -> _Segment:
    folded = fold(segment)
    if not folded:
        return _Segment(False, False, False, False, False)
    hits = _index().hits(folded)
    found = {category for category, _ in hits}
    international_phrases = {
        phrase for category, phrase in hits if category in {"international", "international_cities"}
    }
    domestic_phrases = {
        phrase
        for category, phrase in hits
        if category in {"canada_cities", "canada_regions", "usa_cities", "usa_regions"}
    }
    international = any(
        not any(
            len(domestic) > len(phrase) and f" {phrase} " in f" {domestic} "
            for domestic in domestic_phrases
        )
        for phrase in international_phrases
    )
    code_canada, code_usa = _code_hits(segment, lexicon)

    override_phrases = {phrase for category, phrase in hits if category == "canada_overrides"}
    named_canada = {
        phrase
        for category, phrase in hits
        if category in {"canada_cities", "canada_ambiguous", "montreal", "montreal_ambiguous"}
    }
    overridden = bool(override_phrases) and all(
        any(f" {phrase} " in f" {override} " for override in override_phrases)
        for phrase in named_canada
    )
    ambiguous_here = bool(found & {"canada_ambiguous", "montreal_ambiguous", "usa_ambiguous"})
    coded = (
        (code_canada or code_usa)
        and ambiguous_here
        and not _trails_a_foreign_code(segment)
        and not found & {"international"}
    )
    canada_context = (
        "canada_country" in found
        or "canada_regions" in found
        or (code_canada and (not international or coded))
    ) and not overridden

    montreal = ("montreal" in found and not overridden) or (
        "montreal_ambiguous" in found and canada_context
    )
    canada = (
        montreal
        or canada_context
        or ("canada_cities" in found and not overridden and not international)
        or ("canada_ambiguous" in found and canada_context)
    )
    usa = "usa_country" in found or (
        ("usa_regions" in found or "usa_cities" in found or code_usa)
        and (not international or (code_usa and coded))
    )
    return _Segment(
        montreal=montreal,
        canada=canada,
        usa=usa,
        international=international and not (coded and (canada or usa)),
        remote="remote" in found,
    )


def _scope(segments: list[_Segment]) -> RemoteScope | None:
    scopes = {
        RemoteScope.CANADA
        if segment.canada
        else RemoteScope.US
        if segment.usa
        else RemoteScope.UNSPECIFIED
        for segment in segments
        if segment.remote
    }
    if not scopes:
        return None
    for candidate in (RemoteScope.CANADA, RemoteScope.UNSPECIFIED, RemoteScope.US):
        if candidate in scopes:
            return candidate
    return RemoteScope.UNSPECIFIED


_MULTI_SITE_SUFFIX = re.compile(r"\s*\+\s*\d+\s*$")


def display_location(raw: str) -> str:
    if not raw or not raw.strip():
        return ""

    aliases = location_lexicon().city_aliases
    first = _MULTI_SITE_SUFFIX.sub("", _SEGMENT_SPLIT.split(raw)[0].strip())
    if not first:
        return raw.strip()

    others = len([part for part in _SEGMENT_SPLIT.split(raw)[1:] if part.strip()])
    more = f" +{others}" if others else ""

    fields = [field.strip() for field in first.split(",") if field.strip()]
    if not fields:
        return raw.strip()

    canonical = aliases.get(fold(fields[0]))
    if canonical is None:
        return f"{first}{more}"
    return ", ".join([canonical, *fields[1:]]) + more


def resolve_location(raw: str) -> ResolvedLocation:
    if not raw or not raw.strip():
        return ResolvedLocation()

    lexicon = location_lexicon()
    segments = [
        _resolve_segment(part, lexicon) for part in _SEGMENT_SPLIT.split(raw) if part.strip()
    ]
    if not segments:
        return ResolvedLocation()

    scope = _scope(segments)

    if any(segment.montreal for segment in segments):
        bucket = LocationBucket.MONTREAL
    elif any(segment.canada for segment in segments):
        bucket = LocationBucket.CANADA
    elif any(segment.usa for segment in segments):
        bucket = LocationBucket.USA
    elif any(segment.international for segment in segments):
        bucket = LocationBucket.INTERNATIONAL
    else:
        bucket = LocationBucket.UNKNOWN

    return ResolvedLocation(bucket=bucket, remote_scope=scope)
