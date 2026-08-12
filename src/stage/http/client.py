import asyncio
import json
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
from tenacity import AsyncRetrying, RetryCallState, stop_after_attempt, wait_exponential_jitter

from stage.domain import RateState, block_duration, decay
from stage.http.breaker import CircuitBreaker
from stage.http.cache import ValidatorCache
from stage.http.profiles import RatePosture

USER_AGENT = "stage-cli/0.1.0 (+https://github.com/NicholasXydis/stage; internship aggregator)"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_RETRY_AFTER_S = 60.0
MAX_INTERVAL_S = 10.0
MAX_ATTEMPTS = 3
JITTER_MAX_S = 0.5
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
BLOCKING_STATUSES = frozenset({401, 403})
BLOCKED_COOLDOWN_S = 1800.0
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def request_url(url: str, params: dict[str, str] | None = None) -> httpx.URL:
    target = httpx.URL(url)
    return target.copy_merge_params(params) if params else target


class HttpError(Exception):
    pass


class HttpStatusError(HttpError):
    def __init__(self, bucket: str, response: httpx.Response) -> None:
        super().__init__(f"{bucket} returned {response.status_code}")
        self.response = response
        self.status = response.status_code


class HostNotAllowedError(HttpError):
    pass


class HostBudgetExceededError(HttpError):
    pass


class ResponseTooLargeError(HttpError):
    pass


class BreakerOpenError(HttpError):
    pass


class ForbiddenError(HttpError):
    pass


class RedirectNotAllowedError(HttpError):
    pass


class BucketBlockedError(HttpError):
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


