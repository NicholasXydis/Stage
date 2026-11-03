import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote

from stage.domain import LocationBucket, RemoteScope
from stage.domain.text import sanitize
from stage.lexicon import LocationLexicon, accent_dropped_words, fold, location_lexicon

_SEGMENT_SPLIT = re.compile(r"[;/•|\n]+")
_PLACE_SPLIT = re.compile(r"[;/•|\n]+|\s\+\s")
_FIELD_SPLIT = re.compile(r"[,\-]+")
_LIST_SPLIT = re.compile(r",|\bor\b")
_SUBDIVISION_CODE = re.compile(r"[A-Z]{1,3}")
_DIGIT = re.compile(r"[0-9]")
_CODE_FIELD_SPLIT = re.compile(r"[,()\->]")
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


def _tail_code(segment: str, lexicon: LocationLexicon) -> str:
    fields = segment.split(",")
    index = _last_place_field(fields)
    if index < 1:
        return ""
    tail = fields[index].strip()
    code = fold(tail)
    if not _SUBDIVISION_CODE.fullmatch(tail) or code not in lexicon.country_codes:
        return ""
    return "" if code in _TIME_ZONES else code


def _country_tail(segment: str, lexicon: LocationLexicon) -> str:
    code = _tail_code(segment, lexicon)
    known = lexicon.canada_codes | lexicon.usa_codes
    if not code or code not in known:
        return code
    fields = segment.split(",")
    return code if _foreign_subdivision_before(fields, _last_place_field(fields), known) else ""


_CONJUNCTION = re.compile(r"\bOR\s+(?P<rest>[^\W\d_].*)", re.S)


def _conjunction(field: str, code: str) -> bool:
    match = _CONJUNCTION.search(field)
    if code not in _CONJUNCTIONS or match is None:
        return False
    rest = fold(match["rest"])
    return any(rest == phrase or rest.startswith(f"{phrase} ") for _, phrase in _index().hits(rest))


def _code_hits(
    segment: str, lexicon: LocationLexicon, countries: frozenset[str] = frozenset()
) -> tuple[set[str], set[str]]:
    canada: set[str] = set()
    usa: set[str] = set()
    fields = _FIELD_SPLIT.split(segment)
    known = lexicon.canada_codes | lexicon.usa_codes
    tail = _tail_code(segment, lexicon)
    if tail and (tail not in known or tail in countries):
        return canada, usa
    for index, field in enumerate(fields):
        for token in fold(field).split():
            if token not in known:
                continue
            if not re.search(rf"\b{token.upper()}\b", segment):
                continue
            if _reads_as_country(fields, index, token, known) or _conjunction(field, token):
                continue
            if token in lexicon.canada_codes:
                canada.add(token)
            else:
                usa.add(token)
    return canada, usa


_FOREIGN_ONLY_CODES = frozenset({"de", "es", "ie", "in", "it", "fr", "uk", "nl", "pl", "br", "cn"})
_TIME_ZONES = frozenset({"pt", "et", "est"})
_CONJUNCTIONS = frozenset({"or"})
_WEAK_FOREIGN = frozenset({"global", "worldwide", "anywhere", "work from anywhere"})
_BARE_EITHER = frozenset({"de"})
_BARE_FOREIGN = frozenset(
    {"gb", "sg", "jp", "th", "cn", "hk", "tw", "kr", "mx", "fr", "br", "pl", "cz", "dk", "fi"}
    | {"ro", "hu", "bg", "gr", "tr", "ph", "vn", "za", "ng", "ae", "au", "nz", "pk", "ua", "rs"}
)
_CANADIAN_POSTAL = re.compile(
    r"\b[ABCEGHJ-NPRSTVXY][0-9][ABCEGHJ-NPRSTV-Z] ?[0-9][ABCEGHJ-NPRSTV-Z][0-9]\b"
)


