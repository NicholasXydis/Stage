import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from stage.domain import RateState
from stage.http import (
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
    HostBudget,
    HttpClient,
    HttpStatusError,
    RatePosture,
    RetryableStatusError,
)
from stage.http.client import MAX_INTERVAL_S

ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
HOSTS = frozenset({"boards-api.greenhouse.io"})
UNPACED = RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=50)


def _client(posture: RatePosture = UNPACED) -> HttpClient:
    return HttpClient(allowed_hosts=HOSTS, posture=posture, jitter=False)


@respx.mock
async def test_a_transient_failure_is_retried_then_succeeds() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"jobs": [1]}),
        ]
    )

    async with _client() as client:
        response = await client.get_json(ENDPOINT)

        assert response.payload == {"jobs": [1]}
        assert client.retry_count == 1
        assert client.request_count == 2

    assert route.call_count == 2


@respx.mock
async def test_a_client_error_is_not_retried() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(404))

    async with _client() as client:
        with pytest.raises(HttpStatusError) as raised:
            await client.get_json(ENDPOINT)
        assert raised.value.status == 404
        assert client.retry_count == 0

    assert route.call_count == 1


@respx.mock
async def test_retries_are_bounded_and_consume_budget() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(503))

    async with _client() as client:
        with pytest.raises(RetryableStatusError):
            await client.get_json(ENDPOINT)

        assert client.request_count == 3

    assert route.call_count == 3


@respx.mock
async def test_retries_cannot_push_a_host_past_its_ceiling() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(503))
    posture = RatePosture(concurrency=2, min_interval_s=0.0, max_requests_per_run=2)

    async with _client(posture) as client:
        with pytest.raises(Exception, match="ceiling"):
            await client.get_json(ENDPOINT)

    assert route.call_count == 2


@respx.mock
async def test_a_429_tightens_the_host_rate() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )

    async with _client() as client:
        await client.get_json(ENDPOINT)
        assert client.tightening_count == 1


@respx.mock
@pytest.mark.serial
async def test_retry_after_is_honored_over_backoff() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )

    async with _client() as client:
        started = time.perf_counter()
        response = await client.get_json(ENDPOINT)
        elapsed = time.perf_counter() - started

    backoff_floor = 0.5
    assert response.status == 200
    assert elapsed < backoff_floor, (
        f"{elapsed}s means Retry-After: 0 was ignored and backoff ran instead"
    )


def test_the_breaker_opens_after_consecutive_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_s=30.0)

    for _ in range(2):
        breaker.record_failure(now=100.0)
    assert breaker.state(now=100.0) is BreakerState.CLOSED

    breaker.record_failure(now=100.0)
    assert breaker.state(now=100.0) is BreakerState.OPEN
    assert breaker.allows(now=100.0) is False


def test_the_breaker_half_opens_after_the_cooldown_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=10.0)
    breaker.record_failure(now=100.0)

    assert breaker.allows(now=105.0) is False
    assert breaker.state(now=111.0) is BreakerState.HALF_OPEN
    assert breaker.allows(now=111.0) is True
    assert breaker.allows(now=111.0) is False

    breaker.record_success()
    assert breaker.state(now=111.0) is BreakerState.CLOSED
    assert breaker.allows(now=111.0) is True


@respx.mock
async def test_an_open_breaker_stops_requests_to_that_host() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(500))
    posture = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=50)

    async with _client(posture) as client:
        for _ in range(3):
            with pytest.raises((RetryableStatusError, BreakerOpenError)):
                await client.get_json(ENDPOINT)

        with pytest.raises(BreakerOpenError):
            await client.get_json(ENDPOINT)

    assert route.call_count == 5


@respx.mock
async def test_the_breaker_recovers_after_cooldown_within_one_process() -> None:
    from stage.http import BreakerOpenError

    route = respx.get(ENDPOINT)
    route.mock(return_value=httpx.Response(500))
    posture = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=100)

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture, jitter=False
    ) as client:
        budget = client._budget_for(client.bucket_for("boards-api.greenhouse.io"))
        for _ in range(budget.breaker.failure_threshold):
            budget.breaker.record_failure()
        assert budget.breaker.opened_at is not None

        with pytest.raises(BreakerOpenError):
            await client.get_json(ENDPOINT)

        budget.breaker.opened_at = time.monotonic() - budget.breaker.cooldown_s - 1
        route.mock(return_value=httpx.Response(200, json={"jobs": []}))

        response = await client.get_json(ENDPOINT)

    assert response.payload == {"jobs": []}, (
        "the half-open probe must survive the post-pacing recheck"
    )


