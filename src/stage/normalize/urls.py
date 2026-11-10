import re
from urllib.parse import SplitResult, parse_qsl, urlsplit, urlunsplit

from stage.domain.text import WORKDAY_DOMAINS

ZAPPLY_HOST = "zapply.jobs"

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

CASELESS_PATH_DOMAINS: frozenset[str] = frozenset(WORKDAY_DOMAINS)

_LOCALE_REGION = re.compile(r"^[a-z]{2}[-_][a-zA-Z]{2}$")
_WORKDAY_REQUISITION = re.compile(r"_((?:[a-z]+-?)?\d{5,})(?:-\d{1,2})?$")
_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_IDENTITY_KEYS: frozenset[str] = frozenset(name.replace("_", "") for name in IDENTITY_PARAMS)
_ICIMS_POSTING = re.compile(r"^/jobs/(\d+)(?:/|$)")
_SMARTRECRUITERS_POSTING = re.compile(r"^/([^/]+)/(\d{6,})(?:-[^/]*)?$")
_SMARTRECRUITERS_HOSTS = frozenset({"jobs.smartrecruiters.com", "careers.smartrecruiters.com"})
_ZAPPLY_WORKDAY = re.compile(
    r"/l/d/(?:workday-(?P<tenant>[a-z0-9]+)-.+?|phenom-(?P<site>[a-z0-9]+))"
    r"-?[a-z]*-?(?P<req>[0-9]{4,})[a-z]?(?:-[0-9]{1,2})?/?$"
)
_GREENHOUSE_POSTING = re.compile(r"^/[^/]+/jobs/(\d+)$")
_UUID_POSTING = re.compile(r"^/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27})$")
_WORKDAY_CANONICAL = re.compile(r"^/job/[^/]+/([^/]+)$")
_UUID_HOSTS = {"jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby"}


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
    caseless = any(
        host == domain or host.endswith(f".{domain}") for domain in CASELESS_PATH_DOMAINS
    )
    path = parts.path.lower() if caseless else parts.path
    segments = [segment for segment in path.split("/") if segment]
    last = len(segments) - 1
    kept = [
        segment
        for index, segment in enumerate(segments)
        if index == last or not _is_locale_segment(segment)
    ]
    path = _workday_posting(kept) if caseless else _posting(host, "/" + "/".join(kept))
    return urlunsplit(("https", host, path.rstrip("/") or "/", _identity_query(parts.query), ""))


_APPLY_TAILS = {"jobs.lever.co": "/apply", "jobs.ashbyhq.com": "/application"}


def _posting(host: str, path: str) -> str:
    tail = _APPLY_TAILS.get(host)
    if tail and path.endswith(tail):
        path = path[: -len(tail)]
    if host.endswith(".icims.com") and (found := _ICIMS_POSTING.match(path)):
        return f"/jobs/{found[1]}/job"
    if host in _SMARTRECRUITERS_HOSTS and (found := _SMARTRECRUITERS_POSTING.match(path)):
        return f"/{found[1].lower()}/{found[2]}"
    return path


def _workday_posting(segments: list[str]) -> str:
    if segments and segments[-1] == "apply":
        segments = segments[:-1]
    if len(segments) >= 3 and segments[-3] == "job":
        match = _WORKDAY_REQUISITION.search(segments[-1])
        if match is not None:
            return f"/job/{segments[-2]}/{match.group(1)}"
    return "/" + "/".join(segments)


def zapply_opening(path: str) -> tuple[str, str]:
    found = _ZAPPLY_WORKDAY.search(path.lower())
    return (found["tenant"] or found["site"], found["req"]) if found else ("", "")


def named_posting(url: str) -> tuple[str, str]:
    parts = _split(canonical_apply_url(url))
    if parts is None or not parts.hostname:
        return "", ""
    host, path = parts.hostname, parts.path
    jid = dict(parse_qsl(parts.query)).get("gh_jid", "")
    if jid.isdigit():
        return "greenhouse:*", jid
    greenhouse = host == "greenhouse.io" or host.endswith(".greenhouse.io")
    if greenhouse and (found := _GREENHOUSE_POSTING.match(path)):
        return "greenhouse:*", found[1]
    if host in _UUID_HOSTS and (found := _UUID_POSTING.match(path)):
        return f"{_UUID_HOSTS[host]}:*", found[1]
    if host in _SMARTRECRUITERS_HOSTS and (found := _SMARTRECRUITERS_POSTING.match(path)):
        return "smartrecruiters:*", found[2]
    if any(host.endswith(f".{domain}") for domain in WORKDAY_DOMAINS):
        found = _WORKDAY_CANONICAL.match(path)
        return (f"workday:{host.split('.')[0]}-*", found[1]) if found else ("", "")
    if host == ZAPPLY_HOST:
        tenant, requisition = zapply_opening(path)
        return (f"workday:{tenant}-*", requisition) if tenant else ("", "")
    return "", ""


def names_posting(identifier: str, tail: str) -> bool:
    if identifier.endswith(f":{tail}"):
        return True
    before = identifier[: -len(tail)]
    return tail.isdigit() and identifier.endswith(tail) and not before[-1:].isdigit()
