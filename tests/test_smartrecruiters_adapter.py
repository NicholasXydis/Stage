from datetime import datetime

import httpx
import pytest
import respx

from stage.domain import Company, Platform, job_id
from stage.http import HttpClient, profile
from stage.sources import get_adapter
from stage.sources.base import PayloadValidationError
from stage.sources.smartrecruiters import MAX_PAGES, PAGE_SIZE

ENDPOINT = "https://api.smartrecruiters.com/v1/companies/acme/postings"


@pytest.fixture
def acme_sr() -> Company:
    return Company(name="Acme", platform=Platform.SMARTRECRUITERS, slug="acme")


def _client() -> HttpClient:
    adapter = get_adapter("smartrecruiters")
    return HttpClient(
        allowed_hosts=adapter.hosts, posture=profile(adapter.rate_profile), jitter=False
    )


def _page(total: int, start: int, count: int) -> dict[str, object]:
    return {
        "totalFound": total,
        "content": [
            {
                "id": str(start + n),
                "name": f"Intern {start + n}",
                "location": {"city": "Montréal", "region": "QC", "country": "ca"},
            }
            for n in range(count)
        ],
    }


@respx.mock
async def test_pagination_collects_every_page(acme_sr: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json=_page(150, 0, PAGE_SIZE)),
            httpx.Response(200, json=_page(150, PAGE_SIZE, 50)),
        ]
    )
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert len(result.jobs) == 150
    assert result.degraded == ""
    assert result.jobs[0].location_raw == "Montréal, QC, ca"


@respx.mock
async def test_a_304_on_the_first_page_skips_the_rest(acme_sr: Company, run_time: datetime) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(304))
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert result.not_modified is True
    assert result.jobs == ()
    assert route.call_count == 1


@respx.mock
async def test_a_queued_detail_row_makes_the_listing_bypass_its_validator(
    acme_sr: Company, run_time: datetime
) -> None:
    detail = respx.get(f"{ENDPOINT}/0").mock(
        return_value=httpx.Response(
            200, json={"jobAd": {"sections": {"body": {"text": "<p>Build things.</p>"}}}}
        )
    )
    listing = respx.get(url__startswith=f"{ENDPOINT}?").mock(
        return_value=httpx.Response(200, json=_page(1, 0, 1))
    )
    adapter = get_adapter("smartrecruiters")
    queued = job_id("smartrecruiters", "acme", "0")

    async with _client() as client:
        client.cache.record(
            f"{ENDPOINT}?limit={PAGE_SIZE}&offset=0",
            httpx.Headers({"etag": '"v1"'}),
            run_time,
        )
        client.cache.record(f"{ENDPOINT}/0", httpx.Headers({"etag": '"d1"'}), run_time)
        result = await adapter.fetch(acme_sr, client, run_time, None, [queued])

    assert "if-none-match" not in {header.lower() for header in listing.calls[0].request.headers}, (
        "a queued board must skip its validator, or the 304 skips the detail phase"
    )
    assert "if-none-match" not in {header.lower() for header in detail.calls[0].request.headers}, (
        "a detail read must not send its validator, or a 304 brings back no body"
    )
    assert detail.call_count == 1
    assert result.jobs[0].description == "Build things."
    assert result.authoritative, "the listing was fetched in full, so it still closes"


@respx.mock
async def test_a_board_with_nothing_queued_still_sends_its_validator(
    acme_sr: Company, run_time: datetime
) -> None:
    listing = respx.get(url__startswith=f"{ENDPOINT}?").mock(return_value=httpx.Response(304))
    adapter = get_adapter("smartrecruiters")
    other_board = job_id("smartrecruiters", "othercorp", "0")

    async with _client() as client:
        client.cache.record(
            f"{ENDPOINT}?limit={PAGE_SIZE}&offset=0",
            httpx.Headers({"etag": '"v1"'}),
            run_time,
        )
        result = await adapter.fetch(acme_sr, client, run_time, None, [other_board])

    assert result.not_modified is True
    assert listing.calls[0].request.headers.get("if-none-match") == '"v1"', (
        "the bypass is per board, not per source"
    )


