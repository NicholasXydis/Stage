import json
import re
import socket
import unicodedata
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_UNSAFE_IN_URL = re.compile(r"[\x00-\x20\x7f-\x9f]")
WEB_SCHEMES = ("http", "https")


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
    return candidate


def public_https_url(raw: str) -> str | None:
    candidate = web_url(raw)
    if candidate is None:
        return None
    parts = urlsplit(candidate)
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        address = ip_address(host)
    except ValueError:
        try:
            address = ip_address(socket.inet_aton(host))
        except OSError:
            return candidate
    return candidate if address.is_global else None


def first_line(value: str) -> str:
    lines = [line.strip() for line in sanitize(value).splitlines() if line.strip()]
    return lines[0] if lines else ""


def graphemes(value: str) -> list[str]:
    clusters: list[str] = []
    for char in value:
        if unicodedata.combining(char) and clusters:
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def truncate(value: str, width: int) -> str:
    clean = sanitize(value)
    clusters = graphemes(clean)
    if len(clusters) <= width:
        return clean
    return "".join(clusters[: max(width - 1, 0)]) + "…"


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


__all__ = [
    "dump",
    "first_line",
    "graphemes",
    "json_default",
    "json_safe",
    "public_https_url",
    "sanitize",
    "summary",
    "truncate",
    "web_url",
]
