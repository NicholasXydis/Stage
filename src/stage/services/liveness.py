import asyncio
import json
import random
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from stage.domain import (
    DEFAULT_WINDOW_DAYS,
    Job,
    JobStatus,
    Platform,
    RateState,
    RequestLogged,
)
from stage.domain.liveness import (
    CHECKED_SOURCES,
    CHECKS_PER_SYNC,
    RECHECK_AFTER_HOURS,
    Liveness,
    LivenessCheck,
)
from stage.http import (
    BreakerOpenError,
    BucketBlockedError,
    HostBudget,
    HostBudgetExceededError,
    HostNotAllowedError,
    HttpClient,
    HttpError,
    HttpStatusError,
    RatePosture,
    resolve,
)
from stage.sources import get_feeds, platform_front
from stage.sources.greenhouse import HOST as GREENHOUSE_HOST
from stage.sources.greenhouse import GreenhouseAdapter
from stage.sources.lever import HOST as LEVER_HOST
from stage.sources.lever import LeverAdapter
from stage.sources.platforms import (
    SlugRejectedError,
    identify_url,
    safe_cased_slug,
    safe_slug,
    workday_target,
)
from stage.sources.smartrecruiters import HOST as SMARTRECRUITERS_HOST
from stage.sources.smartrecruiters import SmartRecruitersAdapter
from stage.sources.workday import WorkdayAdapter
from stage.storage import AsyncRepository, open_repository

