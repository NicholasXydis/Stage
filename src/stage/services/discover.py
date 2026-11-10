import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

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
    RateState,
    RequestLogged,
    SourceOfRecord,
    UrlResolved,
    UrlUnrecognized,
    first_line,
)
from stage.domain.robots import RobotsRules, robots_rules
from stage.domain.text import counted, did_you_mean, plain_error
from stage.http import (
    USER_AGENT,
    HostBudgetExceededError,
    HttpClient,
    RatePosture,
    ValidatorCache,
    failure_text,
    is_disconnected,
    resolve,
)
from stage.lexicon import company_legal_suffixes, fold, generic_company_tokens, name_root_tokens
from stage.sources import adapter_for_platform, platform_bucket, platform_front
from stage.sources.platforms import (
    BOARD_PROBES,
    PROBES_BY_PLATFORM,
    URL_ONLY_PLATFORMS,
    PlatformProbe,
    SlugRejectedError,
    first_str,
    identify_url,
    job_count,
    oracle_target,
    safe_slug,
    workday_target,
)

if TYPE_CHECKING:
    from stage.storage import AsyncRepository


class NoMatchingCompanyError(Exception):
    pass


MAX_CANDIDATES_PER_COMPANY = 3
DISCOVERY_PROFILE = "discovery"
ROBOTS_AGENT = USER_AGENT.split("/", 1)[0]
ROBOTS_TIMEOUT_S = 10.0
ROBOTS_AT_ONCE = 2
READ_BY_OWNER_DECISION = ("smartrecruiters.com", "ultipro.com", "ultipro.ca")
PATIENT_DELAY_S = 2.0

SIZE_BANDS: dict[EmployerSize, tuple[int, int]] = {
    EmployerSize.STARTUP: (1, 300),
    EmployerSize.MID: (5, 3_000),
    EmployerSize.LARGE: (25, 60_000),
}

GENERIC_TOKENS = generic_company_tokens()
_LEGAL_SUFFIXES = company_legal_suffixes()
_EVERY_PROBE = {**PROBES_BY_PLATFORM, **BOARD_PROBES}

MIN_DISTINCTIVE_TOKEN = 4

Board = Company | PlatformCandidate


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
            offer(
                head, f'"{head}" is a common first word, and it matched the wrong employer before'
            )
        elif len(head) < MIN_DISTINCTIVE_TOKEN:
            offer(head, f'"{head}" is too short to name one employer')
        else:
            offer(head)

    return SlugPlan(tuple(accepted[:MAX_CANDIDATES_PER_COMPANY]), tuple(skipped))


def name_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in fold(value).split() if token not in _LEGAL_SUFFIXES)


def _joined(value: str) -> str:
    return "".join(token for token in fold(value).split() if token not in _LEGAL_SUFFIXES)


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
        return f"{counted(count, 'posting')} is too few for a {size.value} employer"
    if count > high:
        return f"{counted(count, 'posting')} is too many for a {size.value} employer"
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
        return result(ProbeVerdict.MISS, "the answer has no job list")
    if count == 0:
        return result(ProbeVerdict.EMPTY, "the board exists but is empty, so nothing confirms it")
    implausible = _count_verdict(count, size)
    if implausible:
        return result(ProbeVerdict.REJECTED, implausible)
    if not board_name:
        return result(
            ProbeVerdict.UNVERIFIED,
            f"{probe.platform.value} publishes no board name, so confirm by hand that it is "
            "the right employer",
        )
    if not name_matches(company, board_name):
        return result(
            ProbeVerdict.REJECTED,
            f'the board is named "{board_name}", which does not contain "{company}"',
        )
    return result(ProbeVerdict.MATCH)


ClientFactory = Callable[[frozenset[str], RatePosture], HttpClient]


@dataclass(slots=True)
class StoredLimits:
    stored: dict[str, RateState] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    settled: dict[str, RateState] = field(default_factory=dict)

    def factory(self, buckets: Mapping[str, str]) -> ClientFactory:
        def build(hosts: frozenset[str], posture: RatePosture) -> HttpClient:
            return HttpClient(
                allowed_hosts=hosts,
                posture=posture,
                cache=ValidatorCache(),
                rate_state=self.stored,
                now=self.now,
                host_buckets=buckets,
                host_fronts={host: platform_front(host) for host in hosts},
            )

        return build

    def keep(self, client: HttpClient, heard: "_Heard") -> None:
        if heard.offline:
            return
        for state in client.rate_state(self.now):
            known = self.stored.get(state.bucket)
            if known is not None and state.min_interval_override is None:
                state = replace(state, min_interval_override=known.min_interval_override)
            self.stored[state.bucket] = self.settled[state.bucket] = state