def _trails_a_foreign_code(segment: str) -> bool:
    lexicon = location_lexicon()
    fields = [field.strip() for field in _FIELD_SPLIT.split(segment) if field.strip()]
    if len(fields) < 2:
        return False
    known = lexicon.canada_codes | lexicon.usa_codes | _COUNTRY_PREFIXES
    for field in fields[1:]:
        folded = fold(field)
        if folded in _FOREIGN_ONLY_CODES:
            return True
        if folded and len(folded) <= 3 and folded not in known and not folded.isdigit():
            return True
    return False


_REGION_PREFIX = re.compile(
    r"\s*(?P<country>[A-Z]{2,3})\s*-\s*(?P<region>[A-Z]{1,3})\s*-(?P<rest>.*)", re.S
)
_COUNTRY_PREFIX = re.compile(r"\s*(?P<country>[A-Z]{3})\s*[->,](?P<rest>.*)", re.S)
_SITE_PREFIX = re.compile(r"\s*(?P<country>[A-Z]{2})[A-Z2-9]{3}[0-9]{2}\s*-(?P<rest>.*)", re.S)
_PAIR_PREFIX = re.compile(
    r"\s*(?P<country>[A-Z]{2})(?:\s+-\s*|\s*,\s*|-(?=[^-]+-|[^\W\d_][^-]*$))(?P<rest>.*)", re.S
)
_NOT_A_COUNTRY_PREFIX = frozenset({"pt"})
_OVERSEAS = re.compile(r"\s*OC(?:ONUS)?\s*-", re.IGNORECASE)
_DOMESTIC = frozenset(
    {
        "montreal",
        "canada_cities",
        "canada_regions",
        "canada_country",
        "usa_cities",
        "usa_regions",
        "usa_country",
    }
)
_FOREIGN = frozenset({"international", "international_cities"})


def _pair_prefix(segment: str, lexicon: LocationLexicon) -> re.Match[str] | None:
    match = _PAIR_PREFIX.match(segment)
    codes = lexicon.canada_codes | lexicon.usa_codes | _NOT_A_COUNTRY_PREFIX
    if match is None or fold(match["country"]) in codes:
        return None
    fields = (field.strip() for field in _FIELD_SPLIT.split(match["rest"]))
    if any(field.isupper() and fold(field) in codes for field in fields):
        return None
    return match


def _site_verdict(match: re.Match[str], lexicon: LocationLexicon) -> _Segment | None:
    country = fold(match["country"])
    remote = "remote" in {category for category, _ in _index().hits(fold(match["rest"]))}
    if country == "us":
        return _Segment(False, False, True, False, remote)
    if country == "ca":
        return _Segment(False, True, False, False, remote)
    if country in lexicon.country_codes:
        return _Segment(False, False, False, True, remote)
    return None


def _foreign_prefix(segment: str, lexicon: LocationLexicon) -> _Segment | None:
    overseas = _OVERSEAS.match(segment)
    if overseas is not None:
        segment = segment[overseas.end() :]
    match = (
        _REGION_PREFIX.match(segment)
        or _COUNTRY_PREFIX.match(segment)
        or _SITE_PREFIX.match(segment)
        or _pair_prefix(segment, lexicon)
    )
    if match is not None and match.re is _SITE_PREFIX:
        return _site_verdict(match, lexicon)
    if match is None or fold(match["country"]) not in lexicon.country_codes:
        return None
    place, *others = _LIST_SPLIT.split(match["rest"], maxsplit=1)
    if others:
        later = _resolve_segment(others[0], lexicon)
        if later.montreal or later.canada or later.usa:
            return None
    return _country_verdict(match, place, lexicon)


