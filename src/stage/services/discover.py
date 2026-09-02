import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from stage.domain import (
    CandidateSkipped,
    Company,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    EmployerSize,
    Platform,
    PlatformCandidate,
    PlatformProbed,
    ProbeResult,
    ProbeVerdict,
    RequestLogged,
    SourceOfRecord,
    UrlResolved,
    UrlUnrecognized,
    first_line,
)
from stage.http import (
    HostBudgetExceededError,
    HttpClient,
    RatePosture,
    ValidatorCache,
    resolve,
)
from stage.lexicon import company_legal_suffixes, fold, generic_company_tokens, name_root_tokens
from stage.sources.platforms import (
    PROBES,
    PROBES_BY_PLATFORM,
    URL_ONLY_PLATFORMS,
    PlatformProbe,
    SlugRejectedError,
    first_str,
    identify_url,
    job_count,
    safe_slug,
)


class NoMatchingCompanyError(Exception):
    pass


MAX_CANDIDATES_PER_COMPANY = 3
DISCOVERY_PROFILE = "discovery"

SIZE_BANDS: dict[EmployerSize, tuple[int, int]] = {
    EmployerSize.STARTUP: (1, 300),
    EmployerSize.MID: (5, 3_000),
    EmployerSize.LARGE: (25, 60_000),
}

GENERIC_TOKENS = generic_company_tokens()
_LEGAL_SUFFIXES = company_legal_suffixes()

MIN_DISTINCTIVE_TOKEN = 4


@dataclass(frozen=True, slots=True)
class SlugPlan:
    accepted: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


def slug_candidates(name: str) -> SlugPlan:
    tokens = list(name_root_tokens(name))
    if not tokens:
        return SlugPlan((), ())

    accepted: list[str] = []
    skipped: list[tuple[str, str]] = []

    def offer(candidate: str, reason: str | None = None) -> None:
        if reason is not None:
            skipped.append((candidate, reason))
            return
        if candidate in accepted or not candidate:
            return
        try:
            accepted.append(safe_slug(candidate))
        except SlugRejectedError as exc:
            skipped.append((candidate, str(exc)))

    offer("".join(tokens))
    if len(tokens) > 1:
        offer("-".join(tokens))
        head = tokens[0]
        if head in GENERIC_TOKENS:
            offer(head, f"{head!r} is a generic first token — the confirmed false-positive shape")
        elif len(head) < MIN_DISTINCTIVE_TOKEN:
            offer(head, f"{head!r} is too short to be distinctive on its own")
        else:
            offer(head)

    return SlugPlan(tuple(accepted[:MAX_CANDIDATES_PER_COMPANY]), tuple(skipped))


def name_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in fold(value).split() if token not in _LEGAL_SUFFIXES)


def _joined(value: str) -> str:
    return "".join(token for token in fold(value).split() if token not in _LEGAL_SUFFIXES)


def _acquisition_named(company: Company) -> bool:
    return company.name_gate_exempt


def name_matches(company: str, board_name: str) -> bool:
    wanted = name_tokens(company)
    found = name_tokens(board_name)
    if not wanted or not found:
        return False
    if _joined(company) == _joined(board_name):
        return True
    if not (wanted <= found or found <= wanted):
        return False
    overlap = wanted & found
    if overlap - GENERIC_TOKENS:
        return True
    return len(overlap) >= 2 or wanted == found


def _count_verdict(count: int | None, size: EmployerSize | None) -> str:
    if count is None or size is None:
        return ""
    low, high = SIZE_BANDS[size]
    if count < low:
        return f"{count} posting(s) is below the plausible floor for a {size.value} employer"
    if count > high:
        return f"{count} posting(s) exceeds the plausible ceiling for a {size.value} employer"
    return ""


def classify(
    company: str,
    candidate: PlatformCandidate,
    url: str,
    payload: object,
    probe: PlatformProbe,
    *,
    board_name: str,
    size: EmployerSize | None,
) -> ProbeResult:
    count = job_count(payload, probe)

    def result(verdict: ProbeVerdict, detail: str = "") -> ProbeResult:
        return ProbeResult(
            company=company,
            candidate=candidate,
            verdict=verdict,
            url=url,
            board_name=board_name,
            job_count=count,
            detail=detail,
        )

    if count is None:
        return result(ProbeVerdict.MISS, "no job collection in the response")
    if count == 0:
        return result(ProbeVerdict.EMPTY, "board exists but is empty — nothing to verify against")

    implausible = _count_verdict(count, size)
    if implausible:
        return result(ProbeVerdict.REJECTED, implausible)

    if not board_name:
        return result(
            ProbeVerdict.UNVERIFIED,
            f"{probe.platform.value} exposes no board name — confirm by hand before adding",
        )
    if not name_matches(company, board_name):
        return result(
            ProbeVerdict.REJECTED,
            f"board is named {board_name!r}, which does not contain {company!r}",
        )
    return result(ProbeVerdict.MATCH)