async def stored_limits(repository: "AsyncRepository") -> StoredLimits:
    return StoredLimits(dict(await repository.load_rate_state()))


async def save_limits(repository: "AsyncRepository", limits: StoredLimits) -> None:
    from stage.storage import SourceBatch

    if limits.settled:
        await repository.apply_source_batch(
            SourceBatch(
                source="discover",
                run_started_at=limits.now,
                rate_state=tuple(limits.settled.values()),
            )
        )


@dataclass(slots=True)
class _Heard:
    answered: int = 0
    cut_off: int = 0
    stray: int = 0

    def note(self, failure: Exception | None) -> None:
        if failure is None or _status(failure) is not None:
            self.answered += 1
        elif is_disconnected(failure):
            self.cut_off += 1
        else:
            self.stray += 1

    @property
    def offline(self) -> bool:
        errors = self.cut_off + self.stray
        quiet = self.stray <= max(1, errors // 10)
        return not self.answered and errors >= 2 and self.cut_off > self.stray and quiet


def _platform_buckets(platform: Platform, hosts: Iterable[str]) -> dict[str, str]:
    adapter = adapter_for_platform(platform)
    shared = adapter.bucket_key if adapter is not None else ""
    return {host: shared or platform_bucket(host) for host in hosts}


def _client_for(
    client_factory: ClientFactory | None, limits: StoredLimits, buckets: Mapping[str, str]
) -> ClientFactory:
    return client_factory or limits.factory(buckets)


def resolve_careers_url(url: str) -> DiscoveryEvent:
    candidate = identify_url(url)
    if candidate is None:
        return UrlUnrecognized(
            url=url,
            detail=(
                "Stage does not recognize a job board in this link, so it may be a "
                "custom_json board"
            ),
        )
    detail = ""
    if candidate.platform is Platform.WORKDAY and candidate.workday_site is None:
        detail = "the link names the Workday tenant but not its site"
    elif candidate.platform in URL_ONLY_PLATFORMS:
        detail = (
            f"{candidate.platform.value} boards cannot be checked before they are added, "
            "so this one comes from the link alone"
        )
    return UrlResolved(url=url, candidate=candidate, detail=detail)


def direct_companies_from_apply_urls(
    apply_urls: Mapping[str, Sequence[str]],
    *,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
) -> tuple[Company, ...]:
    allowed = {probe.platform for probe in _selected_probes(platforms, excluded, boards=True)}
    rank = {platform: index for index, platform in enumerate(_EVERY_PROBE)}
    direct: list[Company] = []
    for name, urls in apply_urls.items():
        candidates = [
            candidate
            for url in urls
            if (candidate := identify_url(url)) is not None
            and candidate.platform in allowed
            and _reachable(candidate)
        ]
        if candidates:
            best = min(candidates, key=lambda value: (rank[value.platform], value.slug))
            direct.append(to_company(name, best))
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
    source: SourceOfRecord = SourceOfRecord.DISCOVER,
) -> Company:
    return Company(
        name=name,
        platform=candidate.platform,
        slug=candidate.slug,
        enabled=is_routable(candidate.platform) and verified_on is not None,
        source_of_record=source,
        last_verified=verified_on,
        workday_tenant=candidate.workday_tenant,
        workday_site=candidate.workday_site,
        workday_dc=candidate.workday_dc,
        oracle_host=candidate.oracle_host,
        oracle_site=candidate.oracle_site,
    )


def registered_as(rows: Sequence[Company], name: str) -> tuple[Company, ...]:
    wanted = _joined(name)
    return tuple(row for row in rows if wanted and wanted == _joined(row.name))


def registered_board(rows: Sequence[Company], candidate: PlatformCandidate) -> Company | None:
    from stage.companies import board_identity

    wanted = board_identity(to_company(candidate.slug, candidate))
    return next((row for row in rows if board_identity(row) == wanted), None)


