import re
from dataclasses import dataclass
from functools import lru_cache

from stage.domain import LocationBucket, RemoteScope
from stage.lexicon import LocationLexicon, fold, location_lexicon

_SEGMENT_SPLIT = re.compile(r"[;/•|\n]+")
_FIELD_SPLIT = re.compile(r"[,\-]+")


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    bucket: LocationBucket = LocationBucket.UNKNOWN
    remote_scope: RemoteScope | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Segment:
    montreal: bool
    canada: bool
    usa: bool
    other: bool
    remote: bool
    evidence: tuple[str, ...]


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
            "usa_cities": lexicon.usa_cities,
            "usa_regions": lexicon.usa_regions,
            "usa_country": lexicon.usa_country,
            "international": lexicon.international,
            "international_cities": lexicon.international_cities,
            "remote": lexicon.remote,
        }
    )


def _code_hits(segment: str, lexicon: LocationLexicon) -> tuple[bool, bool]:
    canada = usa = False
    for field in _FIELD_SPLIT.split(segment):
        folded = fold(field)
        if folded not in lexicon.canada_codes and folded not in lexicon.usa_codes:
            continue
        if not re.search(rf"\b{folded.upper()}\b", segment):
            continue
        if folded in lexicon.canada_codes:
            canada = True
        else:
            usa = True
    return canada, usa


def _resolve_segment(segment: str, lexicon: LocationLexicon) -> _Segment:
    folded = fold(segment)
    if not folded:
        return _Segment(False, False, False, False, False, ())
    hits = _index().hits(folded)
    found = {category for category, _ in hits}
    code_canada, code_usa = _code_hits(segment, lexicon)

    overridden = "canada_overrides" in found
    canada_context = (
        "canada_country" in found or "canada_regions" in found or code_canada
    ) and not overridden

    montreal = ("montreal" in found and not overridden) or (
        "montreal_ambiguous" in found and canada_context
    )
    canada = (
        montreal
        or canada_context
        or ("canada_cities" in found and not overridden)
        or ("canada_ambiguous" in found and canada_context)
    )
    usa = "usa_country" in found or "usa_regions" in found or "usa_cities" in found or code_usa
    other = "international" in found or "international_cities" in found
    return _Segment(
        montreal=montreal,
        canada=canada,
        usa=usa,
        other=other,
        remote="remote" in found,
        evidence=tuple(sorted(phrase for _, phrase in hits)),
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


def resolve_location(raw: str) -> ResolvedLocation:
    if not raw or not raw.strip():
        return ResolvedLocation()

    lexicon = location_lexicon()
    segments = [
        _resolve_segment(part, lexicon) for part in _SEGMENT_SPLIT.split(raw) if part.strip()
    ]
    if not segments:
        return ResolvedLocation()

    evidence = tuple(sorted({phrase for segment in segments for phrase in segment.evidence}))
    scope = _scope(segments)

    if any(segment.montreal for segment in segments):
        bucket = LocationBucket.MONTREAL
    elif any(segment.canada for segment in segments):
        bucket = LocationBucket.CANADA
    elif any(segment.usa for segment in segments):
        bucket = LocationBucket.USA
    elif any(segment.other for segment in segments):
        bucket = LocationBucket.OTHER
    elif scope is not None:
        bucket = LocationBucket.REMOTE
    else:
        bucket = LocationBucket.UNKNOWN

    return ResolvedLocation(bucket=bucket, remote_scope=scope, evidence=evidence)