def _country_verdict(match: re.Match[str], place: str, lexicon: LocationLexicon) -> _Segment | None:
    found = {category for category, _ in _index().hits(fold(place))}
    domestic, foreign = found & _DOMESTIC, found & _FOREIGN
    codes = lexicon.canada_codes | lexicon.usa_codes
    region = fold(match.groupdict().get("region") or "")
    remote = "remote" in found
    if fold(match["country"]) in codes:
        if domestic or region in codes or len(region) > 2:
            return None
        return _Segment(False, False, False, bool(foreign or region), remote)
    if domestic and (region in codes if region else not foreign):
        return None
    return _Segment(False, False, False, True, remote)


def _last_place_field(fields: list[str]) -> int:
    for index in reversed(range(len(fields))):
        if not _DIGIT.search(fields[index]) and any(char.isalpha() for char in fields[index]):
            return index
    return -1


def _trailing_country(segment: str, lexicon: LocationLexicon) -> bool:
    fields = _FIELD_SPLIT.split(segment)
    index = _last_place_field(fields)
    if index < 0:
        return False
    words = fields[index].split()
    if not words:
        return False
    word = words[-1].strip(".()")
    code = fold(word)
    if not (2 <= len(code) <= 3 and code.isalpha()) or code not in lexicon.country_codes:
        return False
    known = lexicon.canada_codes | lexicon.usa_codes
    if code in known:
        commas = segment.split(",")
        last = _last_place_field(commas)
        if last < 0 or not _reads_as_country(commas[: last + 1], last, code, known):
            return False
    alone = len(words) == 1 and index > 0
    return word.isupper() if alone else len(code) == 3 and word in (code.upper(), code.title())


_GROUPS = {
    "montreal": "montreal",
    "montreal_ambiguous": "montreal",
    "canada_cities": "canada",
    "canada_ambiguous": "canada",
    "canada_regions": "canada",
    "canada_country": "canada",
    "usa_ambiguous": "usa",
    "usa_cities": "usa",
    "usa_regions": "usa",
    "usa_country": "usa",
    "international": "international",
    "international_cities": "international",
}


def _outermost(hits: set[tuple[str, str]]) -> set[tuple[str, str]]:
    kept: set[tuple[str, str]] = set()
    for category, phrase in hits:
        group = _GROUPS.get(category)
        shadowed = group is not None and any(
            _GROUPS.get(other) not in (None, group)
            and len(longer) > len(phrase)
            and f" {phrase} " in f" {longer} "
            for other, longer in hits
        )
        if not shadowed:
            kept.add((category, phrase))
    return kept


def _names_canada(segment: str) -> bool:
    return any(field.strip() == "CAN" for field in _CODE_FIELD_SPLIT.split(segment))


def _canadian_address(segment: str, lexicon: LocationLexicon) -> bool:
    fields = [field.strip() for field in segment.split(",")]
    fields = fields[: _last_place_field(fields) + 1]
    if len(fields) < 3 or fields[-1] != "CA":
        return False
    return fields[-2].isupper() and fold(fields[-2]) in lexicon.canada_codes


def _bare_code(segment: str) -> _Segment | None:
    remote = {phrase for category, phrase in _index().hits(fold(segment)) if category == "remote"}
    words = [word for word in re.split(r"[\W_]+", segment) if word and fold(word) not in remote]
    if len(words) != 1 or not words[0].isupper():
        return None
    code = fold(words[0])
    if code not in _BARE_EITHER | _BARE_FOREIGN:
        return None
    return _Segment(False, False, False, code in _BARE_FOREIGN, bool(remote))


def _without_weak_foreign(hits: set[tuple[str, str]], coded: bool) -> set[tuple[str, str]]:
    foreign = {phrase for category, phrase in hits if category in _FOREIGN}
    domestic = coded or any(category in _DOMESTIC for category, _ in hits)
    if not foreign or not foreign <= _WEAK_FOREIGN or not domestic:
        return hits
    return {(category, phrase) for category, phrase in hits if category not in _FOREIGN}