ClientFactory = Callable[[frozenset[str], RatePosture], HttpClient]


def _default_client(hosts: frozenset[str], posture: RatePosture) -> HttpClient:
    return HttpClient(allowed_hosts=hosts, posture=posture, cache=ValidatorCache())


def resolve_careers_url(url: str) -> DiscoveryEvent:
    candidate = identify_url(url)
    if candidate is None:
        return UrlUnrecognized(
            url=url,
            detail=(
                "no known ATS in this URL's shape. DevTools -> Network, filter "
                "Fetch/XHR: if a request returns the job list, this is custom_json"
            ),
        )
    detail = ""
    if candidate.platform is Platform.WORKDAY:
        if candidate.workday_site is None:
            detail = "tenant and datacenter resolved, but the site segment is missing from the URL"
        else:
            detail = "workday_facet resolves on first contact, which lands in build step 6"
    elif candidate.platform in URL_ONLY_PLATFORMS:
        detail = f"{candidate.platform.value} has no probeable board endpoint — accepted on shape"
    return UrlResolved(url=url, candidate=candidate, detail=detail)


def direct_companies_from_apply_urls(
    apply_urls: Mapping[str, Sequence[str]],
    *,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
) -> tuple[Company, ...]:
    allowed = {probe.platform for probe in _selected_probes(platforms, excluded)}
    rank = {probe.platform: index for index, probe in enumerate(PROBES)}
    direct: list[Company] = []
    for name, urls in apply_urls.items():
        candidates = [
            candidate
            for url in urls
            if (candidate := identify_url(url)) is not None and candidate.platform in allowed
        ]
        if not candidates:
            continue
        candidate = min(candidates, key=lambda value: (rank[value.platform], value.slug))
        direct.append(to_company(name, candidate))
    return tuple(direct)


def select_unregistered(
    ranked: Sequence[str],
    apply_urls: Mapping[str, Sequence[str]],
    *,
    limit: int,
    direct_only: bool,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
) -> tuple[tuple[Company, ...], tuple[str, ...]]:
    order = {name: index for index, name in enumerate(ranked)}
    direct = direct_companies_from_apply_urls(apply_urls, platforms=platforms, excluded=excluded)
    ordered = tuple(sorted(direct, key=lambda company: order.get(company.name, len(order))))[:limit]
    if direct_only:
        return ordered, ()
    taken = {company.name for company in ordered}
    return ordered, tuple(name for name in ranked if name not in taken)[:limit]


def is_routable(platform: Platform) -> bool:
    from stage.sources import adapter_for_platform

    return adapter_for_platform(platform) is not None


def to_company(
    name: str,
    candidate: PlatformCandidate,
    *,
    verified_on: date | None = None,
) -> Company:
    routable = is_routable(candidate.platform)
    return Company(
        name=name,
        platform=candidate.platform,
        slug=candidate.slug,
        enabled=routable and verified_on is not None,
        source_of_record=SourceOfRecord.DISCOVER,
        last_verified=verified_on,
        workday_tenant=candidate.workday_tenant,
        workday_site=candidate.workday_site,
        workday_dc=candidate.workday_dc,
        oracle_host=candidate.oracle_host,
        oracle_site=candidate.oracle_site,
    )


def _selected_probes(
    platforms: Sequence[Platform] | None,
    excluded: Sequence[Platform] | None = None,
) -> tuple[PlatformProbe, ...]:
    if platforms is None:
        chosen = list(PROBES)
    else:
        chosen = [
            PROBES_BY_PLATFORM[platform] for platform in platforms if platform in PROBES_BY_PLATFORM
        ]
    if excluded is None:
        return tuple(chosen)
    skip = set(excluded)
    return tuple(probe for probe in chosen if probe.platform not in skip)


