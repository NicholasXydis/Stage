import json
import re
import socket
import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from ipaddress import ip_address
from urllib.parse import urlsplit

_CONTROL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_UNSAFE_IN_URL = re.compile(r"[\x00-\x20\x7f-\x9f]")
_BLANK_RUN = re.compile(r"\n{3,}")
_BREAKS = re.compile(r"[\t\n\r\u2028\u2029]+")
_INVISIBLE = re.compile(r"[\u180e\u200b\u2060\ufeff]")
_JOINER = "\u200d"
_EMOJI_STYLE = "\ufe0f"
_FLAGS = range(0x1F1E6, 0x1F200)
_SHOWN = (
    range(0xAD, 0xAE),
    range(0x600, 0x606),
    range(0x6DD, 0x6DE),
    range(0x70F, 0x710),
    range(0x890, 0x892),
    range(0x8E2, 0x8E3),
    range(0x110BD, 0x110BE),
    range(0x110CD, 0x110CE),
)
_SILENT = (
    range(0x1160, 0x1200),
    range(0x2028, 0x202A),
    range(0x3164, 0x3165),
    range(0xD7B0, 0xD800),
    range(0xFFA0, 0xFFA1),
    range(0x1F3FB, 0x1F400),
)
_WIDE = (
    range(0x2630, 0x2638),
    range(0x268A, 0x2690),
    range(0x4DC0, 0x4E00),
    range(0x1D300, 0x1D357),
    range(0x1D360, 0x1D377),
)
WEB_SCHEMES = ("http", "https")
MAX_OPEN_AT_ONCE = 50


def sanitize(value: str) -> str:
    return _CONTROL.sub("", value)