def _overrides(hits: set[tuple[str, str]], folded: str, lexicon: LocationLexicon) -> set[str]:
    return {
        phrase
        for category, phrase in hits
        if category == "canada_overrides"
        and (phrase.split(" ", 1)[0] not in lexicon.canada_regions or folded.startswith(phrase))
    }


def _state_named(segment: str, hits: set[tuple[str, str]]) -> bool:
    states = {phrase for category, phrase in hits if category == "usa_regions"} - {"georgia"}
    for field in _FIELD_SPLIT.split(segment):
        words = fold(field)
        if any(words == state or words.endswith(f" {state}") for state in states):
            return True
    return False


def _resolve_segment(
    segment: str, lexicon: LocationLexicon, countries: frozenset[str] = frozenset()
) -> _Segment:
    folded = fold(segment)
    if not folded:
        return _Segment(False, False, False, False, False)
    foreign = _foreign_prefix(segment, lexicon) or _bare_code(segment)
    if foreign is not None:
        return foreign
    canada_codes, usa_codes = _code_hits(segment, lexicon, countries)
    hits = _without_weak_foreign(_outermost(_index().hits(folded)), bool(canada_codes or usa_codes))
    found = {category for category, _ in hits}
    international = bool(found & _FOREIGN)

    override_phrases = _overrides(hits, folded, lexicon)
    named_canada = {
        phrase
        for category, phrase in hits
        if category in {"canada_cities", "canada_ambiguous", "montreal", "montreal_ambiguous"}
    }
    overridden = bool(override_phrases) and all(
        any(f" {phrase} " in f" {override} " for override in override_phrases)
        for phrase in named_canada
    )
    us_coded = bool(usa_codes - {"ca"})
    canadian_names = bool(named_canada) or bool(found & {"canada_regions", "canada_country"})
    code_canada = bool(canada_codes) and (
        canadian_names or not (us_coded and "usa_cities" in found)
    )
    code_usa = bool(usa_codes)
    ambiguous_here = bool(found & {"canada_ambiguous", "montreal_ambiguous", "usa_ambiguous"})
    us_region = "usa_regions" in found
    coded = (
        (code_canada or code_usa or us_region)
        and ambiguous_here
        and not _trails_a_foreign_code(segment)
        and not found & {"international"}
    ) or _canadian_address(segment, lexicon)
    canada_context = (
        "canada_country" in found
        or _names_canada(segment)
        or "canada_regions" in found
        or _CANADIAN_POSTAL.search(segment) is not None
        or (code_canada and (not international or coded))
    ) and not overridden

    elsewhere = bool(canada_codes - {"qc"}) or any(
        category == "canada_regions" and phrase != "quebec" for category, phrase in hits
    )
    montreal = ("montreal" in found and not overridden) or (
        "montreal_ambiguous" in found and canada_context and not elsewhere
    )
    us_state = us_coded or _state_named(segment, hits)
    canada = (
        montreal
        or canada_context
        or ("canada_cities" in found and not overridden and not international and not us_state)
        or ("canada_ambiguous" in found and canada_context)
    )
    usa = "usa_country" in found or (
        ("usa_regions" in found or "usa_cities" in found or code_usa)
        and (not international or ((code_usa or us_region) and coded))
    )
    if not (montreal or canada or usa or international):
        international = _trailing_country(segment, lexicon)
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
_MORE = re.compile(r"(?P<count>\d+) more")


def display_location(raw: str) -> str:
    if not raw or not raw.strip():
        return ""

    aliases = location_lexicon().city_aliases
    segments = _top_level_segments(raw)
    first = _MULTI_SITE_SUFFIX.sub("", segments[0].strip())
    if not first:
        return raw.strip()

    others = sum(
        int(counted["count"]) if (counted := _MORE.fullmatch(part.strip())) else 1
        for part in segments[1:]
        if part.strip()
    )
    more = f" +{others}" if others else ""

    fields = [field.strip() for field in first.split(",") if field.strip()]
    if not fields:
        return raw.strip()

    canonical = aliases.get(fold(fields[0]))
    if canonical is None:
        return f"{first}{more}"
    return ", ".join([canonical, *fields[1:]]) + more


