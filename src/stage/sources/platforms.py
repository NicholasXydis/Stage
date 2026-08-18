import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from stage.domain import Platform, PlatformCandidate
from stage.lexicon import fold

SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SAFE_PATH_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
_LOCALE = r"[a-z]{2}(?:-[A-Za-z]{2})?"
_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$")


class SlugRejectedError(ValueError):
    pass


def safe_slug(slug: str) -> str:
    lowered = slug.strip().lower()
    if not SAFE_SLUG.match(lowered):
        raise SlugRejectedError(
            f"{slug!r} is not a usable board token — it interpolates into a "
            "hostname, so lowercase letters, digits and hyphens only"
        )
    return lowered


def safe_path_slug(slug: str) -> str:
    lowered = slug.strip().lower()
    if not SAFE_PATH_SLUG.match(lowered):
        raise SlugRejectedError(
            f"{slug!r} is not a usable path token — lowercase letters, digits, dots and "
            "hyphens only"
        )
    return lowered


SAFE_WORKDAY_DC = re.compile(r"^wd\d{1,3}$")
SAFE_WORKDAY_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_ORACLE_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+oraclecloud\.com$")
SAFE_ORACLE_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def workday_target(tenant: str, site: str, dc: str) -> tuple[str, str]:
    missing = [
        field
        for field, value in (
            ("workday_tenant", tenant),
            ("workday_site", site),
            ("workday_dc", dc),
        )
        if not value.strip()
    ]
    if missing:
        raise SlugRejectedError(
            f"registry row is missing {', '.join(missing)} — a Workday tenant, site "
            "and datacenter cannot be guessed. Use `stage discover --url`"
        )

    safe_tenant = safe_slug(tenant)
    stripped_dc = dc.strip().lower()
    if not SAFE_WORKDAY_DC.match(stripped_dc):
        raise SlugRejectedError(
            f"{dc!r} is not a Workday datacenter — expected wd followed by digits "
            "(it interpolates into the hostname)"
        )
    stripped_site = site.strip()
    if not SAFE_WORKDAY_SITE.match(stripped_site):
        raise SlugRejectedError(
            f"{site!r} is not a usable Workday site — it interpolates into the "
            "request path, so letters, digits, underscores and hyphens only"
        )
    host = f"{safe_tenant}.{stripped_dc}.myworkdayjobs.com"
    return host, f"/wday/cxs/{safe_tenant}/{stripped_site}/jobs"


def oracle_target(host: str, site: str) -> tuple[str, str]:
    missing = [
        field
        for field, value in (("oracle_host", host), ("oracle_site", site))
        if not value.strip()
    ]
    if missing:
        raise SlugRejectedError(
            f"registry row is missing {', '.join(missing)} — an Oracle candidate site "
            "cannot be guessed. Use `stage discover --url`"
        )
    safe_host = host.strip().lower()
    if not SAFE_ORACLE_HOST.match(safe_host):
        raise SlugRejectedError(
            f"{host!r} is not a usable Oracle Cloud host — expected an oraclecloud.com "
            "host because it interpolates into the request URL"
        )
    safe_site = site.strip()
    if not SAFE_ORACLE_SITE.match(safe_site):
        raise SlugRejectedError(
            f"{site!r} is not a usable Oracle candidate site — it interpolates into the "
            "request path, so letters, digits, underscores and hyphens only"
        )
    return safe_host, safe_site


@dataclass(frozen=True, slots=True)
class PlatformProbe:
    platform: Platform
    host: str
    probe_url: str
    rate_profile: str
    jobs_paths: tuple[str, ...]
    name_paths: tuple[str, ...] = ()
    count_paths: tuple[str, ...] = ()
    params: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    verify_url: str | None = None
    verify_name_paths: tuple[str, ...] = ()
    slug_validator: Callable[[str], str] = safe_slug

    def host_for(self, slug: str) -> str:
        return self.host.format(slug=self.slug_validator(slug))

    def url_for(self, slug: str) -> str:
        return self.probe_url.format(slug=self.slug_validator(slug))

    def verify_url_for(self, slug: str) -> str | None:
        if self.verify_url is None:
            return None
        return self.verify_url.format(slug=self.slug_validator(slug))