def unaddable(candidate: PlatformCandidate) -> str:
    if not candidate.resolves_board:
        return f"{candidate.platform.value} links name a front end, not a board Stage can read"
    if candidate.platform is Platform.WORKDAY and candidate.workday_site is None:
        return "the link lacks the Workday site. Copy the link from the page that lists the jobs"
    if not is_routable(candidate.platform):
        return f"Stage cannot read {candidate.platform.value} boards yet"
    return ""


def discovered_row(
    name: str, candidate: PlatformCandidate, today: date, board_name: str = ""
) -> Company:
    if board_name:
        return replace(
            to_company(name, candidate, verified_on=today),
            notes=f'{today}: added with stage discover, the board names itself "{board_name}"',
        )
    return replace(
        to_company(name, candidate),
        enabled=is_routable(candidate.platform),
        notes=f"{today}: added with stage discover --url from this employer's careers link",
    )


def _selected_probes(
    platforms: Sequence[Platform] | None,
    excluded: Sequence[Platform] | None = None,
    *,
    boards: bool = False,
) -> tuple[PlatformProbe, ...]:
    catalogue = _EVERY_PROBE if boards else PROBES_BY_PLATFORM
    chosen = (
        catalogue.values()
        if platforms is None
        else [catalogue[platform] for platform in dict.fromkeys(platforms) if platform in catalogue]
    )
    return tuple(probe for probe in chosen if excluded is None or probe.platform not in excluded)


@dataclass(slots=True)
class _Tally:
    matched: list[ProbeResult] = field(default_factory=list)
    unverified: list[ProbeResult] = field(default_factory=list)
    rejected: list[ProbeResult] = field(default_factory=list)
    missed: int = 0
    errors: int = 0
    requests: int = 0
    ceiling_hit: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    unreadable: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)

    def counted(self, result: ProbeResult) -> PlatformProbed:
        match result.verdict:
            case ProbeVerdict.MATCH:
                self.matched.append(result)
            case ProbeVerdict.UNVERIFIED:
                self.unverified.append(result)
            case ProbeVerdict.REJECTED:
                self.rejected.append(result)
            case ProbeVerdict.ERROR:
                self.errors += 1
            case _:
                self.missed += 1
        return PlatformProbed(result)

    def finished(self, *, non_json: bool) -> DiscoveryFinished:
        unanswered = tuple(
            (platform, count)
            for platform, count in sorted(self.unreadable.items())
            if count == self.attempts.get(platform, 0) and count >= 2
        )
        return DiscoveryFinished(
            matched=tuple(self.matched),
            unverified=tuple(self.unverified),
            rejected=tuple(self.rejected),
            missed=self.missed,
            errors=self.errors,
            requests=self.requests,
            elapsed_ms=(time.perf_counter() - self.started) * 1000,
            ceiling_hit=tuple(self.ceiling_hit),
            non_json=unanswered if non_json else (),
        )


async def probe_companies(
    names: Sequence[str],
    *,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
    size: EmployerSize | None = None,
    client_factory: ClientFactory | None = None,
    limits: StoredLimits | None = None,
) -> AsyncIterator[DiscoveryEvent]:
    paced = limits or StoredLimits()
    probes = _selected_probes(platforms, excluded)
    plans = {name: slug_candidates(name) for name in names}
    tally = _Tally()
    yield DiscoveryStarted(
        companies=tuple(names),
        platforms=tuple(probe.platform.value for probe in probes),
        probes_planned=sum(len(plan.accepted) for plan in plans.values()) * len(probes),
    )
    for name, plan in plans.items():
        for slug, reason in plan.skipped:
            yield CandidateSkipped(company=name, slug=slug, reason=reason)
    for probe in probes:
        boards = [
            (name, PlatformCandidate(probe.platform, slug))
            for name, plan in plans.items()
            for slug in plan.accepted
        ]
        if not boards:
            continue
        posture = resolve(probe.rate_profile, [DISCOVERY_PROFILE])
        hosts = _hosts(probe, boards)
        build = _client_for(client_factory, paced, _platform_buckets(probe.platform, hosts))
        heard = _Heard()
        async with build(hosts, posture) as client:
            async for event in _sweep(
                client, probe, boards, tally, heard, source="discover", size=size
            ):
                yield event
            tally.requests += client.request_count
            paced.keep(client, heard)
    yield tally.finished(non_json=True)


