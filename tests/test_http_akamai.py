import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from stage.domain import RateState
from stage.http import (
    BreakerOpenError,
    ForbiddenError,
    HostBudget,
    HttpClient,
    RatePosture,
    RedirectNotAllowedError,
    profile,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
HOST = "acme.wd3.myworkdayjobs.com"
URL = f"https://{HOST}/jobs"
FAST = RatePosture(concurrency=2, min_interval_s=0.02, max_requests_per_run=120)


def _client(posture: RatePosture | None = None, **kwargs: object) -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({HOST}),
        posture=posture or FAST,
        bucket_key="workday",
        jitter=False,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


@respx.mock
async def test_one_denying_tenant_fails_its_board_without_blocking_its_neighbours() -> None:
    healthy = "other.wd3.myworkdayjobs.com"
    route = respx.get(URL).mock(return_value=httpx.Response(403))

    client = HttpClient(
        allowed_hosts=frozenset({HOST, healthy}),
        posture=FAST,
        bucket_key="workday",
        jitter=False,
        now=NOW,
    )
    async with client:
        with pytest.raises(ForbiddenError):
            await client.get_json(URL)
        settled = client.rate_state(NOW)

    assert route.call_count == 1, "a 403 is a decision, not congestion — never retried"
    assert len(settled) == 1
    assert settled[0].consecutive_failures == 1
    assert settled[0].reason == "HTTP 403"
    assert settled[0].blocked_until is None, (
        "one dead tenant on a bucket of hundreds is a fact about that tenant, not about our rate"
    )


@respx.mock
async def test_a_bucket_key_over_one_host_still_blocks_on_its_first_denial() -> None:
    host = "api.collage.co"
    respx.get(f"https://{host}/jobs").mock(return_value=httpx.Response(403))

    client = HttpClient(
        allowed_hosts=frozenset({host}),
        posture=FAST,
        bucket_key="collage",
        jitter=False,
        now=NOW,
    )
    async with client:
        with pytest.raises(ForbiddenError):
            await client.get_json(f"https://{host}/jobs")
        settled = client.rate_state(NOW)

    assert settled[0].blocked_until is not None, (
        "a named bucket over a single host has no second host to corroborate, so it must block"
    )


@respx.mock
async def test_two_denying_tenants_do_block_the_shared_bucket() -> None:
    second = "other.wd3.myworkdayjobs.com"
    respx.get(URL).mock(return_value=httpx.Response(403))
    respx.get(f"https://{second}/jobs").mock(return_value=httpx.Response(403))

    client = HttpClient(
        allowed_hosts=frozenset({HOST, second}),
        posture=FAST,
        bucket_key="workday",
        jitter=False,
        now=NOW,
    )
    async with client:
        for url in (URL, f"https://{second}/jobs"):
            with pytest.raises(ForbiddenError):
                await client.get_json(url)
        settled = client.rate_state(NOW)

    assert settled[0].blocked_until is not None, (
        "denials spread across distinct hosts is evidence about us, not about one tenant"
    )
    assert settled[0].blocked_until > NOW
    assert settled[0].min_interval_override is not None


@respx.mock
async def test_a_single_host_bucket_still_blocks_on_its_first_denial() -> None:
    host = "api.smartrecruiters.com"
    respx.get(f"https://{host}/jobs").mock(return_value=httpx.Response(403))

    client = HttpClient(allowed_hosts=frozenset({host}), posture=FAST, jitter=False, now=NOW)
    async with client:
        with pytest.raises(ForbiddenError):
            await client.get_json(f"https://{host}/jobs")
        settled = client.rate_state(NOW)

    assert settled[0].blocked_until is not None, (
        "when the bucket is one host, its denial is the only evidence there is"
    )
    assert settled[0].min_interval_override is not None


@respx.mock
async def test_a_403_stops_requests_that_were_already_scheduled() -> None:
    paced = RatePosture(concurrency=1, min_interval_s=0.4, max_requests_per_run=120)
    route = respx.get(URL).mock(return_value=httpx.Response(403))

    async with _client(paced) as client:
        results = await asyncio.gather(
            *(client.get_json(URL) for _ in range(8)), return_exceptions=True
        )

    forbidden = [r for r in results if isinstance(r, ForbiddenError)]
    stopped = [r for r in results if isinstance(r, BreakerOpenError)]

    assert route.call_count < 8, "the breaker must stop siblings that already reserved"
    assert forbidden and stopped
    assert route.call_count == len(forbidden)


@respx.mock
async def test_a_request_refused_after_reserving_refunds_the_ceiling() -> None:
    respx.get(URL).mock(return_value=httpx.Response(403))
    paced = RatePosture(concurrency=2, min_interval_s=0.2, max_requests_per_run=120)

    async with _client(paced) as client:
        await asyncio.gather(*(client.get_json(URL) for _ in range(6)), return_exceptions=True)
        budget = client._budgets["workday"]
        sent = len(respx.calls)

        assert budget.requests == sent, (
            "requests refused after reserving must not consume ceiling count"
        )
        assert budget.semaphore._value == paced.concurrency, "every slot was released"
        assert budget.semaphore.locked() is False


@respx.mock
async def test_a_blocked_bucket_refuses_without_charging_the_ceiling() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    state = RateState(
        bucket="workday",
        updated_at=NOW,
        blocked_until=NOW + timedelta(hours=1),
        reason="HTTP 403",
    )
    async with _client(rate_state={"workday": state}) as client:
        with pytest.raises(Exception, match="blocked"):
            await client.get_json(URL)
        assert client._budgets["workday"].requests == 0


@respx.mock
async def test_a_redirect_to_a_host_outside_the_allow_list_is_refused() -> None:
    evil = respx.get("https://evil.test/jobs").mock(return_value=httpx.Response(200, json={}))
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.test/jobs"})
    )

    async with _client() as client:
        with pytest.raises(RedirectNotAllowedError, match="allow-list"):
            await client.get_json(URL)

    assert not evil.called