PROBES: tuple[PlatformProbe, ...] = (
    PlatformProbe(
        platform=Platform.GREENHOUSE,
        host="boards-api.greenhouse.io",
        probe_url="https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        rate_profile="standard",
        jobs_paths=("jobs",),
        count_paths=("meta.total",),
        verify_url="https://boards-api.greenhouse.io/v1/boards/{slug}",
        verify_name_paths=("name",),
    ),
    PlatformProbe(
        platform=Platform.LEVER,
        host="api.lever.co",
        probe_url="https://api.lever.co/v0/postings/{slug}",
        rate_profile="standard",
        jobs_paths=("",),
        params=MappingProxyType({"mode": "json"}),
    ),
    PlatformProbe(
        platform=Platform.ASHBY,
        host="api.ashbyhq.com",
        probe_url="https://api.ashbyhq.com/posting-api/job-board/{slug}",
        rate_profile="standard",
        jobs_paths=("jobs",),
        name_paths=("organizationName", "jobs.0.organizationName"),
        slug_validator=safe_path_slug,
    ),
    PlatformProbe(
        platform=Platform.SMARTRECRUITERS,
        host="api.smartrecruiters.com",
        probe_url="https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        rate_profile="moderate",
        jobs_paths=("content",),
        count_paths=("totalFound",),
        name_paths=("content.0.company.name",),
    ),
    PlatformProbe(
        platform=Platform.BAMBOOHR,
        host="{slug}.bamboohr.com",
        probe_url="https://{slug}.bamboohr.com/careers/list",
        rate_profile="moderate",
        jobs_paths=("result",),
        name_paths=("meta.companyName", "companyName"),
    ),
    PlatformProbe(
        platform=Platform.RECRUITEE,
        host="{slug}.recruitee.com",
        probe_url="https://{slug}.recruitee.com/api/offers/",
        rate_profile="moderate",
        jobs_paths=("offers",),
        name_paths=("offers.0.company_name",),
    ),
    PlatformProbe(
        platform=Platform.BREEZY,
        host="{slug}.breezy.hr",
        probe_url="https://{slug}.breezy.hr/json",
        rate_profile="moderate",
        jobs_paths=("",),
    ),
    PlatformProbe(
        platform=Platform.WORKABLE,
        host="apply.workable.com",
        probe_url="https://apply.workable.com/api/v1/widget/accounts/{slug}",
        rate_profile="conservative",
        jobs_paths=("jobs",),
        name_paths=("name",),
        params=MappingProxyType({"details": "true"}),
    ),
    PlatformProbe(
        platform=Platform.COLLAGE,
        host="api.collage.co",
        probe_url="https://api.collage.co/v1/positions/{slug}",
        rate_profile="moderate",
        jobs_paths=("positions",),
    ),
)

PROBES_BY_PLATFORM: MappingProxyType[Platform, PlatformProbe] = MappingProxyType(
    {probe.platform: probe for probe in PROBES}
)

URL_ONLY_PLATFORMS: frozenset[Platform] = frozenset(
    {
        Platform.WORKDAY,
        Platform.PERSONIO,
        Platform.TEAMTAILOR,
        Platform.JOBVITE,
        Platform.JOIN,
        Platform.EIGHTFOLD,
    }
)


@dataclass(frozen=True, slots=True)
class UrlPattern:
    platform: Platform
    hosts: tuple[str, ...] = ()
    host_pattern: re.Pattern[str] | None = None
    path_pattern: re.Pattern[str] | None = None
    slug_validator: Callable[[str], str] = safe_slug


def _path(expr: str) -> re.Pattern[str]:
    return re.compile(expr)