async def probe_companies(
    names: Sequence[str],
    *,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
    size: EmployerSize | None = None,
    client_factory: ClientFactory = _default_client,
) -> AsyncIterator[DiscoveryEvent]:
    probes = _selected_probes(platforms, excluded)
    plans = {name: slug_candidates(name) for name in names}

    started = time.perf_counter()
    yield DiscoveryStarted(
        companies=tuple(names),
        platforms=tuple(probe.platform.value for probe in probes),
        probes_planned=sum(len(plan.accepted) for plan in plans.values()) * len(probes),
    )

    seen_skips: set[tuple[str, str]] = set()
    for name, plan in plans.items():
        for slug, reason in plan.skipped:
            if (name, slug) in seen_skips:
                continue
            seen_skips.add((name, slug))
            yield CandidateSkipped(company=name, slug=slug, reason=reason)

    matched: list[ProbeResult] = []
    unverified: list[ProbeResult] = []
    rejected: list[ProbeResult] = []
    missed = 0
    errors = 0
    requests = 0
    ceiling_hit: list[str] = []
    decode_failures: dict[str, int] = {}
    attempts: dict[str, int] = {}

    for probe in probes:
        posture = resolve(probe.rate_profile, [DISCOVERY_PROFILE])
        hosts = frozenset(probe.host_for(slug) for plan in plans.values() for slug in plan.accepted)
        if not hosts:
            continue
        blocked = False
        async with client_factory(hosts, posture) as client:
            for name, plan in plans.items():
                if blocked:
                    break
                for slug in plan.accepted:
                    candidate = PlatformCandidate(probe.platform, slug)
                    url = probe.url_for(slug)
                    key = probe.platform.value
                    attempts[key] = attempts.get(key, 0) + 1

                    response = None
                    failure: Exception | None = None
                    board_name = ""
                    try:
                        response = await client.get_json(url, params=dict(probe.params))
                    except Exception as exc:
                        failure = exc
                    if response is not None:
                        board_name = first_str(response.payload, probe.name_paths)
                        if not board_name and probe.verify_url is not None:
                            board_name = await _verify_name(client, probe, slug)

                    for record in client.drain_log():
                        yield RequestLogged(
                            source="discover",
                            method=record.method,
                            url=record.url,
                            status=record.status,
                            elapsed_ms=record.elapsed_ms,
                            attempt=record.attempt,
                            error=record.error,
                        )

                    if isinstance(failure, HostBudgetExceededError):
                        blocked = True
                        ceiling_hit.append(f"{probe.platform.value}: {failure}")
                        break
                    if failure is not None or response is None:
                        absent = failure is not None and _is_absent(failure)
                        if isinstance(failure, ValueError):
                            decode_failures[key] = decode_failures.get(key, 0) + 1
                        if absent:
                            missed += 1
                        else:
                            errors += 1
                        yield PlatformProbed(
                            ProbeResult(
                                company=name,
                                candidate=candidate,
                                verdict=ProbeVerdict.MISS if absent else ProbeVerdict.ERROR,
                                url=url,
                                detail=f"{type(failure).__name__}: {failure}",
                            )
                        )
                        continue

                    result = classify(
                        name,
                        candidate,
                        url,
                        response.payload,
                        probe,
                        board_name=board_name,
                        size=size,
                    )
                    match result.verdict:
                        case ProbeVerdict.MATCH:
                            matched.append(result)
                        case ProbeVerdict.UNVERIFIED:
                            unverified.append(result)
                        case ProbeVerdict.REJECTED:
                            rejected.append(result)
                        case ProbeVerdict.ERROR:
                            errors += 1
                        case _:
                            missed += 1
                    yield PlatformProbed(result)
            requests += client.request_count

    yield DiscoveryFinished(
        matched=tuple(matched),
        unverified=tuple(unverified),
        rejected=tuple(rejected),
        missed=missed,
        errors=errors,
        requests=requests,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        ceiling_hit=tuple(ceiling_hit),
        non_json=tuple(
            (platform, count)
            for platform, count in sorted(decode_failures.items())
            if count == attempts.get(platform, 0) and count >= 2
        ),
    )


async def _verify_name(client: HttpClient, probe: PlatformProbe, slug: str) -> str:
    target = probe.verify_url_for(slug)
    if target is None:
        return ""
    try:
        response = await client.get_json(target)
    except Exception:
        return ""
    return first_str(response.payload, probe.verify_name_paths)