def _top_level_segments(raw: str) -> list[str]:
    segments = [""]
    depth = 0
    for char in raw:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and _SEGMENT_SPLIT.fullmatch(char):
            segments.append("")
        else:
            segments[-1] += char
    return segments


@lru_cache(maxsize=1)
def _city_names() -> frozenset[str]:
    lexicon = location_lexicon()
    return (
        lexicon.usa_cities | lexicon.canada_cities | lexicon.montreal | lexicon.international_cities
    )


_TRAILING_CODE = re.compile(r"\s+[A-Z]{2}$")


def _city_key(field: str, cities: frozenset[str]) -> str:
    for candidate in (field, _TRAILING_CODE.sub("", field)):
        if fold(candidate) in cities:
            return fold(candidate)
    return ""


def _city_list(segment: str, lexicon: LocationLexicon, countries: frozenset[str]) -> list[_Segment]:
    fields = [field.strip() for field in _LIST_SPLIT.split(segment) if field.strip()]
    cities = _city_names()
    named: dict[str, str] = {}
    for field in fields:
        key = _city_key(field, cities)
        if key:
            named.setdefault(key, field)
    if len(named) < 2:
        return []
    return [_resolve_segment(field, lexicon, countries) for field in named.values()]


def _namesake(part: str, lexicon: LocationLexicon) -> bool:
    code = fold(part)
    if code == "georgia":
        return True
    known = lexicon.canada_codes | lexicon.usa_codes
    return part.strip().isupper() and code in known and code in lexicon.country_codes


def _foreign_namesakes(
    parts: list[str], segments: list[_Segment], lexicon: LocationLexicon
) -> list[_Segment]:
    named = [_namesake(part, lexicon) for part in parts]
    others = [segment for segment, flag in zip(segments, named, strict=True) if not flag]
    if len(others) == len(segments) or not any(segment.international for segment in others):
        return segments
    if any(segment.montreal or segment.canada or segment.usa for segment in others):
        return segments
    return [
        _Segment(False, False, False, True, segment.remote) if flag else segment
        for segment, flag in zip(segments, named, strict=True)
    ]


@lru_cache(maxsize=65_536)
def resolve_location(raw: str) -> ResolvedLocation:
    if not raw or not raw.strip():
        return ResolvedLocation()

    lexicon = location_lexicon()
    parts = [part for part in _PLACE_SPLIT.split(raw) if part.strip()]
    countries = frozenset(code for part in parts if (code := _country_tail(part, lexicon)))
    segments = _foreign_namesakes(
        parts, [_resolve_segment(part, lexicon, countries) for part in parts], lexicon
    )
    if not segments:
        return ResolvedLocation()
    for part in parts:
        if _foreign_prefix(part, lexicon) is None:
            segments.extend(_city_list(part, lexicon, countries))

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


_ABROAD_TITLE = re.compile(
    r"\(\s*(?:[mwfdx]|div)(?:\s*/\s*(?:[mwfdx]|div)){2,3}\s*\)"
    r"|\b(?:EMEA|APAC|JAPAC|LATAM|DACH|Europe|Asia|Australia|Africa)\b"
    r"|\b[FH]\s*/\s*[HF]\b"
    r"|\b(?:Praktikant|Werkstudent|Abschlussarbeit|Pflichtpraktikum)"
    r"|\bBAC\s*\+\s*[0-9]"
    r"|\bstage\s+[0-9]+(?:\s*(?:à|a)\s*[0-9]+)?\s+mois\b"
    r"|[\u3040-\u30ff\uac00-\ud7af]",
    re.IGNORECASE,
)
_TITLE_PARENTHESIS = re.compile(r"\((?P<place>[^()]*)\)\s*$")