SYNC_CHECK_SECONDS = 60.0
OPEN_CHECK_SECONDS = 5.0
OPEN_WAIT_MS = 3000
URL_MARKS = ("greenhouse.io/", "lever.co/", "smartrecruiters.com/", "myworkdayjobs.com/")
WORKDAY_MARK = "x-stage-workday"
WORKDAY_GONE = frozenset({"S22"})
WORKDAY_ERROR_BYTES = 64 * 1024
PROFILES = {
    Platform.GREENHOUSE: GreenhouseAdapter.rate_profile,
    Platform.LEVER: LeverAdapter.rate_profile,
    Platform.SMARTRECRUITERS: SmartRecruitersAdapter.rate_profile,
    Platform.WORKDAY: WorkdayAdapter.rate_profile,
}
_GREENHOUSE_JOB = re.compile(r"/jobs/([0-9]{1,20})(?:/|$)")
_LEVER_JOB = re.compile(
    r"/(?:v0/postings/)?[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:/apply)?/?",
    re.IGNORECASE,
)
_SMARTRECRUITERS_JOB = re.compile(
    r"/(?:v1/companies/)?[^/]+/(?:postings/)?([0-9]{6,20})(?:-[^/]*)?/?"
)
_WORKDAY_JOB = re.compile(
    r"(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/(?P<site>[A-Za-z0-9_-]{1,64})(?P<path>/job(?:/[^/]+)+?)"
    r"(?:/apply(?:/[^/]*)?)?/?"
)
_WORKDAY_SEGMENT = re.compile(r"[A-Za-z0-9._~%()!*'+,-]{1,300}")
_NUMERIC = re.compile(r"[0-9]{1,20}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class Target:
    platform: Platform
    host: str
    board: str
    url: str
    board_url: str = ""
    board_params: tuple[tuple[str, str], ...] = ()

    @property
    def bucket(self) -> str:
        return WorkdayAdapter.bucket_key if self.platform is Platform.WORKDAY else self.host


@dataclass(frozen=True, slots=True)
class LivenessResult:
    checked: int = 0
    closed: int = 0
    unknown: int = 0
    requests: tuple[RequestLogged, ...] = ()
    gone: tuple[Job, ...] = ()


@dataclass(frozen=True, slots=True)
class Probe:
    checks: tuple[LivenessCheck, ...] = ()
    requests: tuple[RequestLogged, ...] = ()
    pushback: tuple[RateState, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


def _greenhouse(slug: str, native: str) -> Target | None:
    try:
        board = safe_slug(slug)
    except SlugRejectedError:
        return None
    if not _NUMERIC.fullmatch(native):
        return None
    base = f"https://{GREENHOUSE_HOST}/v1/boards/{board}"
    return Target(Platform.GREENHOUSE, GREENHOUSE_HOST, board, f"{base}/jobs/{native}", base)


def _lever(slug: str, native: str) -> Target | None:
    try:
        board = safe_cased_slug(slug)
    except SlugRejectedError:
        return None
    if not _UUID.fullmatch(native.lower()):
        return None
    base = f"https://{LEVER_HOST}/v0/postings/{board}"
    return Target(
        Platform.LEVER,
        LEVER_HOST,
        board,
        f"{base}/{native.lower()}",
        base,
        (("mode", "json"), ("limit", "1")),
    )


def _smartrecruiters(slug: str, native: str) -> Target | None:
    try:
        board = safe_slug(slug)
    except SlugRejectedError:
        return None
    if not _NUMERIC.fullmatch(native):
        return None
    base = f"https://{SMARTRECRUITERS_HOST}/v1/companies/{board}/postings"
    return Target(
        Platform.SMARTRECRUITERS,
        SMARTRECRUITERS_HOST,
        board,
        f"{base}/{native}",
        base,
        (("limit", "1"),),
    )


def _workday(tenant: str, dc: str, path: str) -> Target | None:
    matched = _WORKDAY_JOB.fullmatch(path)
    if matched is None:
        return None
    site, tail = matched.group("site"), matched.group("path")
    segments = tail.split("/")[1:]
    if any(
        segment in (".", "..") or not _WORKDAY_SEGMENT.fullmatch(segment) for segment in segments
    ):
        return None
    try:
        host, _ = workday_target(tenant, site, dc)
    except SlugRejectedError:
        return None
    return Target(
        Platform.WORKDAY,
        host,
        f"{tenant}/{site}",
        f"https://{host}/wday/cxs/{safe_slug(tenant)}/{site}{tail}",
    )


def listed_target(url: str) -> Target | None:
    candidate = identify_url(url)
    if candidate is None:
        return None
    try:
        parts = urlsplit(url if "//" in url else f"https://{url}")
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    match candidate.platform:
        case Platform.GREENHOUSE:
            found = _GREENHOUSE_JOB.search(path)
            ids = parse_qs(parts.query).get("gh_jid") or []
            native = found.group(1) if found is not None else (ids[0] if ids else "")
            return _greenhouse(candidate.slug, native)
        case Platform.LEVER:
            lever = _LEVER_JOB.fullmatch(path)
            if host == "jobs.eu.lever.co" or lever is None:
                return None
            return _lever(candidate.slug, lever.group(1))
        case Platform.SMARTRECRUITERS:
            posting = _SMARTRECRUITERS_JOB.fullmatch(path)
            return None if posting is None else _smartrecruiters(candidate.slug, posting.group(1))
        case Platform.WORKDAY:
            return _workday(candidate.workday_tenant or "", candidate.workday_dc or "", path)
    return None


def _board_target(job: Job) -> Target | None:
    parts = job.id.split(":")
    if len(parts) != 3:
        return None
    _, slug, native = parts
    match job.source:
        case "greenhouse":
            return _greenhouse(slug, native)
        case "lever":
            listed = listed_target(job.apply_url_raw)
            if listed is not None and listed.url.endswith(f"/{native}"):
                return listed
            return _lever(slug, native)
        case "smartrecruiters":
            return _smartrecruiters(slug, native)
        case "workday":
            listed = listed_target(job.apply_url_raw)
            return listed if listed is not None and listed.platform is Platform.WORKDAY else None
    return None


def target_for(job: Job, feeds: Collection[str]) -> Target | None:
    if job.source in CHECKED_SOURCES:
        return _board_target(job)
    if job.source in feeds:
        return listed_target(job.apply_url_raw)
    return None


def _choose(jobs: Sequence[Job], feeds: Collection[str], limit: int) -> list[tuple[Job, Target]]:
    chosen: list[tuple[Job, Target]] = []
    for job in jobs:
        if len(chosen) >= limit:
            break
        target = target_for(job, feeds)
        if target is not None:
            chosen.append((job, target))
    return chosen


def _workday_code(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(payload, dict) or payload.get("httpStatus") != 403:
        return ""
    code = payload.get("errorCode")
    return code if isinstance(code, str) else ""


class WorkdayAnswers(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if response.status_code != 403:
            return response
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > WORKDAY_ERROR_BYTES:
                    break
        finally:
            await response.aclose()
        code = _workday_code(bytes(body))
        if code in WORKDAY_GONE:
            return httpx.Response(410, headers={WORKDAY_MARK: code}, request=request)
        kept = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(403, headers=kept, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _listed(target: Target, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    match target.platform:
        case Platform.GREENHOUSE:
            return isinstance(payload.get("id"), int) and isinstance(payload.get("title"), str)
        case Platform.LEVER:
            return isinstance(payload.get("id"), str) and isinstance(payload.get("text"), str)
        case Platform.SMARTRECRUITERS:
            return isinstance(payload.get("id"), str) and payload.get("active") is not False
        case Platform.WORKDAY:
            info = payload.get("jobPostingInfo")
            return isinstance(info, dict) and info.get("posted") is not False
    return False


def _board_answers(target: Target, payload: Any) -> bool | None:
    match target.platform:
        case Platform.GREENHOUSE:
            return True if isinstance(payload, dict) and "name" in payload else None
        case Platform.LEVER:
            return True if isinstance(payload, list) else None
        case Platform.SMARTRECRUITERS:
            total = payload.get("totalFound") if isinstance(payload, dict) else None
            return True if isinstance(total, int) and total > 0 else None
    return None


class _Boards:
    def __init__(self) -> None:
        self._known: dict[str, bool | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def alive(self, client: HttpClient, target: Target) -> bool | None:
        key = f"{target.platform.value}:{target.board}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key not in self._known:
                self._known[key] = await _board_alive(client, target)
            return self._known[key]


async def _board_alive(client: HttpClient, target: Target) -> bool | None:
    try:
        response = await client.get_json(
            target.board_url, params=dict(target.board_params) or None, revalidate=True
        )
    except HttpStatusError as exc:
        return False if exc.status == 404 else None
    except (HttpError, httpx.HTTPError):
        return None
    return _board_answers(target, response.payload)


_UNSENT = (BucketBlockedError, BreakerOpenError, HostBudgetExceededError, HostNotAllowedError)


async def _check(client: HttpClient, target: Target, boards: _Boards) -> tuple[Liveness, bool]:
    try:
        response = await client.get_json(target.url, revalidate=True)
    except HttpStatusError as exc:
        if target.platform is Platform.WORKDAY:
            gone = exc.status == 410 and exc.response.headers.get(WORKDAY_MARK) in WORKDAY_GONE
            return (Liveness.GONE if gone else Liveness.UNKNOWN), True
        if exc.status != 404:
            return Liveness.UNKNOWN, True
        alive = await boards.alive(client, target)
        return (Liveness.GONE if alive else Liveness.UNKNOWN), True
    except _UNSENT:
        return Liveness.UNKNOWN, False
    except HttpError:
        return Liveness.UNKNOWN, True
    except httpx.HTTPError:
        return Liveness.UNKNOWN, False
    return (Liveness.OPEN if _listed(target, response.payload) else Liveness.UNKNOWN), True


def _client(
    target: Target,
    rate_state: Mapping[str, RateState],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    hosts: frozenset[str],
    now: datetime,
    rng: random.Random | None,
) -> HttpClient:
    workday = target.platform is Platform.WORKDAY
    return HttpClient(
        allowed_hosts=hosts,
        posture=postures.get(target.bucket) or resolve(PROFILES[target.platform], []),
        transport=WorkdayAnswers() if workday else None,
        rng=rng,
        bucket_key=WorkdayAdapter.bucket_key if workday else "",
        rate_state=rate_state,
        now=now,
        budgets=budgets,
        postures=postures,
        host_fronts={host: platform_front(host) for host in hosts},
    )


def _pushback(state: RateState) -> bool:
    return bool(
        state.blocked_until is not None
        or state.min_interval_override is not None
        or state.consecutive_failures
    )


async def _probe(
    chosen: Sequence[tuple[Job, Target]],
    *,
    rate_state: Mapping[str, RateState],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    now: datetime,
    time_limit_s: float,
    rng: random.Random | None = None,
    clock: Callable[[], datetime] = _now,
) -> Probe:
    if not chosen:
        return Probe()
    asked: dict[str, list[str]] = {}
    targets: dict[str, Target] = {}
    for job, target in chosen:
        asked.setdefault(target.url, []).append(job.id)
        targets[target.url] = target
    hosts: dict[str, set[str]] = {}
    first: dict[str, Target] = {}
    for target in targets.values():
        hosts.setdefault(target.bucket, set()).add(target.host)
        first.setdefault(target.bucket, target)
    if set(first) - set(postures):
        known = await asyncio.to_thread(open_postures)
        if known is None:
            return Probe()
        postures = {**known, **postures}
    before = {bucket: budget.requests for bucket, budget in budgets.items()}
    clients = {
        bucket: _client(target, rate_state, budgets, postures, frozenset(hosts[bucket]), now, rng)
        for bucket, target in first.items()
    }
    boards = _Boards()

    async def run(target: Target) -> tuple[str, Liveness, bool, datetime]:
        verdict, answered = await _check(clients[target.bucket], target, boards)
        return target.url, verdict, answered, clock()

    tasks = [asyncio.create_task(run(target)) for target in targets.values()]
    checks: list[LivenessCheck] = []
    requests: list[RequestLogged] = []
    pushback: list[RateState] = []
    try:
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, time_limit_s))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in tasks:
            if task in done and task.exception() is None:
                url, verdict, answered, moment = task.result()
                if answered:
                    checks.extend(LivenessCheck(ident, verdict, moment) for ident in asked[url])
        settled_at = clock()
        for bucket, client in clients.items():
            source = first[bucket].platform.value
            requests.extend(client.drain_requests(source))
            spent = budgets[bucket].requests if bucket in budgets else 0
            asked_more = spent > before.get(bucket, 0)
            pushback.extend(
                state
                for state in client.rate_state(settled_at)
                if asked_more
                and state.bucket in {bucket, *hosts[bucket], *map(platform_front, hosts[bucket])}
                and _pushback(state)
            )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client in clients.values():
            await client.aclose()
    return Probe(tuple(checks), tuple(requests), tuple(pushback))


def _result(
    chosen: Sequence[tuple[Job, Target]], probe: Probe, closed: Sequence[str] | None
) -> LivenessResult:
    jobs = {job.id: job for job, _ in chosen}
    settled = sum(1 for check in probe.checks if check.verdict is not Liveness.UNKNOWN)
    return LivenessResult(
        checked=len(chosen),
        closed=len(closed or ()),
        unknown=len(chosen) - settled,
        requests=probe.requests,
        gone=tuple(jobs[check.id] for check in probe.checks if check.verdict is Liveness.GONE),
    )


def _gave_up(chosen: Sequence[tuple[Job, Target]], probe: Probe) -> LivenessResult:
    return LivenessResult(checked=len(chosen), unknown=len(chosen), requests=probe.requests)


async def check_still_open(
    repository: AsyncRepository,
    *,
    rate_state: Mapping[str, RateState],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    now: datetime,
    limit: int = CHECKS_PER_SYNC,
    time_limit_s: float = SYNC_CHECK_SECONDS,
    rng: random.Random | None = None,
    clock: Callable[[], datetime] = _now,
) -> LivenessResult:
    feeds = frozenset(get_feeds())
    candidates = await repository.liveness_queue(
        now - timedelta(days=DEFAULT_WINDOW_DAYS),
        now - timedelta(hours=RECHECK_AFTER_HOURS),
        feeds,
        CHECKED_SOURCES,
        URL_MARKS,
    )
    chosen = _choose(candidates, feeds, limit)
    probe = await _probe(
        chosen,
        rate_state=rate_state,
        budgets=budgets,
        postures=postures,
        now=now,
        time_limit_s=time_limit_s,
        rng=rng,
        clock=clock,
    )
    if not probe.checks and not probe.pushback:
        return _result(chosen, probe, ())
    try:
        closed = await repository.record_checks(probe.checks, probe.pushback)
    except sqlite3.OperationalError:
        return _gave_up(chosen, probe)
    return _result(chosen, probe, closed)


def open_postures() -> dict[str, RatePosture] | None:
    from stage.companies import RegistryError, load_companies
    from stage.services.sync import _bucket_postures, _group_by_adapter

    try:
        companies = load_companies()
    except (RegistryError, OSError):
        return None
    grouped, _ = _group_by_adapter(companies)
    return _bucket_postures(grouped, get_feeds())


async def _opened(
    database: Path, job_ids: Sequence[str]
) -> tuple[list[Job], Mapping[str, RateState]]:
    async with open_repository(database, read_only=True) as repository:
        found = [await repository.get_job(ident) for ident in dict.fromkeys(job_ids)]
        jobs = [job for job in found if job is not None and job.status is JobStatus.OPEN]
        return jobs, (await repository.load_rate_state()) if jobs else {}


async def check_opened(
    database: Path,
    job_ids: Sequence[str],
    *,
    record: bool = True,
    time_limit_s: float = OPEN_CHECK_SECONDS,
    clock: Callable[[], datetime] = _now,
) -> LivenessResult:
    if not job_ids or not database.exists():
        return LivenessResult()
    from stage.storage.migrations import SchemaVersionError

    try:
        jobs, rate_state = await _opened(database, job_ids)
    except (sqlite3.Error, OSError, SchemaVersionError):
        return LivenessResult()
    chosen = _choose(jobs, frozenset(get_feeds()), len(jobs))
    if not chosen:
        return LivenessResult()
    probe = await _probe(
        chosen,
        rate_state=rate_state,
        budgets={},
        postures={},
        now=clock(),
        time_limit_s=time_limit_s,
        clock=clock,
    )
    if not record:
        return _result(chosen, probe, None)
    if not probe.checks and not probe.pushback:
        return _result(chosen, probe, ())
    try:
        async with open_repository(database) as repository:
            closed = await repository.record_checks(
                probe.checks, probe.pushback, wait_ms=OPEN_WAIT_MS
            )
    except (sqlite3.Error, OSError):
        return _gave_up(chosen, probe)
    return _result(chosen, probe, closed)


def gone_line(job: Job) -> str:
    return f"{job.company} no longer lists {job.title_raw}, so Stage marked it closed."


def gone_notice(gone: Sequence[Job]) -> str:
    if len(gone) == 1:
        return gone_line(gone[0])
    return (
        f"{len(gone):,} of the postings you opened are no longer listed by their employers, "
        "so Stage marked them closed."
    )


__all__ = [
    "OPEN_CHECK_SECONDS",
    "SYNC_CHECK_SECONDS",
    "LivenessResult",
    "Probe",
    "Target",
    "WorkdayAnswers",
    "check_opened",
    "check_still_open",
    "gone_line",
    "gone_notice",
    "listed_target",
    "open_postures",
    "target_for",
]