@respx.mock
async def test_an_empty_page_stops_paging(acme_sr: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json=_page(150, 0, 2)),
            httpx.Response(200, json={"totalFound": 150, "content": []}),
        ]
    )
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert len(result.jobs) == 2
    assert not result.authoritative
    assert "before the board's stated total" in result.degraded


@respx.mock
async def test_a_server_ignoring_offset_hits_the_hard_stop(
    acme_sr: Company, run_time: datetime
) -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_page(10**6, 0, PAGE_SIZE))
    )
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert route.call_count == 1 + 2 * MAX_PAGES
    assert "page cap" in result.degraded


@respx.mock
async def test_a_board_too_big_to_read_keeps_to_canada_and_the_us(
    acme_sr: Company, run_time: datetime
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        country = request.url.params.get("country")
        if country == "us":
            return httpx.Response(200, json=_page(3, 100, 3))
        if country == "ca":
            return httpx.Response(200, json=_page(1, 200, 1))
        return httpx.Response(200, json=_page(MAX_PAGES * PAGE_SIZE + 1, 0, PAGE_SIZE))

    route = respx.get(ENDPOINT).mock(side_effect=answer)
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert route.call_count == 3
    assert sorted(job.id.rsplit(":", 1)[-1] for job in result.jobs) == ["100", "101", "102", "200"]
    assert not result.degraded


@respx.mock
async def test_a_scoped_read_closes_nothing_it_could_not_see(
    acme_sr: Company, run_time: datetime
) -> None:
    unscoped = _page(MAX_PAGES * PAGE_SIZE + 1, 0, PAGE_SIZE)
    unscoped["content"] = [{"id": "remote-1", "name": "Intern", "location": {"remote": True}}]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("country"):
            return httpx.Response(200, json=_page(1, 100, 1))
        return httpx.Response(200, json=unscoped)

    respx.get(ENDPOINT).mock(side_effect=answer)
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert "remote-1" not in {job.id.rsplit(":", 1)[-1] for job in result.jobs}
    assert not result.authoritative, "a posting with no country is in neither scope"


@respx.mock
async def test_a_scoped_page_is_always_read_in_full(acme_sr: Company, run_time: datetime) -> None:
    unscoped = _page(MAX_PAGES * PAGE_SIZE + 1, 0, PAGE_SIZE)
    asked: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if not request.url.params.get("country"):
            return httpx.Response(200, json=unscoped, headers={"ETag": '"all"'})
        asked.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, json=_page(1, 100, 1), headers={"ETag": '"scope"'})

    respx.get(ENDPOINT).mock(side_effect=answer)
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        await adapter.fetch(acme_sr, client, run_time)
        asked.clear()
        result = await adapter.fetch(acme_sr, client, run_time)

    assert asked and not any(request.headers.get("if-none-match") for request in asked)
    assert result.jobs, "a country's postings are kept even when its page did not change"
    assert "304" not in result.degraded


@respx.mock
async def test_a_missing_content_key_is_a_shape_change_and_fails_loudly(
    acme_sr: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"totalFound": 1}))
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        with pytest.raises(PayloadValidationError, match="content"):
            await adapter.fetch(acme_sr, client, run_time)


@respx.mock
async def test_a_dropped_row_still_counts_against_the_page_total(
    acme_sr: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 2,
                "content": [{"id": "1", "name": "Intern"}, {"name": "no id here"}],
            },
        )
    )
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert len(result.jobs) == 1
    assert "dropped 1 posting that failed validation" in result.degraded
    assert not result.authoritative
    assert respx.calls.call_count == 1, "the total is reached once the drop is counted"


@respx.mock
async def test_the_apply_url_points_at_the_public_board(
    acme_sr: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=_page(1, 7, 1)))
    adapter = get_adapter("smartrecruiters")
    async with _client() as client:
        result = await adapter.fetch(acme_sr, client, run_time)

    assert result.jobs[0].apply_url_raw == "https://jobs.smartrecruiters.com/acme/7"
