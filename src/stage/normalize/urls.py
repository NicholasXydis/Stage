import re
from urllib.parse import SplitResult, parse_qsl, urlsplit, urlunsplit

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

LOCALE_LANGUAGES: frozenset[str] = frozenset(
    {
        "ar",
        "cs",
        "da",
        "de",
        "el",
        "en",
        "es",
        "fi",
        "fr",
        "he",
        "hu",
        "ja",
        "ko",
        "nl",
        "pl",
        "pt",
        "ro",
        "ru",
        "sv",
        "th",
        "tr",
        "vi",
        "zh",
    }
)

IDENTITY_PARAMS: frozenset[str] = frozenset(
    {
        "gh_jid",
        "id",
        "jid",
        "job",
        "jobid",
        "job_id",
        "jobnumber",
        "jobreqid",
        "opportunityid",
        "pid",
        "positionid",
        "postingid",
        "req",
        "reqid",
        "req_id",
        "requisitionid",
        "rid",
        "vacancyid",
    }
)

_LOCALE_REGION = re.compile(r"^[a-z]{2}[-_][a-zA-Z]{2}$")
_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_IDENTITY_KEYS: frozenset[str] = frozenset(name.replace("_", "") for name in IDENTITY_PARAMS)


def _is_locale_segment(segment: str) -> bool:
    return bool(_LOCALE_REGION.match(segment)) or segment.lower() in LOCALE_LANGUAGES


def _split(url: str) -> SplitResult | None:
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _host(url: str) -> str:
    parts = _split(url)
    return "" if parts is None else (parts.hostname or "").lower()


def is_tracker_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in TRACKER_DOMAINS)


def _identity_query(query: str) -> str:
    if not query:
        return ""
    kept: list[str] = []
    for name, value in parse_qsl(query, keep_blank_values=False):
        if name.lower().replace("-", "_").replace("_", "") not in _IDENTITY_KEYS:
            continue
        if _IDENTITY_VALUE.match(value):
            kept.append(f"{name.lower()}={value}")
    return "&".join(sorted(kept))


def canonical_apply_url(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    parts = _split(raw.strip())
    if parts is None or parts.scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    if not host or is_tracker_url(raw):
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    kept = [segment for segment in segments if not _is_locale_segment(segment)]
    path = "/" + "/".join(kept)
    return urlunsplit(("https", host, path.rstrip("/") or "/", _identity_query(parts.query), ""))