async def verify_registry(
    companies: Sequence[Company],
    *,
    platforms: Sequence[Platform] | None = None,
    excluded: Sequence[Platform] | None = None,
    only: Sequence[str] | None = None,
    client_factory: ClientFactory | None = None,
    board_checks: bool = False,
    limits: StoredLimits | None = None,
) -> AsyncIterator[DiscoveryEvent]:
    paced = limits or StoredLimits()
    wanted = None if only is None else {fold(name) for name in only}
    catalogue = _EVERY_PROBE if board_checks else PROBES_BY_PLATFORM
    selected = [
        company
        for company in companies
        if company.platform in catalogue
        and _reachable(company)
        and (platforms is None or company.platform in platforms)
        and (excluded is None or company.platform not in excluded)
        and (wanted is None or fold(company.name) in wanted)
    ]
    if wanted is not None and not selected:
        raise NoMatchingCompanyError(_no_such_rows(only or (), companies))
    grouped: dict[Platform, list[Company]] = {}
    for company in selected:
        grouped.setdefault(company.platform, []).append(company)
    ordered = sorted(grouped, key=lambda platform: platform.value)
    tally = _Tally()
    yield DiscoveryStarted(
        companies=tuple(company.name for company in selected),
        platforms=tuple(platform.value for platform in ordered),
        probes_planned=len(selected),
    )
    for platform in ordered:
        probe = catalogue[platform]
        boards = [(company.name, company) for company in grouped[platform]]
        posture = resolve(probe.rate_profile, []).sized_for(len(boards), len(boards) // 4)
        hosts = _hosts(probe, boards)
        build = _client_for(client_factory, paced, _platform_buckets(platform, hosts))
        heard = _Heard()
        async with build(hosts, posture) as client:
            async for event in _sweep(client, probe, boards, tally, heard, source="verify"):
                yield event
            tally.requests += client.request_count
            paced.keep(client, heard)
    yield tally.finished(non_json=False)


def _no_such_rows(only: Sequence[str], companies: Sequence[Company]) -> str:
    from difflib import get_close_matches

    names = {fold(company.name): company.name for company in companies}
    near = [
        names[match]
        for asked in only
        for match in get_close_matches(fold(asked), list(names), n=1, cutoff=0.75)
    ]
    asked = ", ".join(f'"{name}"' for name in sorted(only))
    hint = did_you_mean(near[0]) if near else "Check the spelling, or leave out --company."
    return f"No registry row is named {asked}. {hint}"


def _hosts(probe: PlatformProbe, boards: Sequence[tuple[str, Board]]) -> frozenset[str]:
    return frozenset(_target(probe, board)[0] for _, board in boards)


async def _sweep(
    client: HttpClient,
    probe: PlatformProbe,
    boards: Sequence[tuple[str, Board]],
    tally: _Tally,
    heard: _Heard,
    *,
    source: str,
    size: EmployerSize | None = None,
) -> AsyncIterator[DiscoveryEvent]:
    key = probe.platform.value
    for name, board in boards:
        candidate = board if isinstance(board, PlatformCandidate) else candidate_for(board)
        url = _target(probe, board)[1]
        tally.attempts[key] = tally.attempts.get(key, 0) + 1
        payload, board_name, failure = await _ask(client, probe, board, url)
        heard.note(failure)
        for record in client.drain_log():
            yield RequestLogged(
                source=source,
                method=record.method,
                url=record.url,
                status=record.status,
                elapsed_ms=record.elapsed_ms,
                attempt=record.attempt,
                error=record.error,
            )
        if isinstance(failure, HostBudgetExceededError):
            tally.ceiling_hit.append(f"{key}: {plain_error(str(failure))}")
            return
        if failure is not None:
            if isinstance(failure, ValueError):
                tally.unreadable[key] = tally.unreadable.get(key, 0) + 1
            absent = _is_absent(failure)
            yield tally.counted(
                ProbeResult(
                    company=name,
                    candidate=candidate,
                    verdict=ProbeVerdict.MISS if absent else ProbeVerdict.ERROR,
                    url=url,
                    detail=failure_text(failure),
                    status=_status(failure),
                )
            )
            continue
        result = classify(name, candidate, url, payload, probe, board_name=board_name, size=size)
        exempt = isinstance(board, Company) and board.name_gate_exempt
        if result.verdict is ProbeVerdict.REJECTED and exempt:
            result = replace(result, verdict=ProbeVerdict.UNVERIFIED)
        yield tally.counted(result)


async def _ask(
    client: HttpClient, probe: PlatformProbe, board: Board, url: str
) -> tuple[object, str, Exception | None]:
    try:
        payload = await _payload(client, probe, board, url)
    except Exception as exc:
        return None, "", exc
    board_name = first_str(payload, probe.name_paths)
    if not board_name and probe.verify_url is not None:
        board_name = await _verify_name(client, probe, board.slug)
    return payload, board_name, None


async def _verify_name(client: HttpClient, probe: PlatformProbe, slug: str) -> str:
    target = probe.verify_url_for(slug)
    if target is None:
        return ""
    try:
        response = await client.get_json(target)
    except Exception:
        return ""
    return first_str(response.payload, probe.verify_name_paths)


def _status(exc: Exception) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _is_absent(exc: Exception) -> bool:
    return isinstance(exc, ValueError) or _status(exc) in (404, 410)


def is_gone(result: ProbeResult) -> bool:
    return result.verdict is ProbeVerdict.REJECTED or (
        result.verdict is ProbeVerdict.MISS and result.status in (404, 410)
    )


def gone_reason(result: ProbeResult) -> str:
    if result.verdict is ProbeVerdict.MISS and result.status is not None:
        return f"the board answered {result.status}"
    return first_line(result.detail or result.verdict.value)[:200]


def candidate_for(company: Company) -> PlatformCandidate:
    return PlatformCandidate(
        platform=company.platform,
        slug=company.slug,
        workday_tenant=company.workday_tenant,
        workday_site=company.workday_site,
        workday_dc=company.workday_dc,
        oracle_host=company.oracle_host,
        oracle_site=company.oracle_site,
    )


def _target(probe: PlatformProbe, board: Board) -> tuple[str, str]:
    if board.platform is Platform.WORKDAY:
        host, path = workday_target(
            board.workday_tenant or "", board.workday_site or "", board.workday_dc or ""
        )
        return host, f"https://{host}{path}"
    if board.platform is Platform.ORACLE_CLOUD:
        from stage.sources.oracle_cloud import PATH

        host, _ = oracle_target(board.oracle_host or "", board.oracle_site or "")
        return host, f"https://{host}{PATH}"
    return probe.host_for(board.slug), probe.url_for(board.slug)


def _reachable(board: Board) -> bool:
    probe = BOARD_PROBES.get(board.platform)
    if probe is None:
        return True
    try:
        _target(probe, board)
    except SlugRejectedError:
        return False
    return True


WORKDAY_PROBE_BODY = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}


