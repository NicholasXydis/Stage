import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from stage.http import HostBudgetExceededError, HttpClient, RatePosture
from stage.http.client import USER_AGENT

ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


@respx.mock
async def test_a_runaway_caller_cannot_exceed_the_ceiling() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    posture = RatePosture(concurrency=2, min_interval_s=0.0, max_requests_per_run=3)
    refused = 0

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture
    ) as client:
        for _ in range(10):
            try:
                await client.get_json(ENDPOINT)
            except HostBudgetExceededError:
                refused += 1

        assert client.request_count == 3

    assert route.call_count == 3
    assert refused == 7


@respx.mock
async def test_the_ceiling_holds_when_requests_are_concurrent() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    posture = RatePosture(concurrency=8, min_interval_s=0.0, max_requests_per_run=3)

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture
    ) as client:
        outcomes = await asyncio.gather(
            *(client.get_json(ENDPOINT) for _ in range(20)), return_exceptions=True
        )

    refused = [item for item in outcomes if isinstance(item, HostBudgetExceededError)]
    assert route.call_count == 3
    assert len(refused) == 17


@respx.mock
async def test_the_ceiling_is_counted_per_host() -> None:
    other = "https://boards-api.greenhouse.io/v1/boards/other/jobs"
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get(other).mock(return_value=httpx.Response(200, json={"jobs": []}))
    posture = RatePosture(concurrency=2, min_interval_s=0.0, max_requests_per_run=2)

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture
    ) as client:
        await client.get_json(ENDPOINT)
        await client.get_json(other)
        with pytest.raises(HostBudgetExceededError):
            await client.get_json(ENDPOINT)


@respx.mock
async def test_user_agent_is_honest() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    async with HttpClient(allowed_hosts=frozenset({"boards-api.greenhouse.io"})) as client:
        await client.get_json(ENDPOINT)

    assert route.calls[0].request.headers["user-agent"] == USER_AGENT
    assert "stage-cli" in USER_AGENT


@respx.mock
@pytest.mark.serial
async def test_min_interval_is_enforced_per_host() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    posture = RatePosture(concurrency=1, min_interval_s=0.05, max_requests_per_run=10)

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture, jitter=False
    ) as client:
        started = time.perf_counter()
        for _ in range(4):
            await client.get_json(ENDPOINT)
        elapsed = time.perf_counter() - started

    floor = 3 * posture.min_interval_s
    assert elapsed >= floor * 0.9, f"{elapsed} paced faster than the declared {floor}"
    assert elapsed < floor * 4, f"{elapsed} is far slower than the declared {floor}"


async def test_concurrency_is_enforced_not_merely_declared() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={})

    posture = RatePosture(concurrency=3, min_interval_s=0.0, max_requests_per_run=50)
    async with HttpClient(
        allowed_hosts=frozenset({"example.com"}),
        posture=posture,
        transport=httpx.MockTransport(handler),
        jitter=False,
    ) as client:
        await asyncio.gather(*(client.get_json(f"https://example.com/{n}") for n in range(9)))

    assert peak == 3


@pytest.mark.serial
async def test_the_stride_divides_the_interval_by_concurrency() -> None:

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    posture = RatePosture(concurrency=4, min_interval_s=0.4, max_requests_per_run=50)
    async with HttpClient(
        allowed_hosts=frozenset({"example.com"}),
        posture=posture,
        transport=httpx.MockTransport(handler),
        jitter=False,
    ) as client:
        started = time.perf_counter()
        await asyncio.gather(*(client.get_json(f"https://example.com/{n}") for n in range(8)))
        elapsed = time.perf_counter() - started

    assert 0.6 < elapsed < 1.2, elapsed


async def test_the_per_run_ceiling_is_unaffected_by_concurrency() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    posture = RatePosture(concurrency=5, min_interval_s=0.0, max_requests_per_run=4)
    async with HttpClient(
        allowed_hosts=frozenset({"example.com"}),
        posture=posture,
        transport=httpx.MockTransport(handler),
        jitter=False,
    ) as client:
        results = await asyncio.gather(
            *(client.get_json(f"https://example.com/{n}") for n in range(10)),
            return_exceptions=True,
        )

    assert calls == 4
    assert sum(isinstance(r, HostBudgetExceededError) for r in results) == 6


@respx.mock
async def test_an_oversized_body_is_refused_before_it_is_held() -> None:
    from stage.http import ResponseTooLargeError
    from stage.http.client import MAX_RESPONSE_BYTES

    chunks_pulled = 0

    class _Endless(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal chunks_pulled
            block = b"x" * (1024 * 1024)
            while True:
                chunks_pulled += 1
                yield block

    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, stream=_Endless()))

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), jitter=False
    ) as client:
        with pytest.raises(ResponseTooLargeError, match="mid-transfer"):
            await client.get_json(ENDPOINT)

    assert chunks_pulled <= MAX_RESPONSE_BYTES // (1024 * 1024) + 2, (
        "the read must stop at the ceiling rather than draining an endless body"
    )


@respx.mock
async def test_a_declared_content_length_is_refused_before_the_body_arrives() -> None:
    from stage.http import ResponseTooLargeError
    from stage.http.client import MAX_RESPONSE_BYTES

    served = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served = True
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_RESPONSE_BYTES + 1)},
            content=b"{}",
        )

    respx.get(ENDPOINT).mock(side_effect=respond)

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), jitter=False
    ) as client:
        with pytest.raises(ResponseTooLargeError, match="announced"):
            await client.get_json(ENDPOINT)

    assert served


@respx.mock
async def test_a_body_under_the_ceiling_still_parses() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": [{"id": 1}]}))

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), jitter=False
    ) as client:
        response = await client.get_json(ENDPOINT)

    assert response.payload == {"jobs": [{"id": 1}]}


@respx.mock
async def test_one_tightening_is_counted_once_not_once_per_request_in_flight() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    posture = RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=50)
    in_flight = 6

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture, jitter=False
    ) as client:
        budget = client._budget_for("boards-api.greenhouse.io")

        async def tighten_midflight() -> None:
            await asyncio.sleep(0)
            budget.tighten(1.5)

        await asyncio.gather(
            *(client.get_json(ENDPOINT) for _ in range(in_flight)), tighten_midflight()
        )

        assert budget.metrics.tightenings == 1, "the fixture must tighten exactly once"
        assert client.tightening_count == 1, f"counted {client.tightening_count} for one"


@respx.mock
async def test_healthy_slow_responses_do_not_tighten_the_rate() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("slow") == "1":
            await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    respx.get(url__startswith=ENDPOINT).mock(side_effect=respond)
    posture = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=80)

    async with HttpClient(
        allowed_hosts=frozenset({"boards-api.greenhouse.io"}), posture=posture, jitter=False
    ) as client:
        budget = client._budget_for("boards-api.greenhouse.io")
        for index in range(8):
            await client.get_json(f"{ENDPOINT}?fast={index}")
        for index in range(12):
            await client.get_json(f"{ENDPOINT}?slow=1&n={index}")

    assert budget.metrics.tightenings == 0