def test_a_claimed_probe_can_be_handed_back() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.0)
    breaker.record_failure()

    assert breaker.state() is BreakerState.HALF_OPEN
    assert breaker.allows(), "the first caller claims the single probe"
    assert not breaker.allows(), "a second caller must not get one"

    breaker.release_probe()
    assert breaker.allows(), "a reservation that claims the probe and aborts must hand it back"


def test_is_open_does_not_consume_the_probe() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.0)
    breaker.record_failure()

    assert not breaker.is_open(), "half-open is not open; the recheck must let a probe pass"
    assert not breaker.is_open()
    assert breaker.allows(), "is_open must leave the probe unclaimed however often it runs"


def _answered(*latencies: float) -> HostBudget:
    budget = HostBudget(posture=RatePosture(concurrency=1, min_interval_s=1.0))
    for latency in latencies:
        budget.observe(latency)
    return budget


def test_a_site_that_slows_down_widens_the_gap_without_a_rejection() -> None:
    budget = _answered(*[0.4] * 8, *[3.0] * 8)

    assert budget.min_interval_s == 1.5
    assert budget.metrics.tightenings == 1, "the next day's allowance stays cautious"
    assert budget.rejections == 0
    assert budget.settle("x", datetime.now(UTC)).min_interval_override is None


def test_a_consistently_slow_site_is_not_punished() -> None:
    budget = _answered(*[3.0] * 40)

    assert budget.metrics.tightenings == 0
    assert budget.min_interval_s == 1.0


def test_one_huge_answer_does_not_slow_a_bucket() -> None:
    budget = _answered(*[0.4] * 15, 90.0)

    assert budget.metrics.tightenings == 0


def test_a_slowdown_under_two_seconds_is_left_alone() -> None:
    budget = _answered(*[0.1] * 8, *[1.9] * 8)

    assert budget.metrics.tightenings == 0, "nineteen times slower, but still quick"


def test_the_gap_widens_at_most_once_per_eight_answers_up_to_the_cap() -> None:
    budget = _answered(*[0.4] * 8, *[3.0] * 15)
    assert budget.metrics.tightenings == 1

    budget.observe(3.0)
    assert budget.metrics.tightenings == 2
    assert budget.min_interval_s == 2.25

    for _ in range(80):
        budget.observe(3.0)
    assert budget.min_interval_s == MAX_INTERVAL_S


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def perf_counter(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


@respx.mock
async def test_only_full_answers_count_toward_a_slowdown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("stage.http.client.time", clock)
    answers = [(200, 0.2)] * 8 + [(304, 5.0)] * 8 + [(404, 5.0)] * 8 + [(200, 5.0)] * 8

    def answer(request: httpx.Request) -> httpx.Response:
        status, latency = answers.pop(0)
        clock.now += latency
        return httpx.Response(status, json={"jobs": []} if status == 200 else None)

    respx.get(ENDPOINT).mock(side_effect=answer)
    posture = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=100)
    async with HttpClient(allowed_hosts=HOSTS, posture=posture, jitter=False) as client:
        for _ in range(24):
            with contextlib.suppress(HttpStatusError):
                await client.get_json(ENDPOINT)
        assert client.tightening_count == 0, "304s and errors are not answers"

        for _ in range(8):
            await client.get_json(ENDPOINT)
        assert client.tightening_count == 1
        assert not client.rate_state()[0].min_interval_override, "a slowdown is not a rejection"


DEAD = "dow.wd1.myworkdayjobs.com"
NEIGHBOUR = "kla.wd1.myworkdayjobs.com"
ALIVE = "acme.wd3.myworkdayjobs.com"
FRONT = "wd1.myworkdayjobs.com"


def _tenants(rate_state: dict[str, RateState] | None = None) -> HttpClient:
    from stage.sources import platform_front

    hosts = (DEAD, NEIGHBOUR, ALIVE)
    return HttpClient(
        allowed_hosts=frozenset(hosts),
        posture=UNPACED,
        bucket_key="workday",
        jitter=False,
        rate_state=rate_state,
        now=datetime.now(UTC),
        host_fronts={host: platform_front(host) for host in hosts},
    )


def _slowly(answer: httpx.Response | Exception) -> Any:
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return respond


def _routes(dead: httpx.Response | Exception) -> tuple[respx.Route, respx.Route, respx.Route]:
    healthy = httpx.Response(200, json={})
    return (
        respx.get(url__startswith=f"https://{DEAD}/").mock(side_effect=_slowly(dead)),
        respx.get(url__startswith=f"https://{NEIGHBOUR}/").mock(side_effect=_slowly(healthy)),
        respx.get(url__startswith=f"https://{ALIVE}/").mock(side_effect=_slowly(healthy)),
    )


