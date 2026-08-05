
import re
from urllib.parse import urlsplit, urlunsplit

TRACKER_DOMAINS: frozenset[str] = frozenset(
    {
        "simplify.jobs",
        "click.appcast.io",
        "trk.simplify.jobs",
        "jobright.ai",
        "app.otta.com",
        "get.hiring.cafe",
        "grnh.se",
        "boards.greenhouse.io.simplify.jobs",
        "l.workwithus.io",
        "track.rippling-ats.com",
    }
)

_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}([-_][a-zA-Z]{2})?$")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_tracker_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in TRACKER_DOMAINS)


def canonical_apply_url(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    if not host or is_tracker_url(raw):
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    kept = [segment for segment in segments if not _LOCALE_SEGMENT.match(segment)]
    path = "/" + "/".join(kept)
    return urlunsplit(("https", host, path.rstrip("/") or "/", "", ""))
