import asyncio
import hashlib
import json
import random
import socket
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from statistics import median
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
from tenacity import AsyncRetrying, RetryCallState, stop_after_attempt, wait_exponential_jitter

from stage import __version__
from stage.domain import RateState, RequestLogged, block_duration, decay
from stage.domain.text import counted, plain_error
from stage.http.breaker import BreakerState, CircuitBreaker
from stage.http.cache import ValidatorCache
from stage.http.profiles import RatePosture

USER_AGENT = (
    f"stage-cli/{__version__} (+https://github.com/NicholasXydis/Stage; internship aggregator)"
)
TEXT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
MEGABYTE = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * MEGABYTE
SIGN_IN = "Sign in to the network, then run stage sync again."
MAX_RETRY_AFTER_S = 60.0
MAX_DEFERRAL_S = 86400.0
RESET_AS_EPOCH_S = 1_000_000.0
RESET_AS_EPOCH_MS = 100_000_000_000.0
MAX_INTERVAL_S = 10.0
MIN_TIGHTEN_FLOOR_S = 0.25
SLOW_WINDOW = 8
SLOW_RATIO = 2.0
SLOW_FLOOR_S = 2.0
SLOW_WIDEN = 1.5
MAX_ATTEMPTS = 3
JITTER_MAX_S = 0.5
STRIDE_JITTER_FRACTION = 0.35
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
BLOCKING_STATUSES = frozenset({401, 403})
BLOCKED_COOLDOWN_S = 1800.0
DENIED_HOSTS_BEFORE_BLOCK = 2
TLS12_ONLY_HOSTS = frozenset({"www.jobbank.gc.ca"})
MAX_REDIRECTS = 3
NETWORK_LOGIN_STATUS = 511
PORTAL_PAGE_BYTES = 256 * 1024
PORTAL_WITNESSES = 2
ORIGIN_BOUND_HEADERS = frozenset(
    {"authorization", "cookie", "origin", "referer", "proxy-authorization", "x-csrf-token"}
)
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
FRONT_COOLDOWN_S = 600.0
PROBE_WAIT_S = 0.25
BREAKER_PAUSED = "circuit breaker paused"
TRIPPED = f"Stage's {BREAKER_PAUSED} it after repeated failures"
STATUS_WORDS = {
    400: "bad request",
    401: "login required",
    403: "forbidden",
    404: "not found",
    405: "method not allowed",
    408: "request timeout",
    410: "gone",
    422: "request rejected",
    429: "too many requests",
    500: "server error",
    502: "bad gateway",
    503: "unavailable",
    504: "gateway timeout",
}
UNEXPECTED = (
    AssertionError,
    AttributeError,
    LookupError,
    NameError,
    NotImplementedError,
    RecursionError,
    TypeError,
    ZeroDivisionError,
)
NAME_LOOKUP_FAILURES = (
    "nodename nor servname",
    "name or service not known",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "no address associated",
)


def request_url(url: str, params: dict[str, str] | None = None) -> httpx.URL:
    target = httpx.URL(url)
    return target.copy_merge_params(params) if params else target