async def _timed_out_datacenter() -> dict[str, RateState]:
    async with _tenants() as client:
        for index in range(2):
            with pytest.raises((httpx.ReadTimeout, BreakerOpenError)):
                await client.get_json(f"https://{DEAD}/{index}")
        with pytest.raises(BreakerOpenError, match=NEIGHBOUR):
            await client.get_json(f"https://{NEIGHBOUR}/after")
        await client.get_json(f"https://{ALIVE}/after")
        return {state.bucket: state for state in client.rate_state()}


@respx.mock
async def test_timeouts_stop_their_datacenter_but_no_other() -> None:
    dead, neighbour, alive = _routes(httpx.ReadTimeout("wd1 is down"))

    settled = await _timed_out_datacenter()

    assert (dead.call_count, neighbour.call_count, alive.call_count) == (5, 0, 1)
    assert settled[FRONT].consecutive_failures == 5
    assert settled[FRONT].blocked_until is None, "the next run sends one probe, not a block"
    assert DEAD not in settled, "a timeout is the datacenter's failure, not the tenant's"
    assert settled["workday"].blocked_until is None


@respx.mock
async def test_status_failures_stay_with_the_tenant_that_sent_them() -> None:
    dead, neighbour, _ = _routes(httpx.Response(503))

    async with _tenants() as client:
        for index in range(2):
            with pytest.raises((RetryableStatusError, BreakerOpenError)):
                await client.get_json(f"https://{DEAD}/{index}")
        with pytest.raises(BreakerOpenError, match=DEAD):
            await client.get_json(f"https://{DEAD}/again")
        await client.get_json(f"https://{NEIGHBOUR}/same-datacenter")
        settled = {state.bucket: state for state in client.rate_state()}

    assert dead.call_count == 5 and neighbour.call_count == 1
    assert settled[DEAD].blocked_until is not None
    assert FRONT not in settled


@pytest.mark.parametrize(("recovered", "sent"), [(False, 1), (True, 3)])
@respx.mock
async def test_after_a_datacenter_trips_the_next_run_sends_one_probe_first(
    recovered: bool, sent: int
) -> None:
    dead, neighbour, _ = _routes(httpx.ReadTimeout("wd1 is down"))
    settled = await _timed_out_datacenter()
    respx.reset()
    if recovered:
        dead.mock(side_effect=_slowly(httpx.Response(200, json={})))

    async with _tenants(settled) as client:
        results = await asyncio.gather(
            client.get_json(f"https://{DEAD}/probe"),
            client.get_json(f"https://{NEIGHBOUR}/one"),
            client.get_json(f"https://{NEIGHBOUR}/two"),
            return_exceptions=True,
        )

    assert dead.call_count + neighbour.call_count == sent
    refused = [result for result in results if isinstance(result, BreakerOpenError)]
    assert len(refused) == 3 - sent, "the rest wait for the probe instead of firing"


@pytest.mark.parametrize(("status", "sent"), [(503, 1), (200, 3)])
@respx.mock
async def test_an_expired_block_reopens_with_a_single_probe(status: int, sent: int) -> None:
    from stage.http import RequestRefusedError

    host = "boards-api.greenhouse.io"
    route = respx.get(ENDPOINT).mock(side_effect=_slowly(httpx.Response(status, json={"jobs": []})))
    expired = RateState(
        bucket=host,
        updated_at=datetime.now(UTC) - timedelta(hours=2),
        blocked_until=datetime.now(UTC) - timedelta(minutes=1),
        consecutive_failures=7,
    )
    async with HttpClient(
        allowed_hosts=HOSTS, posture=UNPACED, jitter=False, rate_state={host: expired}
    ) as client:
        results = await asyncio.gather(
            *(client.get_json(ENDPOINT) for _ in range(3)), return_exceptions=True
        )

    assert route.call_count == sent, "the Ashby block doubled when three probes failed at once"
    assert sum(isinstance(result, RequestRefusedError) for result in results) == 3 - sent


def test_a_workday_source_is_blocked_only_when_every_tenant_is() -> None:
    from stage.services.sync import _active_block

    now = datetime.now(UTC)
    later = now + timedelta(hours=1)
    held = {DEAD: RateState(bucket=DEAD, updated_at=now, blocked_until=later)}
    hosts = {DEAD: "workday", ALIVE: "workday"}

    assert _active_block(held, hosts, now) is None
    both = {**held, ALIVE: RateState(bucket=ALIVE, updated_at=now, blocked_until=later)}
    assert _active_block(both, hosts, now) is not None
    shared = {"workday": RateState(bucket="workday", updated_at=now, blocked_until=later)}
    assert _active_block(shared, hosts, now) == shared["workday"]
