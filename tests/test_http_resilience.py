import time

import httpx
import pytest
import respx

from stage.http import BreakerOpenError, BreakerState, CircuitBreaker, HttpClient, RatePosture
from stage.http.client import RetryableStatusError

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
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json(ENDPOINT)
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