async def _payload(client: HttpClient, probe: PlatformProbe, board: Board, url: str) -> object:
    if board.platform is Platform.WORKDAY:
        return (await client.post_json(url, body=dict(WORKDAY_PROBE_BODY))).payload
    if board.platform is Platform.ORACLE_CLOUD:
        _, site = oracle_target(board.oracle_host or "", board.oracle_site or "")
        finder = f"findReqs;siteNumber={site},limit=1"
        params = {"onlyData": "true", "expand": "requisitionList", "finder": finder}
        return (await client.get_json(url, params=params)).payload
    if board.platform is Platform.JOBVITE:
        return _jobvite_total((await client.get_text(url)).text)
    return (await client.get_json(url, params=dict(probe.params))).payload


def _jobvite_total(text: str) -> dict[str, int]:
    from stage.sources.jobvite import read_page

    page = read_page(text)
    if not page.listing:
        return {"total": 0} if page.declared_empty else {}
    return {"total": len(page.rows)}


def _robots_target(board: Board) -> tuple[str, tuple[str, ...]] | None:
    probe = _EVERY_PROBE.get(board.platform)
    if probe is None:
        return None
    try:
        host, url = _target(probe, board)
    except SlugRejectedError:
        return None
    query = urlencode(dict(probe.params))
    paths = [urlsplit(url).path + (f"?{query}" if query else "")]
    if board.platform is Platform.WORKDAY and board.workday_site:
        paths.append(f"/{board.workday_site.strip()}/")
    return host, tuple(paths)