@respx.mock
async def test_a_redirect_downgrading_to_http_is_refused() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"Location": f"http://{HOST}/jobs"})
    )
    async with _client() as client:
        with pytest.raises(RedirectNotAllowedError, match="downgrade"):
            await client.get_json(URL)


@respx.mock
async def test_a_redirect_within_the_allow_list_is_followed() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"Location": f"https://{HOST}/v2/jobs"})
    )
    respx.get(f"https://{HOST}/v2/jobs").mock(return_value=httpx.Response(200, json={"ok": True}))
    async with _client() as client:
        result = await client.get_json(URL)
    assert result.payload == {"ok": True}


@respx.mock
async def test_a_redirect_loop_terminates() -> None:
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"Location": URL}))
    async with _client() as client:
        with pytest.raises(RedirectNotAllowedError, match="redirects"):
            await client.get_json(URL)


def test_the_three_controls_stay_independent() -> None:
    budget = HostBudget(posture=profile("workday"))
    assert budget.semaphore._value == profile("workday").concurrency
    assert budget.stride == pytest.approx(0.75)
    assert budget.requests == 0


@respx.mock
async def test_a_client_reports_only_its_own_requests_not_the_whole_shared_dict() -> None:
    other = "boards-api.greenhouse.io"
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(f"https://{other}/x").mock(return_value=httpx.Response(200, json={}))

    shared: dict[str, HostBudget] = {}
    async with HttpClient(
        allowed_hosts=frozenset({HOST}),
        posture=FAST,
        bucket_key="workday",
        jitter=False,
        now=NOW,
        budgets=shared,
    ) as first:
        await first.get_json(URL)
        await first.get_json(URL)

        async with HttpClient(
            allowed_hosts=frozenset({other}),
            posture=FAST,
            jitter=False,
            now=NOW,
            budgets=shared,
        ) as second:
            await second.get_json(f"https://{other}/x")

            assert second.request_count == 1, "a source must not report its neighbour's work"
            assert first.request_count == 2


@respx.mock
async def test_two_clients_on_one_bucket_each_report_their_own_share() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    shared: dict[str, HostBudget] = {}

    def build() -> HttpClient:
        return HttpClient(
            allowed_hosts=frozenset({HOST}),
            posture=FAST,
            bucket_key="workday",
            jitter=False,
            now=NOW,
            budgets=shared,
        )

    async with build() as first:
        await first.get_json(URL)
        async with build() as second:
            await second.get_json(URL)
            await second.get_json(URL)

            assert first.request_count == 1
            assert second.request_count == 2
            assert shared["workday"].requests == 3, "the bucket still counts the ceiling"