def web_url(raw: str) -> str | None:
    candidate = raw.strip()
    if not candidate or _UNSAFE_IN_URL.search(candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in WEB_SCHEMES or not parts.netloc:
        return None
    if parts.username or parts.password or not _public_host(parts.hostname or ""):
        return None
    return candidate


def _public_host(raw: str) -> bool:
    host = raw.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ip_address(host)
    except ValueError:
        try:
            address = ip_address(socket.inet_aton(host))
        except OSError:
            return True
    return address.is_global


def public_https_url(raw: str) -> str | None:
    candidate = web_url(raw)
    if candidate is None or urlsplit(candidate).scheme != "https":
        return None
    return candidate


_ERROR_NAME = re.compile(r"\b([A-Z][A-Za-z]+(?:Error|Exception|Timeout))\b(:\s*)?")
_ERROR_WORDS = {
    "CancelledError": "the request was cancelled",
    "ConnectError": "could not connect",
    "ConnectTimeout": "the connection timed out",
    "ConnectionResetError": "the connection was reset",
    "PoolTimeout": "too many requests were waiting",
    "ReadError": "the connection dropped",
    "ReadTimeout": "the answer timed out",
    "RemoteProtocolError": "the site broke off its answer",
    "TimeoutError": "the request timed out",
    "WriteTimeout": "the request timed out",
}


def plain_error(message: str) -> str:
    def replace(match: re.Match[str]) -> str:
        said = _ERROR_WORDS.get(match.group(1), "")
        return f"{said}: " if said and match.group(2) else said

    cleaned = _ERROR_NAME.sub(replace, message).strip().rstrip(":").strip()
    return cleaned or "Stage could not tell what went wrong"


WORKDAY_DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com")


def posted_day(moment: datetime) -> date:
    stated = moment.astimezone(UTC)
    return stated.date() if stated.time() == time() else moment.astimezone().date()


def is_workday_opening(canonical: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(canonical)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    workday = any(host.endswith(f".{domain}") for domain in WORKDAY_DOMAINS)
    return workday and parts.path.startswith("/job/") and parts.path.count("/") == 3


def did_you_mean(near: str) -> str:
    return f'Did you mean "{near}"?'


def sentence(message: str) -> str:
    text = message.strip()
    head = text.split(" ", 1)[0]
    if head.isalpha() and head.islower() and head != "stage":
        text = text[:1].upper() + text[1:]
    return text if not text or text.endswith((".", "?", "!", ":")) else f"{text}."


def unknown_choice(label: str, value: str, choices: Sequence[str], cutoff: float = 0.6) -> str:
    from difflib import get_close_matches

    options = list(choices)
    if not value.strip():
        return f"Choose a {label} from: {', '.join(options)}."
    near = get_close_matches(value.strip().lower(), options, n=1, cutoff=cutoff)
    hint = did_you_mean(near[0]) if near else f"Choose from: {', '.join(options)}."
    return f'Unknown {label} "{truncate(value, 60)}". {hint}'


def counted(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def first_line(value: str) -> str:
    lines = [line.strip() for line in sanitize(value).splitlines() if line.strip()]
    return lines[0] if lines else ""


def paragraphs(value: str) -> str:
    lines = [line.strip() for line in sanitize(value).splitlines()]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def one_line(value: str) -> str:
    return _BREAKS.sub(" ", _INVISIBLE.sub("", sanitize(value)))


def _char_cells(char: str) -> int:
    point = ord(char)
    if point < 0xA0:
        return 1 if 0x20 <= point < 0x7F else 0
    if _within(point, _SHOWN):
        return 1
    if _within(point, _SILENT) or unicodedata.category(char) in ("Mn", "Me", "Mc", "Cf"):
        return 0
    if _within(point, _WIDE) or unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def _within(point: int, spans: tuple[range, ...]) -> bool:
    return any(point in span for span in spans)


def _joins(cluster: str, char: str) -> bool:
    if cluster.endswith(_JOINER) or not _char_cells(char):
        return True
    return ord(char) in _FLAGS and len(cluster) == 1 and ord(cluster) in _FLAGS


def graphemes(value: str) -> list[str]:
    clusters: list[str] = []
    for char in value:
        if clusters and _joins(clusters[-1], char):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def cells(value: str) -> int:
    return sum(_cluster_cells(cluster) for cluster in graphemes(value))


def _cluster_cells(cluster: str) -> int:
    total = 0
    last = ""
    skip = False
    for char in cluster:
        if skip:
            skip = False
        elif char == _JOINER:
            skip = True
        elif char == _EMOJI_STYLE:
            if last and (last in "#*0123456789" or ord(last) >= 0xA9):
                total += 1
            last = ""
        elif size := _char_cells(char):
            total += size
            last = char if size == 1 else ""
    return total


def truncate(value: str, width: int) -> str:
    clean = one_line(value)
    if clean.isascii():
        return clean if len(clean) <= width else clean[: max(width - 3, 0)] + "..."
    clusters = graphemes(clean)
    sizes = [_cluster_cells(cluster) for cluster in clusters]
    if sum(sizes) <= width:
        return clean
    room = max(width - 3, 0)
    kept: list[str] = []
    for cluster, size in zip(clusters, sizes, strict=True):
        if size > room:
            break
        kept.append(cluster)
        room -= size
    return "".join(kept) + "..."


def summary(value: str, width: int) -> str:
    return truncate(first_line(value), width)


def json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"unserializable value of type {type(value).__name__}")


def json_safe(value: object) -> object:
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return value


def dump(payload: object) -> str:
    return json.dumps(json_safe(payload), indent=2, ensure_ascii=False, default=json_default)


_BOARD_LABEL = re.compile(
    r"\s*\((?:workday|eightfold|phenom|ashby|campus|corporate|university|students?|interns?"
    r"|internships|early careers?|early talent|fr|french)\)\s*$",
    re.IGNORECASE,
)


_LOCATION_COUNT = re.compile(r"\s*[0-9]+ Locations\s*")
LOCATION_COUNT_GLOB = "[0-9]* Locations"


def counts_locations(place: str) -> bool:
    return _LOCATION_COUNT.fullmatch(place) is not None


def plain_company(name: str) -> str:
    return _BOARD_LABEL.sub("", name) or name


def escape_markup(value: str) -> str:
    return sanitize(value).replace("\\", "\\\\").replace("[", "\\[")


__all__ = [
    "LOCATION_COUNT_GLOB",
    "MAX_OPEN_AT_ONCE",
    "WEB_SCHEMES",
    "cells",
    "counted",
    "counts_locations",
    "did_you_mean",
    "dump",
    "escape_markup",
    "first_line",
    "graphemes",
    "is_workday_opening",
    "json_default",
    "json_safe",
    "one_line",
    "paragraphs",
    "plain_company",
    "plain_error",
    "public_https_url",
    "sanitize",
    "sentence",
    "summary",
    "truncate",
    "unknown_choice",
    "web_url",
]
