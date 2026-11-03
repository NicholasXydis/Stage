import asyncio
import time

import httpx
import respx

from stage.http import HttpClient, RatePosture

HOST = "pace.example.com"


def _client(posture: RatePosture) -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({HOST}),
        posture=posture,
        bucket_key="pace",
        jitter=False,
    )


@respx.mock
async def test_a_stalled_loop_never_lets_queued_requests_out_together() -> None:
    starts: list[float] = []

    def answer(request: httpx.Request) -> httpx.Response:
        starts.append(time.monotonic())
        if len(starts) == 1:
            time.sleep(0.5)
        return httpx.Response(200, json={})

    respx.get(url__startswith=f"https://{HOST}/").mock(side_effect=answer)
    posture = RatePosture(concurrency=3, min_interval_s=0.3, max_requests_per_run=50)
    async with _client(posture) as client:
        await asyncio.gather(*(client.get_json(f"https://{HOST}/{index}") for index in range(4)))

    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert min(gaps) >= 0.1 - 0.01, gaps


@respx.mock
async def test_a_short_retry_after_holds_the_whole_host_not_just_one_request() -> None:
    starts: dict[str, list[float]] = {"a": [], "b": []}

    def answer(request: httpx.Request) -> httpx.Response:
        name = request.url.path.strip("/")
        starts[name].append(time.monotonic())
        if name == "a" and len(starts["a"]) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={})

    respx.get(url__startswith=f"https://{HOST}/").mock(side_effect=answer)
    posture = RatePosture(concurrency=1, min_interval_s=0.05, max_requests_per_run=50)
    async with _client(posture) as client:
        first = asyncio.create_task(client.get_json(f"https://{HOST}/a"))
        await asyncio.sleep(0.01)
        await asyncio.gather(first, client.get_json(f"https://{HOST}/b"))

    assert starts["b"][0] - starts["a"][0] >= 0.95, "the host asked for a second's rest"


@respx.mock
async def test_a_redirect_hop_is_spaced_and_counted_like_any_request() -> None:
    starts: list[float] = []

    def answer(request: httpx.Request) -> httpx.Response:
        starts.append(time.monotonic())
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": f"https://{HOST}/end"})
        return httpx.Response(200, json={})

    respx.get(url__startswith=f"https://{HOST}/").mock(side_effect=answer)
    posture = RatePosture(concurrency=1, min_interval_s=0.3, max_requests_per_run=50)
    async with _client(posture) as client:
        await client.get_json(f"https://{HOST}/start")
        assert client.request_count == 2

    assert starts[1] - starts[0] >= 0.3 - 0.01
