import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.http import HttpClient, RatePosture, ValidatorCache
from stage.sources.base import PayloadValidationError
from stage.sources.jobbank import SEARCH, JobBankFeed
from stage.sources.themuse import SEARCH as MUSE_SEARCH
from stage.sources.themuse import TheMuseFeed

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _row(identifier: str, title: str, employer: str, flag: str = "jobinternshipflag") -> str:
    badge = f'<span class="{flag}">Internship</span>' if flag else ""
    return (
        f'<article id="article-{identifier}">'
        f'<h3 class="title"><span class="flag">{badge}</span>'
        f'<span class="noctitle">{title}</span></h3>'
        f'<ul><li class="business">{employer}</li>'
        f'<li class="location"><span class="wb-inv">Location</span>Montréal (QC)</li></ul>'
        f"</article>"
    )


def _page(*rows: str) -> str:
    return f"<!doctype html><html><body>{''.join(rows)}</body></html>"


def _client(hosts: frozenset[str]) -> HttpClient:
    return HttpClient(
        allowed_hosts=hosts, posture=RatePosture(min_interval_s=0.0), cache=ValidatorCache()
    )


def _muse(identifier: int, name: str, company: str | None) -> dict[str, object]:
    row: dict[str, object] = {
        "id": identifier,
        "name": name,
        "locations": [{"name": "Montreal, Canada"}],
        "refs": {"landing_page": f"https://www.themuse.com/jobs/{identifier}"},
        "contents": "<p>Body</p>",
        "categories": [{"name": "Software Engineering"}],
    }
    if company is not None:
        row["company"] = {"name": company}
    return row


@respx.mock
def test_job_bank_keeps_badged_rows_and_drops_the_rest() -> None:
    page = _page(
        _row("111", "computer programmer", "Acme Inc"),
        _row("222", "sales associate", "Retail Co", flag=""),
    )
    respx.get(url__startswith=SEARCH).mock(return_value=httpx.Response(200, text=page))

    async def run() -> None:
        async with _client(JobBankFeed.hosts) as client:
            result = await JobBankFeed().fetch(client, NOW)
        assert [job.title_raw for job in result.jobs] == ["computer programmer"], (
            "an unbadged row was kept, so the board's own classification is not deciding"
        )
        assert result.jobs[0].company == "Acme Inc", "the employer must come from the row"
        assert result.jobs[0].location_raw == "Montréal (QC)", (
            "the screen-reader label leaked into the location"
        )
        assert not result.authoritative, "a keyword slice must never be authoritative"

    asyncio.run(run())


@respx.mock
def test_job_bank_rebuilds_the_apply_url_without_the_session_id() -> None:
    respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, text=_page(_row("987", "developer", "Acme")))
    )

    async def run() -> None:
        async with _client(JobBankFeed.hosts) as client:
            result = await JobBankFeed().fetch(client, NOW)
        built = result.jobs[0].apply_url_raw
        assert built.endswith("/jobposting/987"), built
        assert "jsessionid" not in result.jobs[0].apply_url_raw, (
            "a rotating session id in the apply url changes the posting's identity every run"
        )

    asyncio.run(run())


@respx.mock
def test_job_bank_counts_a_row_that_cannot_be_identified_as_malformed() -> None:
    broken = '<article id="not-a-number"><span class="noctitle">x</span></article>'
    respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, text=_page(_row("1", "programmer", "Acme"), broken))
    )

    async def run() -> None:
        async with _client(JobBankFeed.hosts) as client:
            result = await JobBankFeed().fetch(client, NOW)
        assert len(result.jobs) == 1, "the good row must survive its malformed neighbour"
        assert "failed validation" in result.degraded, result.degraded

    asyncio.run(run())


@respx.mock
def test_job_bank_treats_a_search_that_matches_nothing_as_a_fact_not_drift() -> None:
    empty = _page()
    hit = _page(_row("5", "programmeur", "Acme"))
    respx.get(url__startswith=SEARCH).mock(
        side_effect=[httpx.Response(200, text=hit)] + [httpx.Response(200, text=empty)] * 60
    )

    async def run() -> None:
        async with _client(JobBankFeed.hosts) as client:
            result = await JobBankFeed().fetch(client, NOW)
        assert len(result.jobs) == 1, "the one matching search must still be kept"
        assert "matched nothing" in result.degraded, result.degraded

    asyncio.run(run())


