from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.domain import HttpValidator
from stage.http import HttpClient, RatePosture, ValidatorCache

ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
HOSTS = frozenset({"boards-api.greenhouse.io"})
UNPACED = RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=50)


def _client(cache: ValidatorCache | None = None) -> HttpClient:
    return HttpClient(allowed_hosts=HOSTS, posture=UNPACED, cache=cache, jitter=False)


@respx.mock
async def test_a_validator_is_captured_from_the_response() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"jobs": []},
            headers={"ETag": '"abc123"', "Last-Modified": "Wed, 29 Jul 2026 10:00:00 GMT"},
        )
    )
    cache = ValidatorCache()

    async with _client(cache) as client:
        await client.get_json(ENDPOINT)

    stored = cache.get(ENDPOINT)
    assert stored is not None
    assert stored.etag == '"abc123"'
    assert stored.last_modified == "Wed, 29 Jul 2026 10:00:00 GMT"
    assert cache.pending[ENDPOINT] == stored


@respx.mock
async def test_a_stored_validator_is_sent_on_the_next_request() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(304))
    cache = ValidatorCache(
        {
            ENDPOINT: HttpValidator(
                url=ENDPOINT,
                etag='"abc123"',
                last_modified="Wed, 29 Jul 2026 10:00:00 GMT",
                fetched_at=datetime.now(UTC),
            )
        }
    )

    async with _client(cache) as client:
        response = await client.get_json(ENDPOINT)
        assert response.not_modified
        assert response.payload is None
        assert client.not_modified_count == 1

    sent = route.calls[0].request
    assert sent.headers["if-none-match"] == '"abc123"'
    assert sent.headers["if-modified-since"] == "Wed, 29 Jul 2026 10:00:00 GMT"


@respx.mock
async def test_an_unchanged_board_reports_not_modified_to_the_adapter(
    acme: object, run_time: datetime
) -> None:
    from stage.domain import Company, Platform
    from stage.sources import get_adapter

    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))
    cache = ValidatorCache(
        {
            f"{ENDPOINT}?content=true": HttpValidator(url=ENDPOINT, etag='"x"'),
            ENDPOINT: HttpValidator(url=ENDPOINT, etag='"x"'),
        }
    )
    company = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")

    async with _client(cache) as client:
        result = await get_adapter("greenhouse").fetch(company, client, run_time)

    assert result.not_modified
    assert result.jobs == ()


@respx.mock
async def test_a_response_without_validators_is_not_cached() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"jobs": []}))
    cache = ValidatorCache()

    async with _client(cache) as client:
        await client.get_json(ENDPOINT)

    assert cache.get(ENDPOINT) is None
    assert cache.pending == {}


@pytest.mark.parametrize("header", ["etag", "last-modified"])
@respx.mock
async def test_either_validator_alone_is_enough(header: str) -> None:
    value = '"solo"' if header == "etag" else "Wed, 29 Jul 2026 10:00:00 GMT"
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"jobs": []}, headers={header: value})
    )
    cache = ValidatorCache()

    async with _client(cache) as client:
        await client.get_json(ENDPOINT)

    stored = cache.get(ENDPOINT)
    assert stored is not None
    assert stored.usable