@dataclass(slots=True)
class HostBudget:
    posture: RatePosture
    seed: RateState | None = None
    metrics: HostMetrics = field(default_factory=HostMetrics)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    requests: int = 0
    min_interval_s: float = 0.0
    next_allowed_at: float = 0.0
    fast_latency: float = 0.0
    slow_latency: float = 0.0
    samples: int = 0
    last_tightened_at: int = 0
    rejections: int = 0
    seeded_interval_s: float = 0.0
    last_error: str = ""
    deferred_s: float = 0.0
    deferred_reason: str = ""
    semaphore: asyncio.Semaphore = field(init=False)
    gate: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.posture.concurrency)
        self.gate = asyncio.Lock()
        self.min_interval_s = self.posture.min_interval_s
        if self.seed is not None:
            if self.seed.min_interval_override is not None:
                self.min_interval_s = max(self.min_interval_s, self.seed.min_interval_override)
            self.breaker.consecutive_failures = self.seed.consecutive_failures
        self.seeded_interval_s = self.min_interval_s

    @property
    def stride(self) -> float:
        return self.min_interval_s / max(1, self.posture.concurrency)

    def defer_for(self, seconds: float, reason: str) -> None:
        if seconds > self.deferred_s:
            self.deferred_s = seconds
            self.deferred_reason = reason

    def tighten(self, factor: float, *, rejected: bool = False) -> None:
        self.min_interval_s = min(MAX_INTERVAL_S, self.min_interval_s * factor)
        self.metrics.tightenings += 1
        self.last_tightened_at = self.requests
        if rejected:
            self.rejections += 1

    def observe_latency(self, time_to_headers_s: float) -> None:
        sample = time_to_headers_s
        self.samples += 1
        self.fast_latency = sample if self.samples == 1 else 0.6 * sample + 0.4 * self.fast_latency
        self.slow_latency = (
            sample if self.samples == 1 else 0.15 * sample + 0.85 * self.slow_latency
        )
        if (
            self.samples >= 8
            and self.requests - self.last_tightened_at >= 5
            and self.fast_latency > self.slow_latency * 1.75
        ):
            self.tighten(1.5)

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
        escalated = (
            block_duration(self.breaker.consecutive_failures)
            if self.breaker.opened_at is not None
            else 0.0
        )
        if self.deferred_s > 0 or escalated > 0:
            longest = max(self.deferred_s, escalated)
            reason = (
                self.deferred_reason
                if self.deferred_s >= escalated
                else (self.last_error or "circuit breaker tripped")
            )
            return dataclass_replace(
                state,
                blocked_until=now + timedelta(seconds=longest),
                consecutive_failures=self.breaker.consecutive_failures,
                last_failure_at=now,
                reason=reason,
            )
        if self.breaker.consecutive_failures:
            return dataclass_replace(
                state,
                consecutive_failures=self.breaker.consecutive_failures,
                last_failure_at=now,
                reason=self.last_error,
            )
        return state


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
    ) -> None:
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._posture = posture or RatePosture()
        self._cache = cache or ValidatorCache()
        self._budgets: dict[str, HostBudget] = {} if budgets is None else budgets
        self._postures = dict(postures or {})
        self._touched: set[str] = set()
        self._tighten_baseline: dict[str, int] = {}
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
        self._client = httpx.AsyncClient(
            timeout=timeout or DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
            transport=transport,
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

    def bucket_for(self, host: str) -> str:
        return self._bucket_key or host

    def _budget_for(self, bucket: str) -> HostBudget:
        budget = self._budgets.get(bucket)
        if budget is None:
            posture = self._postures.get(bucket, self._posture)
            budget = HostBudget(posture=posture, seed=self._rate_state.get(bucket))
            if self._jitter and budget.min_interval_s > 0:
                budget.next_allowed_at = time.monotonic() + self._rng.uniform(0.0, JITTER_MAX_S)
            self._budgets[bucket] = budget
        self._tighten_baseline.setdefault(bucket, budget.metrics.tightenings)
        self._touched.add(bucket)
        return budget

    def _authorize(self, url: str) -> tuple[str, HostBudget]:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if host not in self._allowed_hosts:
            raise HostNotAllowedError(f"{host or url!r} is not a registry host")
        bucket = self.bucket_for(host)
        return bucket, self._budget_for(bucket)

    def rate_state(self, now: datetime | None = None) -> tuple[RateState, ...]:
        moment = now or datetime.now(UTC)
        return tuple(
            budget.settle(bucket, moment)
            for bucket, budget in sorted(self._budgets.items())
            if bucket in self._touched and budget.requests > 0
        )

    def _refuse(
        self, bucket: str, budget: HostBudget, *, count: bool, claim_probe: bool = True
    ) -> None:
        blocking = self._blocked.get(bucket)
        if blocking is not None and blocking.is_blocked(self._now):
            raise BucketBlockedError(
                f"{bucket} is blocked for another "
                f"{blocking.blocks_remaining_s(self._now):.0f}s ({blocking.reason})"
            )
        barred = not budget.breaker.allows() if claim_probe else budget.breaker.is_open()
        if barred:
            raise BreakerOpenError(
                f"{bucket} circuit breaker is open after "
                f"{budget.breaker.consecutive_failures} consecutive failures"
            )
        if count and budget.requests >= budget.posture.max_requests_per_run:
            raise HostBudgetExceededError(
                f"{bucket} reached its ceiling of {budget.posture.max_requests_per_run} "
                "requests for this run"
            )

    async def _reserve(self, bucket: str, budget: HostBudget) -> None:
        async with budget.gate:
            self._refuse(bucket, budget, count=True)
            budget.requests += 1
            now = time.monotonic()
            wait = budget.next_allowed_at - now
            budget.next_allowed_at = max(now, budget.next_allowed_at) + budget.stride
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with budget.gate:
                self._refuse(bucket, budget, count=False, claim_probe=False)
        except HttpError:
            async with budget.gate:
                budget.requests -= 1
                budget.breaker.release_probe()
            raise

    @staticmethod
    def _retry_after_raw(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
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

    def _validate_hop(self, url: httpx.URL) -> None:
        if url.scheme != "https":
            raise RedirectNotAllowedError(
                f"redirect to {url} downgrades to {url.scheme}; TLS is never optional (§11)"
            )
        if (url.host or "").lower() not in self._allowed_hosts:
            raise RedirectNotAllowedError(
                f"redirect to {url.host!r} leaves the registry allow-list — "
                "adapters may only contact hosts the registry names (§11)"
            )

    async def _send(
        self, target: httpx.URL, headers: dict[str, str], method: str, body: Any
    ) -> httpx.Response:
        current = target
        for _ in range(MAX_REDIRECTS + 1):
            request = self._client.build_request(
                method, current, headers=headers, json=body if method == "POST" else None
            )
            response = await self._client.send(request, stream=True)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            await response.aclose()
            current = current.join(location)
            self._validate_hop(current)
            method = "GET" if response.status_code in (301, 302, 303) else method
        raise RedirectNotAllowedError(f"{target} exceeded {MAX_REDIRECTS} redirects")

    async def _read_capped(self, bucket: str, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                announced = int(declared)
            except ValueError:
                announced = -1
            if announced > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"{bucket} announced {announced} bytes, over the "
                    f"{MAX_RESPONSE_BYTES}-byte ceiling"
                )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"{bucket} exceeded the {MAX_RESPONSE_BYTES}-byte ceiling mid-transfer"
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
    ) -> JsonResponse:
        if attempt > 1:
            budget.metrics.retries += 1
            self._own.retries += 1
        target = request_url(url, params)
        key = str(target)
        headers = self._cache.conditional_headers(key) if method == "GET" and not revalidate else {}
        headers_at = 0.0
        try:
            async with budget.semaphore:
                await self._reserve(bucket, budget)
                self._own.requests += 1
                started = time.perf_counter()
                response = await self._send(target, headers, method, body)
                headers_at = time.perf_counter()
                try:
                    content = await self._read_capped(bucket, response)
                finally:
                    await response.aclose()
        except ResponseTooLargeError as exc:
            self._log.append(
                RequestRecord(
                    method=method,
                    url=key,
                    status=None,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            budget.metrics.failures += 1
            budget.breaker.record_failure()
            budget.last_error = f"{type(exc).__name__}: {exc}"
            self._log.append(
                RequestRecord(
                    method=method,
                    url=key,
                    status=None,
                    elapsed_ms=elapsed,
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise

        elapsed_s = time.perf_counter() - started
        time_to_headers_s = headers_at - started
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
            budget.metrics.failures += 1
            budget.breaker.record_failure()
            budget.tighten(2.0, rejected=True)
            reason = f"HTTP {response.status_code}"
            budget.last_error = reason
            budget.defer_for(BLOCKED_COOLDOWN_S, reason)
            raise ForbiddenError(f"{bucket} returned {response.status_code}")

        if response.status_code in RETRYABLE_STATUSES:
            budget.metrics.failures += 1
            budget.breaker.record_failure()
            raw_retry_after = self._retry_after_raw(response)
            if response.status_code == 429 or raw_retry_after is not None:
                budget.tighten(2.0, rejected=True)
            if raw_retry_after is not None and raw_retry_after > MAX_RETRY_AFTER_S:
                budget.defer_for(raw_retry_after, f"Retry-After: {raw_retry_after:.0f}s")
            budget.last_error = f"HTTP {response.status_code}"
            raise RetryableStatusError(
                f"{bucket} returned {response.status_code}",
                response.status_code,
                min(raw_retry_after, MAX_RETRY_AFTER_S) if raw_retry_after is not None else None,
            )

        if response.status_code == 304:
            budget.breaker.record_success()
            budget.observe_latency(time_to_headers_s)
            budget.metrics.not_modified += 1
            self._own.not_modified += 1
            return JsonResponse(status=304, payload=None, not_modified=True)

        if response.is_error:
            budget.metrics.failures += 1
            budget.last_error = f"HTTP {response.status_code}"
            raise HttpStatusError(bucket, response)
        budget.breaker.record_success()
        budget.observe_latency(time_to_headers_s)

        payload = json.loads(content)
        if method == "GET":
            self._cache.record(key, response.headers, datetime.now(UTC))
        return JsonResponse(status=response.status_code, payload=payload, not_modified=False)

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        revalidate: bool = False,
    ) -> JsonResponse:
        return await self._request("GET", url, params=params, revalidate=revalidate)

    async def post_json(self, url: str, *, body: Any) -> JsonResponse:
        return await self._request("POST", url, body=body)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        revalidate: bool = False,
    ) -> JsonResponse:
        bucket, budget = self._authorize(url)

        def wait(state: RetryCallState) -> float:
            outcome = state.outcome
            exc = outcome.exception() if outcome is not None else None
            if isinstance(exc, RetryableStatusError) and exc.retry_after is not None:
                return exc.retry_after
            return float(wait_exponential_jitter(initial=0.5, max=8.0)(state))

        def should_retry(state: RetryCallState) -> bool:
            outcome = state.outcome
            if outcome is None or not outcome.failed:
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
                    bucket, budget, url, params, attempt, method, body, revalidate
                )
        raise HttpError(f"{bucket} exhausted retries for {url}")