@dataclass(frozen=True, slots=True)
class Robots:
    rules: Mapping[str, RobotsRules] = field(default_factory=dict)

    def refusal(self, board: Board) -> str:
        host, paths = _robots_target(board) or ("", ())
        rules = self.rules.get(host)
        caseless = board.platform is Platform.WORKDAY
        blocked = next(
            (path for path in paths if rules and not rules.allows(path, caseless=caseless)), ""
        )
        return f"{host}/robots.txt does not allow reading {blocked}" if blocked else ""

    def screened(self, row: Company, today: date | None = None) -> Company:
        host, _ = _robots_target(row) or ("", ())
        rules = self.rules.get(host)
        delay = 0.0 if rules is None else rules.crawl_delay or 0.0
        if delay < PATIENT_DELAY_S:
            return row
        line = (
            f"{today or datetime.now(UTC).date()}: {host}/robots.txt asks for {delay:g} "
            "seconds between requests, so Stage reads it with rate_profile patient"
        )
        return replace(
            row, rate_profile="patient", notes="\n".join(filter(None, (row.notes, line)))
        )

    def screen(
        self, rows: Iterable[Company], today: date | None = None
    ) -> tuple[list[Company], list[tuple[Company, str]]]:
        kept: list[Company] = []
        skipped: list[tuple[Company, str]] = []
        for row in rows:
            reason = self.refusal(row)
            if reason:
                skipped.append((row, reason))
            else:
                kept.append(self.screened(row, today))
        return kept, skipped


NO_ROBOTS = Robots()


def _read_by_owner_decision(host: str) -> bool:
    return any(host == site or host.endswith(f".{site}") for site in READ_BY_OWNER_DECISION)


async def read_robots(
    boards: Iterable[Board],
    *,
    client_factory: ClientFactory | None = None,
    timeout: float = ROBOTS_TIMEOUT_S,
    limits: StoredLimits | None = None,
) -> Robots:
    hosts = sorted(
        {
            target[0]
            for board in boards
            if (target := _robots_target(board)) and not _read_by_owner_decision(target[0])
        }
    )
    if not hosts:
        return NO_ROBOTS
    gate = asyncio.Semaphore(ROBOTS_AT_ONCE)
    paced = limits or StoredLimits()
    build = _client_for(client_factory, paced, {host: platform_bucket(host) for host in hosts})
    heard = _Heard()
    async with build(frozenset(hosts), resolve(DISCOVERY_PROFILE, [])) as client:

        async def read(host: str) -> tuple[str, str]:
            async with gate:
                try:
                    async with asyncio.timeout(timeout):
                        text = (await client.get_text(f"https://{host}/robots.txt")).text
                except Exception as exc:
                    heard.note(exc)
                    return host, ""
                heard.note(None)
                return host, text

        found = await asyncio.gather(*(read(host) for host in hosts))
        paced.keep(client, heard)
    return Robots({host: robots_rules(text, ROBOTS_AGENT) for host, text in found if text})


VERIFY_STRIKE = "switched off by stage discover --verify"
_VERIFY_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}: (switched off by stage discover --verify, |board is named )"
)


@dataclass(frozen=True, slots=True)
class VerifyReport:
    verified: int = 0
    disabled: int = 0
    held: tuple[str, ...] = ()


def _note_lines(company: Company) -> list[str]:
    return (company.notes or "").splitlines()


def switched_off_by_verify(company: Company) -> bool:
    lines = _note_lines(company)
    return not company.enabled and bool(lines) and bool(_VERIFY_LINE.match(lines[-1]))


def apply_verification(
    companies: Sequence[Company], results: Sequence[ProbeResult], today: date
) -> tuple[tuple[Company, ...], VerifyReport]:
    answers = {(result.company, result.candidate.label): result for result in results}
    updated: list[Company] = []
    verified = disabled = 0
    held: list[str] = []
    for company in companies:
        result = answers.get((company.name, candidate_for(company).label))
        if result is None:
            updated.append(company)
        elif result.verdict is ProbeVerdict.MATCH and company.enabled:
            if company.last_verified != today:
                verified += 1
            updated.append(replace(company, last_verified=today))
        elif result.verdict is ProbeVerdict.MATCH and switched_off_by_verify(company):
            verified += 1
            kept = [line for line in _note_lines(company) if not _VERIFY_LINE.match(line)]
            updated.append(
                replace(company, enabled=True, last_verified=today, notes="\n".join(kept) or None)
            )
        elif result.verdict is ProbeVerdict.MATCH:
            held.append(company.name)
            updated.append(company)
        elif is_gone(result) and company.enabled:
            disabled += 1
            line = f"{today.isoformat()}: {VERIFY_STRIKE}, {gone_reason(result)}"
            updated.append(
                replace(
                    company,
                    enabled=False,
                    last_verified=None,
                    notes="\n".join([*_note_lines(company), line]),
                )
            )
        else:
            updated.append(company)
    return tuple(updated), VerifyReport(verified, disabled, tuple(held))


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
    review: tuple[ReviewCandidate, ...] = ()

    @property
    def postings(self) -> int:
        return sum(row.job_count for row in self.adopted)