def is_disconnected(exc: BaseException) -> bool:
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            return False
        if isinstance(
            current,
            httpx.ConnectError
            | httpx.ConnectTimeout
            | httpx.ProxyError
            | socket.gaierror
            | CaptivePortalError,
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class HttpError(Exception):
    pass


def skipped_by_breaker(error: str) -> bool:
    return BREAKER_PAUSED in error or "BreakerOpenError" in error


def returned(host: str, status: int) -> str:
    words = STATUS_WORDS.get(status)
    return f"{host} returned {status} ({words})" if words else f"{host} returned {status}"


def _span(seconds: float) -> str:
    if seconds < 90:
        return counted(round(seconds), "second")
    if seconds < 3570:
        return counted(round(seconds / 60), "minute")
    hours = round(seconds / 3600, 1)
    return "1 hour" if hours == 1 else f"{hours:g} hours"


def _answered_by(response: httpx.Response, fallback: str) -> str:
    try:
        return response.request.url.host or fallback
    except RuntimeError:
        return fallback


class HttpStatusError(HttpError):
    def __init__(self, bucket: str, response: httpx.Response) -> None:
        super().__init__(returned(_answered_by(response, bucket), response.status_code))
        self.response = response
        self.status = response.status_code


class HostNotAllowedError(HttpError):
    pass


class RequestRefusedError(HttpError):
    pass


class HostBudgetExceededError(RequestRefusedError):
    pass


class UnreadablePayloadError(HttpError, ValueError):
    pass


class ResponseTooLargeError(HttpError):
    pass


class BreakerOpenError(RequestRefusedError):
    pass


class ForbiddenError(HttpError):
    pass


class RedirectNotAllowedError(HttpError):
    pass


class BucketBlockedError(RequestRefusedError):
    pass


class CaptivePortalError(HttpError):
    pass


class RetryableStatusError(HttpError):
    def __init__(self, message: str, status: int, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class JsonResponse:
    status: int
    payload: Any
    not_modified: bool
    size: int = 0


@dataclass(frozen=True, slots=True)
class TextResponse:
    status: int
    text: str
    not_modified: bool


@dataclass(frozen=True, slots=True)
class RequestRecord:
    method: str
    url: str
    status: int | None
    elapsed_ms: float
    attempt: int
    error: str = ""


@dataclass(slots=True)
class HostMetrics:
    requests: int = 0
    not_modified: int = 0
    retries: int = 0
    tightenings: int = 0
    failures: int = 0
    latencies: list[float] = field(default_factory=list)

    def percentile(self, fraction: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[index]


def resume(breaker: CircuitBreaker, seed: RateState | None) -> CircuitBreaker:
    if seed is None or not seed.consecutive_failures:
        return breaker
    breaker.consecutive_failures = seed.consecutive_failures
    if seed.blocked_until is not None or seed.consecutive_failures >= breaker.failure_threshold:
        breaker.opened_at = time.monotonic() - breaker.cooldown_s
    return breaker


@dataclass(slots=True)
class HostHealth:
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    last_error: str = ""
    blocks: bool = True

    def settle(self, host: str, now: datetime) -> RateState:
        failures = self.breaker.consecutive_failures
        if self.breaker.opened_at is not None and self.blocks:
            return RateState(
                bucket=host,
                updated_at=now,
                blocked_until=now + timedelta(seconds=block_duration(failures)),
                consecutive_failures=failures,
                last_failure_at=now,
                reason=self.last_error or TRIPPED,
            )
        if failures:
            return RateState(
                bucket=host,
                updated_at=now,
                consecutive_failures=failures,
                last_failure_at=now,
                reason=self.last_error,
            )
        return RateState(bucket=host, updated_at=now)


@dataclass(slots=True)
class HostBudget:
    posture: RatePosture
    seed: RateState | None = None
    metrics: HostMetrics = field(default_factory=HostMetrics)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    requests: int = 0
    min_interval_s: float = 0.0
    next_allowed_at: float = 0.0
    rejections: int = 0
    seeded_interval_s: float = 0.0
    last_error: str = ""
    deferred_s: float = 0.0
    deferred_reason: str = ""
    denied_hosts: set[str] = field(default_factory=set)
    answered: int = 0
    slowed_at: int = 0
    first_answers: list[float] = field(default_factory=list)
    recent_answers: deque[float] = field(default_factory=lambda: deque(maxlen=SLOW_WINDOW))
    hosts: dict[str, HostHealth] = field(default_factory=dict)
    own_breaker_used: bool = False
    semaphore: asyncio.Semaphore = field(init=False)
    gate: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.posture.concurrency)
        self.gate = asyncio.Lock()
        self.min_interval_s = self.posture.min_interval_s
        if self.seed is not None:
            if self.seed.min_interval_override is not None:
                self.min_interval_s = max(self.min_interval_s, self.seed.min_interval_override)
            resume(self.breaker, self.seed)
        self.seeded_interval_s = self.min_interval_s

    @property
    def stride(self) -> float:
        return self.min_interval_s / max(1, self.posture.concurrency)

    def defer_for(self, seconds: float, reason: str) -> None:
        capped = min(seconds, MAX_DEFERRAL_S)
        if capped > self.deferred_s:
            self.deferred_s = capped
            self.deferred_reason = reason

    def tighten(self, factor: float, *, rejected: bool = False) -> None:
        established = max(self.min_interval_s, MIN_TIGHTEN_FLOOR_S)
        self.min_interval_s = min(MAX_INTERVAL_S, established * factor)
        self.metrics.tightenings += 1
        if rejected:
            self.rejections += 1

    def observe(self, latency_s: float) -> None:
        self.answered += 1
        if len(self.first_answers) < SLOW_WINDOW:
            self.first_answers.append(latency_s)
            return
        self.recent_answers.append(latency_s)
        if len(self.recent_answers) < SLOW_WINDOW or self.answered - self.slowed_at < SLOW_WINDOW:
            return
        recent = median(self.recent_answers)
        if recent > SLOW_FLOOR_S and recent > SLOW_RATIO * median(self.first_answers):
            self.tighten(SLOW_WIDEN)
            self.slowed_at = self.answered

    def settle(self, bucket: str, now: datetime) -> RateState:
        baseline = self.posture.min_interval_s
        override = (
            self.min_interval_s if self.rejections else decay(self.seeded_interval_s, baseline)
        )
        if override is not None and override <= baseline:
            override = None

        state = RateState(
            bucket=bucket,
            updated_at=now,
            min_interval_override=override,
            rotation_cursor=self.seed.rotation_cursor if self.seed is not None else "",
        )
        breaker = self.breaker if self.own_breaker_used or not self.hosts else CircuitBreaker()
        escalated = (
            block_duration(breaker.consecutive_failures) if breaker.opened_at is not None else 0.0
        )
        if self.deferred_s > 0 or escalated > 0:
            longest = max(self.deferred_s, escalated)
            reason = (
                self.deferred_reason
                if self.deferred_s >= escalated
                else (self.last_error or TRIPPED)
            )
            return dataclass_replace(
                state,
                blocked_until=now + timedelta(seconds=longest),
                consecutive_failures=breaker.consecutive_failures,
                last_failure_at=now,
                reason=reason,
            )
        if breaker.consecutive_failures:
            return dataclass_replace(
                state,
                consecutive_failures=breaker.consecutive_failures,
                last_failure_at=now,
                reason=self.last_error,
            )
        return state


def _tls12_context() -> ssl.SSLContext:
    context = httpx.create_ssl_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 8 and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _host_of(exc: httpx.HTTPError) -> str:
    try:
        return exc.request.url.host or "the site"
    except RuntimeError:
        return "the site"


def _connect_text(exc: httpx.HTTPError, host: str) -> str:
    chain = _cause_chain(exc)
    said = " ".join(str(item) for item in chain).lower()
    if any(isinstance(item, socket.gaierror) for item in chain) or any(
        marker in said for marker in NAME_LOOKUP_FAILURES
    ):
        return f"Stage could not look up {host}, so the network may be down"
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain) or (
        "certificate_verify_failed" in said
    ):
        return (
            f"{host} sent a certificate Stage could not verify, "
            "which usually means a Wi-Fi login page"
        )
    if any(isinstance(item, ConnectionRefusedError) for item in chain):
        return f"{host} refused the connection"
    return f"Stage could not connect to {host}"


def transport_text(exc: httpx.HTTPError) -> str:
    host = _host_of(exc)
    if isinstance(exc, httpx.ConnectTimeout):
        return f"{host} did not accept a connection in time"
    if isinstance(exc, httpx.ReadTimeout):
        return f"{host} took too long to answer"
    if isinstance(exc, httpx.TimeoutException):
        return f"the request to {host} timed out"
    if isinstance(exc, httpx.ProxyError):
        return f"the proxy refused the request to {host}"
    if isinstance(exc, httpx.ConnectError):
        return _connect_text(exc, host)
    if isinstance(exc, httpx.RemoteProtocolError):
        return f"{host} broke off its answer"
    if isinstance(exc, httpx.ReadError | httpx.WriteError):
        return f"the connection to {host} dropped"
    detail = plain_error(": ".join(_described(exc)))
    return f"the request to {host} failed ({detail})"


def failure_text(exc: BaseException) -> str:
    if isinstance(exc, HttpError):
        return str(exc)
    if isinstance(exc, httpx.HTTPError):
        return transport_text(exc)
    detail = plain_error(": ".join(_described(exc)))
    if isinstance(exc, UNEXPECTED):
        return f"Stage hit an unexpected problem ({detail})"
    return detail


def _described(exc: BaseException) -> list[str]:
    detail = str(exc)
    seen = {id(exc)}
    cause = exc.__cause__ or exc.__context__
    while not detail and cause is not None and id(cause) not in seen:
        detail = str(cause)
        seen.add(id(cause))
        if detail:
            return [type(exc).__name__, type(cause).__name__, detail]
        cause = cause.__cause__ or cause.__context__
    return [type(exc).__name__, detail]


def _origin_of(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, (url.host or "").lower(), url.port)


def _site(host: str) -> str:
    return ".".join(host.lower().rsplit(".", 2)[-2:])


class HttpClient:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        posture: RatePosture | None = None,
        cache: ValidatorCache | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
        jitter: bool = True,
        bucket_key: str = "",
        rate_state: Mapping[str, RateState] | None = None,
        now: datetime | None = None,
        budgets: dict[str, HostBudget] | None = None,
        postures: Mapping[str, RatePosture] | None = None,
        host_buckets: Mapping[str, str] | None = None,
        host_fronts: Mapping[str, str] | None = None,
    ) -> None:
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._host_buckets = {host.lower(): bucket for host, bucket in (host_buckets or {}).items()}
        self._host_fronts = {host.lower(): front for host, front in (host_fronts or {}).items()}
        self._touched_hosts: set[str] = set()
        self._posture = posture or RatePosture()
        self._cache = cache or ValidatorCache()
        self._budgets: dict[str, HostBudget] = {} if budgets is None else budgets
        self._postures = dict(postures or {})
        self._touched: set[str] = set()
        self._tighten_baseline: dict[str, int] = {}
        self._portal: dict[str, set[str]] = {}
        self._own = HostMetrics()
        self._log: list[RequestRecord] = []
        self._rng = rng or random.Random()
        self._jitter = jitter
        self._bucket_key = bucket_key
        self._rate_state = dict(rate_state or {})
        self._now = now or datetime.now(UTC)
        self._blocked = {
            bucket: state
            for bucket, state in self._rate_state.items()
            if state.is_blocked(self._now)
        }
        mounts: dict[str, httpx.AsyncBaseTransport] | None = None
        if transport is None:
            capped = self._allowed_hosts & TLS12_ONLY_HOSTS
            mounts = {
                f"https://{host}": httpx.AsyncHTTPTransport(verify=_tls12_context())
                for host in sorted(capped)
            } or None
        self._client = httpx.AsyncClient(
            timeout=timeout or DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
            transport=transport,
            mounts=mounts or {},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cache(self) -> ValidatorCache:
        return self._cache

    @property
    def request_count(self) -> int:
        return self._own.requests

    @property
    def not_modified_count(self) -> int:
        return self._own.not_modified

    @property
    def retry_count(self) -> int:
        return self._own.retries

    @property
    def tightening_count(self) -> int:
        return sum(
            self._budgets[bucket].metrics.tightenings - baseline
            for bucket, baseline in self._tighten_baseline.items()
        )

    def latency_percentiles(self) -> tuple[float, float]:
        if not self._own.latencies:
            return 0.0, 0.0
        return self._own.percentile(0.5), self._own.percentile(0.95)

    def drain_log(self) -> list[RequestRecord]:
        drained = self._log
        self._log = []
        return drained

    def drain_requests(self, source: str) -> list[RequestLogged]:
        return [
            RequestLogged(
                source=source,
                method=record.method,
                url=record.url,
                status=record.status,
                elapsed_ms=record.elapsed_ms,
                attempt=record.attempt,
                error=record.error,
            )
            for record in self.drain_log()
        ]

    def bucket_for(self, host: str) -> str:
        return self._bucket_key or self._host_buckets.get(host, host)

    def _budget_for(self, bucket: str) -> HostBudget:
        budget = self._budgets.get(bucket)
        if budget is None:
            posture = self._postures.get(bucket, self._posture)
            budget = HostBudget(posture=posture, seed=self._rate_state.get(bucket))
            if self._jitter and budget.min_interval_s > 0:
                budget.next_allowed_at = time.monotonic() + self._rng.uniform(0.0, JITTER_MAX_S)
            self._budgets[bucket] = budget
        elif bucket not in self._touched:
            claimed = self._postures.get(bucket)
            ceiling = budget.posture.max_requests_per_run
            if claimed is not None and claimed.max_requests_per_run < ceiling:
                budget.posture = dataclass_replace(
                    budget.posture, max_requests_per_run=claimed.max_requests_per_run
                )
        self._tighten_baseline.setdefault(bucket, budget.metrics.tightenings)
        self._touched.add(bucket)
        return budget

    def _health(self, budget: HostBudget, name: str, *, front: bool = False) -> HostHealth:
        health = budget.hosts.get(name)
        if health is None:
            cooldown = FRONT_COOLDOWN_S if front else CircuitBreaker().cooldown_s
            breaker = resume(CircuitBreaker(cooldown_s=cooldown), self._rate_state.get(name))
            health = HostHealth(breaker=breaker, blocks=not front)
            budget.hosts[name] = health
        return health

    def _heard(self, bucket: str, budget: HostBudget, host: str) -> CircuitBreaker:
        if host != bucket:
            self._touched_hosts.add(host)
        return self._breaker(bucket, budget, host)

    def _front(self, host: str) -> str:
        return self._host_fronts.get(host, host)

    def _breaker(self, bucket: str, budget: HostBudget, host: str) -> CircuitBreaker:
        if host == bucket:
            budget.own_breaker_used = True
            return budget.breaker
        return self._health(budget, host).breaker

    def _breakers(self, bucket: str, budget: HostBudget, host: str) -> list[CircuitBreaker]:
        breakers = [self._breaker(bucket, budget, host)]
        front = self._front(host)
        if front != host:
            breakers.append(self._health(budget, front, front=True).breaker)
        return breakers

    def _fail(
        self, bucket: str, budget: HostBudget, host: str, reason: str, *, transport: bool = False
    ) -> None:
        budget.metrics.failures += 1
        budget.last_error = reason
        front = self._front(host)
        if transport and front != host:
            health = self._health(budget, front, front=True)
            self._touched_hosts.add(front)
            health.breaker.record_failure()
            health.last_error = reason
            return
        self._heard(bucket, budget, host).record_failure()
        if host != bucket:
            self._health(budget, host).last_error = reason

    def _answered(self, budget: HostBudget, host: str) -> None:
        front = self._front(host)
        if front != host:
            self._touched_hosts.add(front)
            self._health(budget, front, front=True).breaker.record_success()

    def _probe_out(self, bucket: str, budget: HostBudget, host: str) -> bool:
        return any(
            breaker.state() is BreakerState.HALF_OPEN and breaker.probing
            for breaker in self._breakers(bucket, budget, host)
        )

    async def _await_probe(self, bucket: str, budget: HostBudget, host: str) -> None:
        while self._probe_out(bucket, budget, host):
            await asyncio.sleep(PROBE_WAIT_S)

    def _authorize(self, url: str) -> tuple[str, HostBudget, str]:
        try:
            split = urlsplit(url)
        except ValueError:
            raise HostNotAllowedError(f"{url} is not a web address Stage can read") from None
        host = (split.hostname or "").lower()
        if host not in self._allowed_hosts:
            raise HostNotAllowedError(
                f"{host or url} is not a host the registry names, so Stage did not contact it"
            )
        if split.scheme != "https":
            raise RedirectNotAllowedError(f"{url} is not https, and Stage only reads https")
        bucket = self.bucket_for(host)
        return bucket, self._budget_for(bucket), host

    def rate_state(self, now: datetime | None = None) -> tuple[RateState, ...]:
        moment = now or datetime.now(UTC)
        states: list[RateState] = []
        for bucket, budget in sorted(self._budgets.items()):
            if bucket not in self._touched or budget.requests <= 0:
                continue
            states.append(budget.settle(bucket, moment))
            states.extend(
                health.settle(host, moment)
                for host, health in sorted(budget.hosts.items())
                if host in self._touched_hosts
                and (health.breaker.consecutive_failures or host in self._rate_state)
            )
        return tuple(states)

    def _refuse(
        self,
        bucket: str,
        budget: HostBudget,
        host: str,
        *,
        count: bool,
        claim_probe: bool = True,
    ) -> list[CircuitBreaker]:
        for name in dict.fromkeys((bucket, self._front(host), host)):
            blocking = self._blocked.get(name)
            if blocking is not None and blocking.is_blocked(self._now):
                raise BucketBlockedError(
                    f"{name} is blocked for another "
                    f"{_span(blocking.blocks_remaining_s(self._now))}"
                    + (f": {plain_error(blocking.reason)}" if blocking.reason else "")
                )
        if budget.deferred_s > 0:
            raise BucketBlockedError(
                f"{bucket} is blocked for {_span(budget.deferred_s)}: {budget.deferred_reason}"
            )
        claimed: list[CircuitBreaker] = []
        for breaker in self._breakers(bucket, budget, host):
            probe = claim_probe and breaker.state() is BreakerState.HALF_OPEN
            if (claim_probe and not breaker.allows()) or (not claim_probe and breaker.is_open()):
                for held in claimed:
                    held.release_probe()
                raise BreakerOpenError(
                    f"Stage's {BREAKER_PAUSED} {host} after "
                    f"{counted(breaker.consecutive_failures, 'failure')} in a row"
                )
            if probe:
                claimed.append(breaker)
        if count and budget.requests >= budget.posture.max_requests_per_run:
            for held in claimed:
                held.release_probe()
            raise HostBudgetExceededError(
                f"Stage reached its ceiling of "
                f"{counted(budget.posture.max_requests_per_run, 'request')} to {bucket} "
                "for this run"
            )
        return claimed

    def _paced(self, budget: HostBudget) -> float:
        stride = budget.stride
        if not self._jitter or stride <= 0:
            return stride
        return stride + self._rng.uniform(0.0, stride * STRIDE_JITTER_FRACTION)

    async def _reserve(
        self, bucket: str, budget: HostBudget, host: str, *, first: bool = True
    ) -> list[CircuitBreaker]:
        while True:
            async with budget.gate:
                pending = first and self._probe_out(bucket, budget, host)
                if not pending:
                    claimed = self._refuse(bucket, budget, host, count=True, claim_probe=first)
                    budget.requests += 1
                    break
            await asyncio.sleep(PROBE_WAIT_S)
        try:
            while True:
                async with budget.gate:
                    self._refuse(bucket, budget, host, count=False, claim_probe=False)
                    now = time.monotonic()
                    wait = budget.next_allowed_at - now
                    if wait <= 0:
                        budget.next_allowed_at = now + self._paced(budget)
                        return claimed
                await asyncio.sleep(wait)
        except HttpError:
            async with budget.gate:
                budget.requests -= 1
                for held in claimed:
                    held.release_probe()
            raise

    def _hold_off(self, budget: HostBudget, seconds: float) -> None:
        budget.next_allowed_at = max(budget.next_allowed_at, time.monotonic() + seconds)

    @staticmethod
    def _reset_seconds(value: float, now: float) -> float | None:
        if value < RESET_AS_EPOCH_S:
            return value
        if value < RESET_AS_EPOCH_MS:
            return value - now
        if value < RESET_AS_EPOCH_MS * 1000:
            return value / 1000 - now
        return None

    @staticmethod
    def _rate_limit_reset(response: httpx.Response) -> float | None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if reset is None or remaining is None or remaining.strip() != "0":
            return None
        try:
            value = float(reset)
        except ValueError:
            return None
        if value < 0:
            return None
        seconds = HttpClient._reset_seconds(value, datetime.now(UTC).timestamp())
        if seconds is None or seconds <= 0:
            return None
        return seconds

    @staticmethod
    def _retry_after_raw(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return HttpClient._rate_limit_reset(response)
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())

    def _portal_seen(self, signature: str, witness: str) -> bool:
        witnesses = self._portal.setdefault(signature, set())
        witnesses.add(witness)
        return len(witnesses) >= PORTAL_WITNESSES

    def _portal_page(self, content: bytes, host: str, *, record: bool) -> None:
        if len(content) > PORTAL_PAGE_BYTES or not content.strip():
            return
        signature = f"page {hashlib.sha256(content).hexdigest()}"
        if (record or signature in self._portal) and self._portal_seen(signature, _site(host)):
            raise CaptivePortalError(
                f"{host} answered with the same page as another site, "
                f"which usually means a Wi-Fi login page. {SIGN_IN}"
            )

    def _validate_hop(self, origin: httpx.URL, url: httpx.URL) -> None:
        if url.scheme != "https":
            raise RedirectNotAllowedError(
                f"{origin.host} redirected to {url}, a downgrade from https, so Stage stopped"
            )
        if (url.host or "").lower() not in self._allowed_hosts:
            raise RedirectNotAllowedError(
                f"{origin.host} redirected to {url.host}, a host the registry does not name, "
                "so Stage stopped"
            )

    async def _send(
        self,
        target: httpx.URL,
        headers: dict[str, str],
        method: str,
        body: Any,
        hop: Callable[[], Awaitable[None]] | None = None,
    ) -> httpx.Response:
        current = target
        carried = headers
        for step in range(MAX_REDIRECTS + 1):
            if step and hop is not None:
                await hop()
            request = self._client.build_request(
                method, current, headers=carried, json=body if method == "POST" else None
            )
            response = await self._client.send(request, stream=True)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            await response.aclose()
            following = current.join(location)
            try:
                self._validate_hop(current, following)
            except RedirectNotAllowedError as exc:
                destination = (following.host or "").lower()
                elsewhere = _site(destination) != _site(target.host or "")
                if (
                    elsewhere
                    and destination not in self._allowed_hosts
                    and self._portal_seen(f"redirect {destination}", str(target))
                ):
                    raise CaptivePortalError(
                        f"{target.host} redirected to {destination} as another board did, "
                        f"which usually means a Wi-Fi login page. {SIGN_IN}"
                    ) from exc
                raise
            if _origin_of(following) != _origin_of(current):
                carried = {
                    name: value
                    for name, value in carried.items()
                    if name.lower() not in ORIGIN_BOUND_HEADERS
                }
            current = following
            method = "GET" if response.status_code in (301, 302, 303) else method
        raise RedirectNotAllowedError(
            f"{target.host} redirected more than {MAX_REDIRECTS} times, so Stage stopped"
        )

    async def _read_capped(self, bucket: str, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                announced = int(declared)
            except ValueError:
                announced = -1
            if announced > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"{_answered_by(response, bucket)} announced {announced / MEGABYTE:.0f} MB, "
                    f"over Stage's {MAX_RESPONSE_BYTES // MEGABYTE} MB limit"
                )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"{_answered_by(response, bucket)} sent more than Stage's "
                    f"{MAX_RESPONSE_BYTES // MEGABYTE} MB limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def _attempt(
        self,
        bucket: str,
        budget: HostBudget,
        url: str,
        params: dict[str, str] | None,
        attempt: int,
        method: str = "GET",
        body: Any = None,
        revalidate: bool = False,
        decode: str = "json",
        extra_headers: Mapping[str, str] | None = None,
    ) -> JsonResponse:
        claimed: list[CircuitBreaker] = []
        try:
            return await self._exchange(
                claimed,
                bucket,
                budget,
                url,
                params,
                attempt,
                method,
                body,
                revalidate,
                decode,
                extra_headers,
            )
        finally:
            for breaker in claimed:
                breaker.release_probe()

    async def _exchange(
        self,
        claimed: list[CircuitBreaker],
        bucket: str,
        budget: HostBudget,
        url: str,
        params: dict[str, str] | None,
        attempt: int,
        method: str,
        body: Any,
        revalidate: bool,
        decode: str,
        extra_headers: Mapping[str, str] | None,
    ) -> JsonResponse:
        if attempt > 1:
            budget.metrics.retries += 1
            self._own.retries += 1
        target = request_url(url, params)
        key = str(target)
        host = (target.host or bucket).lower()
        headers = self._cache.conditional_headers(key) if method == "GET" and not revalidate else {}
        if extra_headers:
            headers = {**headers, **dict(extra_headers)}

        async def hop() -> None:
            await self._reserve(bucket, budget, host, first=False)
            self._own.requests += 1

        started = time.perf_counter()
        try:
            await self._await_probe(bucket, budget, host)
            async with budget.semaphore:
                claimed.extend(await self._reserve(bucket, budget, host))
                self._own.requests += 1
                started = time.perf_counter()
                response = await self._send(target, headers, method, body, hop)
                try:
                    content = await self._read_capped(bucket, response)
                finally:
                    await response.aclose()
        except (ResponseTooLargeError, RedirectNotAllowedError, CaptivePortalError) as exc:
            self._log.append(
                RequestRecord(
                    method=method,
                    url=key,
                    status=None,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    attempt=attempt,
                    error=": ".join(_described(exc)),
                )
            )
            raise
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            self._fail(
                bucket,
                budget,
                host,
                transport_text(exc),
                transport=isinstance(exc, httpx.TransportError),
            )
            self._log.append(
                RequestRecord(
                    method=method,
                    url=key,
                    status=None,
                    elapsed_ms=elapsed,
                    attempt=attempt,
                    error=": ".join(_described(exc)),
                )
            )
            raise

        elapsed_s = time.perf_counter() - started
        self._answered(budget, host)
        budget.metrics.latencies.append(elapsed_s * 1000)
        self._own.latencies.append(elapsed_s * 1000)
        self._log.append(
            RequestRecord(
                method=method,
                url=key,
                status=response.status_code,
                elapsed_ms=elapsed_s * 1000,
                attempt=attempt,
            )
        )

        if response.status_code in BLOCKING_STATUSES:
            reason = returned(host, response.status_code)
            self._fail(bucket, budget, host, reason)
            budget.denied_hosts.add(host)
            spans_hosts = len(self._allowed_hosts) > 1
            if not spans_hosts or len(budget.denied_hosts) >= DENIED_HOSTS_BEFORE_BLOCK:
                budget.tighten(2.0, rejected=True)
                budget.defer_for(BLOCKED_COOLDOWN_S, reason)
            raise ForbiddenError(reason)

        if response.status_code in RETRYABLE_STATUSES:
            reason = returned(host, response.status_code)
            self._fail(bucket, budget, host, reason)
            raw_retry_after = self._retry_after_raw(response)
            if response.status_code == 429 or raw_retry_after is not None:
                budget.tighten(2.0, rejected=True)
            if raw_retry_after is not None and raw_retry_after > MAX_RETRY_AFTER_S:
                budget.defer_for(
                    raw_retry_after, f"{host} asked Stage to wait {_span(raw_retry_after)}"
                )
            elif raw_retry_after is not None:
                self._hold_off(budget, raw_retry_after)
            raise RetryableStatusError(
                reason,
                response.status_code,
                min(raw_retry_after, MAX_RETRY_AFTER_S) if raw_retry_after is not None else None,
            )

        if response.status_code == NETWORK_LOGIN_STATUS:
            raise CaptivePortalError(
                f"{host} answered 511, so the network wants a login first. {SIGN_IN}"
            )

        if response.status_code == 304:
            self._heard(bucket, budget, host).record_success()
            budget.metrics.not_modified += 1
            self._own.not_modified += 1
            return JsonResponse(status=304, payload=None, not_modified=True)

        if response.is_error:
            budget.metrics.failures += 1
            budget.last_error = returned(host, response.status_code)
            raise HttpStatusError(bucket, response)
        try:
            payload = (
                content.decode("utf-8", "replace") if decode == "text" else json.loads(content)
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            unreadable = (
                f"{host} answered {response.status_code} with a page Stage could not read as "
                f"{'text' if decode == 'text' else 'JSON'}, usually a bot check or an error page"
            )
            self._fail(bucket, budget, host, unreadable)
            self._portal_page(content, host, record=True)
            raise UnreadablePayloadError(unreadable) from exc
        if decode == "text" and self._portal:
            self._portal_page(content, host, record=False)
        self._heard(bucket, budget, host).record_success()
        budget.observe(elapsed_s)
        if method == "GET":
            self._cache.record(key, response.headers, datetime.now(UTC))
        return JsonResponse(
            status=response.status_code, payload=payload, not_modified=False, size=len(content)
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        revalidate: bool = False,
    ) -> JsonResponse:
        return await self._request("GET", url, params=params, revalidate=revalidate)

    async def post_json(
        self, url: str, *, body: Any, extra_headers: Mapping[str, str] | None = None
    ) -> JsonResponse:
        return await self._request("POST", url, body=body, extra_headers=extra_headers)

    async def get_text(self, url: str, *, revalidate: bool = False) -> TextResponse:
        response = await self._request(
            "GET",
            url,
            decode="text",
            revalidate=revalidate,
            extra_headers={"Accept": TEXT_ACCEPT},
        )
        return TextResponse(
            status=response.status,
            text="" if response.payload is None else str(response.payload),
            not_modified=response.not_modified,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        revalidate: bool = False,
        decode: str = "json",
        extra_headers: Mapping[str, str] | None = None,
    ) -> JsonResponse:
        bucket, budget, host = self._authorize(url)

        def wait(state: RetryCallState) -> float:
            outcome = state.outcome
            exc = outcome.exception() if outcome is not None else None
            if isinstance(exc, RetryableStatusError) and exc.retry_after is not None:
                return exc.retry_after
            return float(wait_exponential_jitter(multiplier=0.5, max=8.0)(state))

        def should_retry(state: RetryCallState) -> bool:
            outcome = state.outcome
            if outcome is None or not outcome.failed:
                return False
            breakers = self._breakers(bucket, budget, host)
            if budget.deferred_s > 0 or any(breaker.is_open() for breaker in breakers):
                return False
            return isinstance(outcome.exception(), RetryableStatusError | httpx.TransportError)

        attempt = 0
        async for wrapped in AsyncRetrying(
            retry=should_retry,
            wait=wait,
            stop=stop_after_attempt(MAX_ATTEMPTS),
            reraise=True,
        ):
            with wrapped:
                attempt += 1
                return await self._attempt(
                    bucket,
                    budget,
                    url,
                    params,
                    attempt,
                    method,
                    body,
                    revalidate,
                    decode,
                    extra_headers,
                )
        raise HttpError(f"{host} kept failing after {MAX_ATTEMPTS} tries")