@respx.mock
def test_job_bank_raises_when_no_search_returns_a_row_at_all() -> None:
    respx.get(url__startswith=SEARCH).mock(return_value=httpx.Response(200, text=_page()))

    async def run() -> None:
        async with _client(JobBankFeed.hosts) as client:
            await JobBankFeed().fetch(client, NOW)

    with pytest.raises(PayloadValidationError, match="changed"):
        asyncio.run(run())


@respx.mock
def test_the_muse_takes_each_employer_from_the_payload() -> None:
    body = {"results": [_muse(1, "Data Intern", "Autodesk")], "page_count": 1}
    respx.get(url__startswith=MUSE_SEARCH).mock(return_value=httpx.Response(200, json=body))

    async def run() -> None:
        async with _client(TheMuseFeed.hosts) as client:
            result = await TheMuseFeed().fetch(client, NOW)
        assert result.jobs[0].company == "Autodesk", (
            "a multi-employer feed must not file every posting under the source name"
        )
        assert result.jobs[0].signals.category == "Software Engineering"
        assert not result.authoritative, "a filtered slice must never be authoritative"

    asyncio.run(run())


@respx.mock
def test_the_muse_falls_back_when_a_posting_names_no_company() -> None:
    body = {"results": [_muse(2, "Intern", None)], "page_count": 1}
    respx.get(url__startswith=MUSE_SEARCH).mock(return_value=httpx.Response(200, json=body))

    async def run() -> None:
        async with _client(TheMuseFeed.hosts) as client:
            result = await TheMuseFeed().fetch(client, NOW)
        assert result.jobs[0].company == "The Muse", result.jobs[0].company

    asyncio.run(run())


@respx.mock
def test_the_muse_stops_at_the_page_count_it_is_given() -> None:
    body = {"results": [_muse(3, "Intern", "Acme")], "page_count": 1}
    route = respx.get(url__startswith=MUSE_SEARCH).mock(return_value=httpx.Response(200, json=body))

    async def run() -> None:
        async with _client(TheMuseFeed.hosts) as client:
            await TheMuseFeed().fetch(client, NOW)
        assert route.call_count == 1, (
            f"the walk ignored page_count and made {route.call_count} requests"
        )

    asyncio.run(run())


@respx.mock
def test_the_muse_drops_a_bad_row_rather_than_losing_the_page() -> None:
    body = {"results": [_muse(4, "Intern", "Acme"), {"id": 0, "name": ""}], "page_count": 1}
    respx.get(url__startswith=MUSE_SEARCH).mock(return_value=httpx.Response(200, json=body))

    async def run() -> None:
        async with _client(TheMuseFeed.hosts) as client:
            result = await TheMuseFeed().fetch(client, NOW)
        assert len(result.jobs) == 1, "one bad row cost the whole page"
        assert "failed validation" in result.degraded, result.degraded

    asyncio.run(run())


@respx.mock
def test_the_muse_raises_when_the_response_loses_its_carrier() -> None:
    respx.get(url__startswith=MUSE_SEARCH).mock(
        return_value=httpx.Response(200, json={"page_count": 1})
    )

    async def run() -> None:
        async with _client(TheMuseFeed.hosts) as client:
            await TheMuseFeed().fetch(client, NOW)

    with pytest.raises(PayloadValidationError, match="validation"):
        asyncio.run(run())


@respx.mock
def test_a_text_fetch_asks_for_html_rather_than_json() -> None:
    route = respx.get("https://example.test/page").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )

    async def run() -> None:
        async with _client(frozenset({"example.test"})) as client:
            await client.get_text("https://example.test/page")

    asyncio.run(run())
    accept = route.calls.last.request.headers["Accept"]
    assert "text/html" in accept, (
        f"a text fetch asked for {accept!r}; job bank answers 500 to an application/json Accept"
    )