URL_PATTERNS: tuple[UrlPattern, ...] = (
    UrlPattern(
        platform=Platform.GREENHOUSE,
        hosts=(
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "boards.eu.greenhouse.io",
            "job-boards.eu.greenhouse.io",
            "boards-api.greenhouse.io",
            "api.greenhouse.io",
        ),
        path_pattern=_path(rf"^/(?:v1/boards/)?(?:{_LOCALE}/)?(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.LEVER,
        hosts=("jobs.lever.co", "api.lever.co", "jobs.eu.lever.co"),
        path_pattern=_path(r"^/(?:v0/postings/)?(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.ASHBY,
        hosts=("jobs.ashbyhq.com", "api.ashbyhq.com"),
        path_pattern=_path(r"^/(?:posting-api/job-board/)?(?P<slug>[^/?#]+)"),
        slug_validator=safe_path_slug,
    ),
    UrlPattern(
        platform=Platform.SMARTRECRUITERS,
        hosts=(
            "careers.smartrecruiters.com",
            "jobs.smartrecruiters.com",
            "api.smartrecruiters.com",
        ),
        path_pattern=_path(r"^/(?:v1/companies/)?(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.WORKABLE,
        hosts=("apply.workable.com",),
        path_pattern=_path(r"^/(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.JOBVITE,
        hosts=("jobs.jobvite.com",),
        path_pattern=_path(r"^/(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.JOIN,
        hosts=("join.com", "www.join.com"),
        path_pattern=_path(r"^/companies/(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.BAMBOOHR,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.bamboohr\.com$"),
    ),
    UrlPattern(
        platform=Platform.RECRUITEE,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.recruitee\.com$"),
    ),
    UrlPattern(
        platform=Platform.BREEZY,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.breezy\.hr$"),
    ),
    UrlPattern(
        platform=Platform.COLLAGE,
        hosts=("secure.collage.co", "api.collage.co"),
        path_pattern=_path(r"^/(?:jobs|v1/positions)/(?P<slug>[^/?#]+)"),
    ),
    UrlPattern(
        platform=Platform.TEAMTAILOR,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.teamtailor\.com$"),
    ),
    UrlPattern(
        platform=Platform.WORKABLE,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.workable\.com$"),
    ),
    UrlPattern(
        platform=Platform.PERSONIO,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)$"),
    ),
    UrlPattern(
        platform=Platform.TALEO,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.taleo\.net$"),
    ),
    UrlPattern(
        platform=Platform.ICIMS,
        host_pattern=re.compile(r"^careers-(?P<slug>[a-z0-9][a-z0-9-]*)\.icims\.com$"),
    ),
    UrlPattern(
        platform=Platform.NJOYN,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.njoyn\.com$"),
    ),
    UrlPattern(
        platform=Platform.EIGHTFOLD,
        host_pattern=re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)\.eightfold\.ai$"),
    ),
)

_RESERVED_PATH_SEGMENTS = frozenset({"jobs", "job", "careers", "search", "embed", "api", "v1"})


def _split(url: str) -> tuple[str, str] | None:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url if "//" in url else f"https://{url}")
    except ValueError:
        return None
    if parts.scheme not in ("https", "http", ""):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    return host, parts.path or "/"


def _workday(host: str, path: str) -> PlatformCandidate | None:
    matched = _WORKDAY_HOST.match(host)
    if matched is None:
        return None
    tenant = matched.group("tenant")
    trimmed = re.sub(rf"^/wday/cxs/{re.escape(tenant)}/", "/", path, flags=re.IGNORECASE)
    trimmed = re.sub(rf"^/{_LOCALE}/", "/", trimmed)
    segments = [segment for segment in trimmed.split("/") if segment]
    site = segments[0] if segments else None
    if site is not None and site.lower() in {"jobs", "job"}:
        site = None
    return PlatformCandidate(
        platform=Platform.WORKDAY,
        slug=tenant,
        workday_tenant=tenant,
        workday_site=site,
        workday_dc=matched.group("dc"),
    )


def _successfactors(host: str, path: str) -> PlatformCandidate | None:
    if not re.match(r"^/(?:go/|content/(?:\?|$))", path, flags=re.IGNORECASE):
        return None
    slug = _vanity_slug(host)
    if slug is None:
        return None
    return PlatformCandidate(platform=Platform.SUCCESSFACTORS, slug=slug)


_CAREERS_SUBDOMAINS = frozenset(
    {
        "emploi",
        "emplois",
        "career",
        "careers",
        "carriere",
        "carrieres",
        "job",
        "jobs",
        "recruiting",
        "recrutement",
        "talent",
        "www",
    }
)


def _vanity_slug(host: str) -> str | None:
    labels = [fold(label) for label in host.split(".") if label]
    while labels and labels[0] in _CAREERS_SUBDOMAINS:
        labels.pop(0)
    if len(labels) < 2:
        return None
    slug = labels[0]
    if slug in _CAREERS_SUBDOMAINS or not SAFE_SLUG.match(slug):
        return None
    return slug


_EXPERIENCE_PATH = re.compile(r"^/[a-z]{2}_[A-Za-z]{2}/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", re.I)


def _eightfold(url: str, host: str, path: str) -> PlatformCandidate | None:
    from urllib.parse import parse_qs, urlsplit

    if host.endswith(".eightfold.ai"):
        return PlatformCandidate(platform=Platform.EIGHTFOLD, slug=host.split(".")[0])
    if not path.lower().startswith("/careers"):
        return None
    try:
        query = parse_qs(urlsplit(url if "//" in url else f"https://{url}").query)
    except ValueError:
        return None
    domains = query.get("domain") or ()
    if not domains:
        return None
    slug = _vanity_slug(domains[0].lower())
    if slug is None:
        return None
    return PlatformCandidate(platform=Platform.EIGHTFOLD, slug=slug)


def _experience_layer(host: str, path: str) -> PlatformCandidate | None:
    if not _EXPERIENCE_PATH.match(path):
        return None
    slug = _vanity_slug(host)
    if slug is None:
        return None
    return PlatformCandidate(platform=Platform.AVATURE, slug=slug, resolves_board=False)


def _oracle_cloud(host: str, path: str) -> PlatformCandidate | None:
    if not host.endswith(".oraclecloud.com"):
        return None
    if "/hcmui/candidateexperience" not in path.lower():
        return None
    slug = host.split(".")[0]
    if not SAFE_SLUG.match(slug):
        return None
    matched = re.search(r"/sites/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/|$)", path, re.I)
    site = matched.group(1) if matched is not None else None
    return PlatformCandidate(
        platform=Platform.ORACLE_CLOUD,
        slug=slug,
        oracle_host=host,
        oracle_site=site,
        resolves_board=site is not None,
    )


def identify_url(url: str) -> PlatformCandidate | None:
    split = _split(url)
    if split is None:
        return None
    host, path = split

    eightfold = _eightfold(url, host, path)
    if eightfold is not None:
        return eightfold

    workday = _workday(host, path)
    if workday is not None:
        return workday

    oracle = _oracle_cloud(host, path)
    if oracle is not None:
        return oracle

    for pattern in URL_PATTERNS:
        if pattern.host_pattern is not None:
            host_match = pattern.host_pattern.match(host)
            if host_match is not None:
                return PlatformCandidate(pattern.platform, host_match.group("slug"))
            continue
        if host not in pattern.hosts or pattern.path_pattern is None:
            continue
        path_match = pattern.path_pattern.match(path)
        if path_match is None:
            continue
        try:
            slug = pattern.slug_validator(path_match.group("slug"))
        except SlugRejectedError:
            continue
        if slug in _RESERVED_PATH_SEGMENTS:
            continue
        return PlatformCandidate(pattern.platform, slug)

    return _successfactors(host, path) or _experience_layer(host, path)


def dig(payload: Any, path: str) -> Any:
    if path == "":
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            if not part.isdigit():
                return None
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def first_str(payload: Any, paths: tuple[str, ...]) -> str:
    for path in paths:
        value = dig(payload, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def first_int(payload: Any, paths: tuple[str, ...]) -> int | None:
    for path in paths:
        value = dig(payload, path)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def job_count(payload: Any, probe: PlatformProbe) -> int | None:
    counted = first_int(payload, probe.count_paths)
    if counted is not None:
        return counted
    for path in probe.jobs_paths:
        value = dig(payload, path)
        if isinstance(value, list):
            return len(value)
    return None
