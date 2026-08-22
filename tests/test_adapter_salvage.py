from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.domain import Company, Platform
from stage.http import HttpClient, profile
from stage.http.client import MAX_RESPONSE_BYTES
from stage.sources import get_adapter
from stage.sources.base import PayloadValidationError

NOW = datetime(2026, 8, 21, tzinfo=UTC)
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
ORACLE = (
    "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
)


def _greenhouse_company() -> Company:
    return Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")


def _oracle_company() -> Company:
    return Company(
        name="Oracle",
        platform=Platform.ORACLE_CLOUD,
        slug="eeho",
        oracle_host="eeho.fa.us2.oraclecloud.com",
        oracle_site="jobsearch",
    )


def _client(name: str) -> HttpClient:
    adapter = get_adapter(name)
    hosts = adapter.hosts or frozenset({"eeho.fa.us2.oraclecloud.com"})
    return HttpClient(allowed_hosts=hosts, posture=profile(adapter.rate_profile), jitter=False)


def _oversized() -> httpx.Response:
    return httpx.Response(
        200,
        json={"jobs": []},
        headers={"content-length": str(MAX_RESPONSE_BYTES + 1)},
    )


def _greenhouse_job(identifier: int, title: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{identifier}",
        "location": {"name": "Montreal, QC"},
        "updated_at": "2026-08-01T00:00:00Z",
    }


def _oracle_payload(rows: list[dict[str, object]], total: int) -> dict[str, object]:
    return {"items": [{"TotalJobsCount": total, "requisitionList": rows}]}


def _oracle_posting(identifier: str) -> dict[str, object]:
    return {
        "Id": identifier,
        "Title": f"Intern {identifier}",
        "PostedDate": "2026-08-01",
        "PrimaryLocation": "Montreal, QC",
        "PrimaryLocationCountry": "CA",
    }


@respx.mock
async def test_a_board_over_the_size_cap_is_refetched_without_descriptions() -> None:
    respx.get(GREENHOUSE, params={"content": "true"}).mock(return_value=_oversized())
    respx.get(GREENHOUSE, params={"content": "false"}).mock(
        return_value=httpx.Response(200, json={"jobs": [_greenhouse_job(1, "Software Intern")]})
    )
    async with _client("greenhouse") as client:
        result = await get_adapter("greenhouse").fetch(_greenhouse_company(), client, NOW)

    assert [job.title_raw for job in result.jobs] == ["Software Intern"], (
        "the fallback listing lost its postings"
    )
    assert "without descriptions" in result.degraded, "the reduced fetch was not reported"


@respx.mock
async def test_the_reduced_fetch_still_honours_an_unchanged_answer() -> None:
    respx.get(GREENHOUSE, params={"content": "true"}).mock(return_value=_oversized())
    respx.get(GREENHOUSE, params={"content": "false"}).mock(return_value=httpx.Response(304))
    async with _client("greenhouse") as client:
        result = await get_adapter("greenhouse").fetch(_greenhouse_company(), client, NOW)

    assert result.not_modified, "a 304 on the reduced fetch must not read as an empty board"


@respx.mock
async def test_an_oracle_walk_that_ends_on_a_stale_page_keeps_its_rows_and_loses_authority() -> (
    None
):
    route = respx.get(ORACLE)
    route.side_effect = [
        httpx.Response(200, json=_oracle_payload([_oracle_posting("1")], total=200)),
        httpx.Response(304),
    ]
    async with _client("oracle_cloud") as client:
        result = await get_adapter("oracle_cloud").fetch(_oracle_company(), client, NOW)

    assert len(result.jobs) == 1, "the rows read before the stale page were dropped"
    assert not result.authoritative, "a walk that ended on a 304 cannot close postings"
    assert "304" in result.degraded, "the stale page was not reported"


@respx.mock
async def test_an_oracle_page_that_empties_before_the_reported_total_is_not_authoritative() -> None:
    route = respx.get(ORACLE)
    route.side_effect = [
        httpx.Response(200, json=_oracle_payload([_oracle_posting("1")], total=500)),
        httpx.Response(200, json=_oracle_payload([], total=500)),
    ]
    async with _client("oracle_cloud") as client:
        result = await get_adapter("oracle_cloud").fetch(_oracle_company(), client, NOW)

    assert len(result.jobs) == 1, "the salvaged rows were discarded"
    assert not result.authoritative, "a short walk against a larger total closes nothing"
    assert "empty page" in result.degraded, "the premature end was not reported"


@respx.mock
async def test_an_oracle_response_of_the_wrong_shape_fails_loudly() -> None:
    respx.get(ORACLE).mock(return_value=httpx.Response(200, json={"items": []}))
    async with _client("oracle_cloud") as client:
        with pytest.raises(PayloadValidationError, match="invalid recruiting search response"):
            await get_adapter("oracle_cloud").fetch(_oracle_company(), client, NOW)