def _is_absent(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (401, 403, 404, 410)


async def verify_registry(
    companies: Sequence[Company],
    *,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
    only: Sequence[str] | None = None,
    client_factory: ClientFactory = _default_client,
) -> AsyncIterator[DiscoveryEvent]:
    wanted = None if only is None else {fold(name) for name in only}
    selected = [
        company
        for company in companies
        if company.platform in PROBES_BY_PLATFORM
        and (platforms is None or company.platform in platforms)
        and (excluded is None or company.platform not in excluded)
        and (wanted is None or fold(company.name) in wanted)
    ]
    if wanted is not None and not selected:
        raise NoMatchingCompanyError(f"no registry row matches: {', '.join(sorted(only or ()))}")
    grouped: dict[Platform, list[Company]] = {}
    for company in selected:
        grouped.setdefault(company.platform, []).append(company)

    started = time.perf_counter()
    yield DiscoveryStarted(
        companies=tuple(company.name for company in selected),
        platforms=tuple(platform.value for platform in sorted(grouped, key=lambda p: p.value)),
        probes_planned=len(selected),
    )

    matched: list[ProbeResult] = []
    unverified: list[ProbeResult] = []
    rejected: list[ProbeResult] = []
    missed = 0
    errors = 0
    requests = 0
    ceiling_hit: list[str] = []

    for platform in sorted(grouped, key=lambda item: item.value):
        probe = PROBES_BY_PLATFORM[platform]
        rows = grouped[platform]
        posture = resolve(probe.rate_profile, [])
        hosts = frozenset(probe.host_for(company.slug) for company in rows)
        blocked = False
        async with client_factory(hosts, posture) as client:
            for company in rows:
                if blocked:
                    break
                candidate = PlatformCandidate(platform, company.slug)
                url = probe.url_for(company.slug)
                response = None
                failure: Exception | None = None
                board_name = ""
                try:
                    response = await client.get_json(url, params=dict(probe.params))
                except Exception as exc:
                    failure = exc
                if response is not None:
                    board_name = first_str(response.payload, probe.name_paths)
                    if not board_name and probe.verify_url is not None:
                        board_name = await _verify_name(client, probe, company.slug)

                for record in client.drain_log():
                    yield RequestLogged(
                        source="verify",
                        method=record.method,
                        url=record.url,
                        status=record.status,
                        elapsed_ms=record.elapsed_ms,
                        attempt=record.attempt,
                        error=record.error,
                    )

                if isinstance(failure, HostBudgetExceededError):
                    blocked = True
                    ceiling_hit.append(f"{platform.value}: {failure}")
                    break
                if failure is not None or response is None:
                    absent = failure is not None and _is_absent(failure)
                    if absent:
                        missed += 1
                    else:
                        errors += 1
                    yield PlatformProbed(
                        ProbeResult(
                            company=company.name,
                            candidate=candidate,
                            verdict=ProbeVerdict.MISS if absent else ProbeVerdict.ERROR,
                            url=url,
                            detail=f"{type(failure).__name__}: {failure}",
                        )
                    )
                    continue

                result = classify(
                    company.name,
                    candidate,
                    url,
                    response.payload,
                    probe,
                    board_name=board_name,
                    size=None,
                )
                if result.verdict is ProbeVerdict.REJECTED and _acquisition_named(company):
                    result = replace(result, verdict=ProbeVerdict.UNVERIFIED)
                match result.verdict:
                    case ProbeVerdict.MATCH:
                        matched.append(result)
                    case ProbeVerdict.UNVERIFIED:
                        unverified.append(result)
                    case ProbeVerdict.REJECTED:
                        rejected.append(result)
                    case ProbeVerdict.ERROR:
                        errors += 1
                    case _:
                        missed += 1
                yield PlatformProbed(result)
            requests += client.request_count

    yield DiscoveryFinished(
        matched=tuple(matched),
        unverified=tuple(unverified),
        rejected=tuple(rejected),
        missed=missed,
        errors=errors,
        requests=requests,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        ceiling_hit=tuple(ceiling_hit),
    )


def apply_verification(
    companies: Sequence[Company], outcome: DiscoveryFinished, today: date
) -> tuple[tuple[Company, ...], int, int]:
    live = {result.company for result in outcome.matched}
    dead = {result.company: result.detail or result.verdict.value for result in outcome.rejected}
    updated: list[Company] = []
    verified = disabled = 0
    for company in companies:
        if company.name in live:
            if company.last_verified != today or not company.enabled:
                verified += 1
            updated.append(replace(company, enabled=True, last_verified=today, notes=None))
        elif company.name in dead:
            if company.enabled:
                disabled += 1
            updated.append(
                replace(
                    company,
                    enabled=False,
                    last_verified=None,
                    notes=f"{today.isoformat()}: {first_line(dead[company.name])[:200]}",
                )
            )
        else:
            updated.append(company)
    return tuple(updated), verified, disabled


GENERIC_SLUG_MAX = 6


@dataclass(frozen=True, slots=True)
class AdoptedRow:
    company: Company
    job_count: int


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    company: str
    platform: Platform
    slug: str
    job_count: int
    distinctive: bool

    @property
    def label(self) -> str:
        return f"{self.platform.value}/{self.slug}"


@dataclass(frozen=True, slots=True)
class AdoptionReport:
    adopted: tuple[AdoptedRow, ...]
    refused: tuple[tuple[str, str, str], ...]
    already_known: int
    probed: int
    applied: bool
    review: tuple[ReviewCandidate, ...] = ()

    @property
    def postings(self) -> int:
        return sum(row.job_count for row in self.adopted)


def slug_is_distinctive(company: str, slug: str) -> bool:
    bare = slug.replace("-", "").replace("_", "")
    return len(bare) > GENERIC_SLUG_MAX and name_matches(company, slug)


def adoption_refusal(result: ProbeResult) -> str:
    if result.verdict is not ProbeVerdict.MATCH:
        return f"verdict {result.verdict.value}, only a self-naming board is adopted"
    if (result.job_count or 0) < 1:
        return "board answered with no postings"
    return ""


def needs_review(result: ProbeResult) -> bool:
    return result.verdict is ProbeVerdict.UNVERIFIED and (result.job_count or 0) > 0


def _adopted_note(today: date, result: ProbeResult) -> str:
    return (
        f"{today}: seen in feed postings but absent from the registry; discover matched "
        f"{result.candidate.platform.value}/{result.candidate.slug} with {result.job_count} "
        "job(s) and the board named itself, so it is polled directly instead of secondhand"
    )


def _unnamed_note(today: date, result: ProbeResult) -> str:
    platform = result.candidate.platform.value
    return (
        f"{today}: token from this employer's own apply URL, not slug guessing; {platform} "
        f"publishes no board name, so provenance is the evidence ({result.job_count} job(s))"
    )


def adopt_unregistered(
    existing: Sequence[Company],
    results: Sequence[tuple[str, ProbeResult]],
    *,
    today: date,
    adopt_unnamed: bool = False,
) -> AdoptionReport:
    keys = {(row.platform, row.slug.lower()) for row in existing}
    captions = {row.name.casefold() for row in existing}
    adopted: list[AdoptedRow] = []
    refused: list[tuple[str, str, str]] = []
    review: list[ReviewCandidate] = []
    seen_review: set[tuple[Platform, str]] = set()
    known = 0

    for company, result in results:
        reason = adoption_refusal(result)
        if reason and not (adopt_unnamed and needs_review(result)):
            key = (result.candidate.platform, result.candidate.slug.lower())
            if result.verdict is ProbeVerdict.MATCH:
                refused.append((company, result.candidate.label, reason))
            elif needs_review(result) and key not in keys and key not in seen_review:
                seen_review.add(key)
                review.append(
                    ReviewCandidate(
                        company=company,
                        platform=result.candidate.platform,
                        slug=result.candidate.slug,
                        job_count=result.job_count or 0,
                        distinctive=slug_is_distinctive(company, result.candidate.slug),
                    )
                )
            continue
        key = (result.candidate.platform, result.candidate.slug.lower())
        if key in keys or company.casefold() in captions:
            known += 1
            continue
        keys.add(key)
        captions.add(company.casefold())
        adopted.append(
            AdoptedRow(
                company=Company(
                    name=company,
                    platform=result.candidate.platform,
                    slug=result.candidate.slug,
                    enabled=True,
                    last_verified=today,
                    source_of_record=SourceOfRecord.DISCOVER,
                    name_gate_exempt=result.verdict is not ProbeVerdict.MATCH,
                    notes=(
                        _adopted_note(today, result)
                        if result.verdict is ProbeVerdict.MATCH
                        else _unnamed_note(today, result)
                    ),
                ),
                job_count=result.job_count or 0,
            )
        )
    return AdoptionReport(
        adopted=tuple(adopted),
        refused=tuple(refused),
        already_known=known,
        probed=len({company for company, _ in results}),
        applied=False,
        review=tuple(review),
    )