_TITLE_TAIL = re.compile(r"\s[-\u2013]\s(?P<place>[^-\u2013()|]+?)\s*$")


def _titled_abroad(title: str) -> bool:
    if _ABROAD_TITLE.search(title):
        return True
    titled = _titled_place(title)
    if titled and resolve_location(titled).bucket is LocationBucket.INTERNATIONAL:
        return True
    hinted = _TITLE_PARENTHESIS.search(title)
    if hinted is not None and _abroad_hint(hinted["place"]):
        return True
    tail = _TITLE_TAIL.search(title)
    return tail is not None and _names_only_places(tail["place"]) and _abroad_hint(tail["place"])


def _abroad_hint(place: str) -> bool:
    found = {category for category, _ in _index().hits(fold(place))}
    if found & {"canada_ambiguous", "montreal_ambiguous", "usa_ambiguous"}:
        return False
    return resolve_location(place).bucket is LocationBucket.INTERNATIONAL


def _names_only_places(place: str) -> bool:
    words = set(fold(place).split())
    named = {word for _, phrase in _index().hits(fold(place)) for word in phrase.split()}
    return bool(words) and words <= named


def resolve_listing(place: str, title: str = "") -> ResolvedLocation:
    resolved = resolve_location(place)
    if resolved.bucket is not LocationBucket.UNKNOWN or not _titled_abroad(title or ""):
        return resolved
    return ResolvedLocation(LocationBucket.INTERNATIONAL, resolved.remote_scope)


_MANY_PLACES = re.compile(r"\s*(?P<count>[0-9]{1,3})\s+Locations\s*", re.IGNORECASE)
_WORKDAY_PLACE = re.compile(
    r"https?://[^/]+\.myworkday(?:jobs|site)\.com/(?:[^/?#]+/)*?job/(?P<place>[^/?#]+)/[^/?#]+",
    re.IGNORECASE,
)
_PREFERRED = frozenset({LocationBucket.MONTREAL, LocationBucket.CANADA, LocationBucket.USA})
_TITLED_PLACE = re.compile(
    r"\s*(?:19|20)[0-9]{2}\s*\|\s*(?P<region>[^|]+?)\s*\|\s*(?P<place>[^|]+?)\s*\|"
)


def _titled_place(title: str) -> str:
    match = _TITLED_PLACE.match(title or "")
    if match is None:
        return ""
    for place in (match["place"], f"{match['place']}, {match['region']}"):
        if resolve_location(place).bucket is not LocationBucket.UNKNOWN:
            return place
    return ""


def listed_place(location_raw: str, apply_url: str, title: str = "") -> str:
    place = _linked_place(location_raw, apply_url)
    if place.strip():
        return place
    return _titled_place(title) or place


def _linked_place(location_raw: str, apply_url: str) -> str:
    many = _MANY_PLACES.fullmatch(location_raw or "")
    link = _WORKDAY_PLACE.match(apply_url or "")
    blank = not (location_raw or "").strip()
    if link is None or (not blank and (many is None or int(many["count"]) < 2)):
        return location_raw
    known = accent_dropped_words()
    place = "-".join(
        known.get(word.lower(), word)
        for word in " ".join(sanitize(unquote(link["place"])).split()).split("-")
    )
    bucket = resolve_location(place).bucket
    wanted = bucket is not LocationBucket.UNKNOWN if blank else bucket in _PREFERRED
    if not wanted or chr(0xFFFD) in place:
        return location_raw
    head, _, rest = place.partition("-")
    if (
        fold(head) in location_lexicon().country_codes
        and resolve_location(rest).bucket is not bucket
    ):
        return location_raw
    tidy = " - ".join(" ".join(part.replace("-", " ").split()) for part in place.split("---"))
    shown = tidy if resolve_location(tidy).bucket is bucket else place
    if many is None:
        return shown
    return f"{shown} / {int(many['count']) - 1} more"