def slug_is_distinctive(company: str, slug: str) -> bool:
    bare = slug.replace("-", "").replace("_", "")
    return len(bare) > GENERIC_SLUG_MAX and name_matches(company, slug)


def adoption_refusal(result: ProbeResult) -> str:
    if result.verdict is not ProbeVerdict.MATCH:
        return f"only a self-naming board is adopted, and this one is {result.verdict.value}"
    if (result.job_count or 0) < 1:
        return "the board has no postings"
    return ""


def needs_review(result: ProbeResult) -> bool:
    return result.verdict is ProbeVerdict.UNVERIFIED and (result.job_count or 0) > 0


def _adopted_note(today: date, result: ProbeResult) -> str:
    if result.verdict is not ProbeVerdict.MATCH:
        return (
            f"{today}: board token from this employer's own apply URL, not a guessed slug. "
            f"{result.candidate.platform.value} publishes no board name, so that link is the "
            f"evidence ({counted(result.job_count or 0, 'job')})"
        )
    return (
        f"{today}: seen in feed postings but missing from the registry. stage discover matched "
        f"{result.candidate.platform.value}/{result.candidate.slug} with "
        f"{counted(result.job_count or 0, 'job')} and the board named itself, so Stage reads "
        "it directly"
    )


def adopt_unregistered(
    existing: Sequence[Company],
    results: Sequence[tuple[str, ProbeResult]],
    *,
    today: date,
    adopt_unnamed: bool = False,
    robots: Robots = NO_ROBOTS,
) -> AdoptionReport:
    keys = {(row.platform, row.slug.lower()) for row in existing}
    captions = {row.name.casefold() for row in existing}
    adopted: list[AdoptedRow] = []
    refused: list[tuple[str, str, str]] = []
    review: dict[tuple[Platform, str], ReviewCandidate] = {}
    known = 0

    for company, result in results:
        key = (result.candidate.platform, result.candidate.slug.lower())
        reason = adoption_refusal(result)
        if reason and not (adopt_unnamed and needs_review(result)):
            if result.verdict is ProbeVerdict.MATCH:
                refused.append((company, result.candidate.label, reason))
            elif needs_review(result) and key not in keys:
                review.setdefault(
                    key,
                    ReviewCandidate(
                        company=company,
                        platform=result.candidate.platform,
                        slug=result.candidate.slug,
                        job_count=result.job_count or 0,
                        distinctive=slug_is_distinctive(company, result.candidate.slug),
                    ),
                )
            continue
        if key in keys or company.casefold() in captions:
            known += 1
            continue
        blocked = robots.refusal(result.candidate)
        if blocked:
            refused.append((company, result.candidate.label, blocked))
            continue
        keys.add(key)
        captions.add(company.casefold())
        row = replace(
            to_company(company, result.candidate, verified_on=today),
            enabled=True,
            name_gate_exempt=result.verdict is not ProbeVerdict.MATCH,
            notes=_adopted_note(today, result),
        )
        adopted.append(
            AdoptedRow(company=robots.screened(row, today), job_count=result.job_count or 0)
        )
    return AdoptionReport(
        adopted=tuple(adopted),
        refused=tuple(refused),
        already_known=known,
        probed=len({company for company, _ in results}),
        review=tuple(review.values()),
    )


async def plan_adoption(
    existing: Sequence[Company],
    results: Sequence[tuple[str, ProbeResult]],
    *,
    today: date,
    adopt_unnamed: bool = False,
    check_robots: bool = False,
    client_factory: ClientFactory | None = None,
    limits: StoredLimits | None = None,
) -> tuple[AdoptionReport, Robots]:
    outcome = adopt_unregistered(existing, results, today=today, adopt_unnamed=adopt_unnamed)
    if not check_robots or not outcome.adopted:
        return outcome, NO_ROBOTS
    robots = await read_robots(
        (row.company for row in outcome.adopted), client_factory=client_factory, limits=limits
    )
    screened = adopt_unregistered(
        existing, results, today=today, adopt_unnamed=adopt_unnamed, robots=robots
    )
    return screened, robots
