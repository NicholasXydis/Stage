from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.domain import Company, Platform
from stage.http import HttpClient, profile
from stage.sources import adapter_for_platform
from stage.sources.oracle_cloud import OracleCloudAdapter

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _company() -> Company:
    return Company(
        name="Oracle",
        platform=Platform.ORACLE_CLOUD,
        slug="eeho",
        oracle_host="eeho.fa.us2.oraclecloud.com",
        oracle_site="jobsearch",
    )


def _payload(rows: list[dict[str, object]], total: int) -> dict[str, object]:
    return {"items": [{"TotalJobsCount": total, "requisitionList": rows}]}


def _posting(identifier: str, title: str) -> dict[str, object]:
    return {
        "Id": identifier,
        "Title": title,
        "PostedDate": "2026-08-01",
        "PrimaryLocation": "Montreal, QC",
        "PrimaryLocationCountry": "CA",
        "ShortDescriptionStr": "Build <b>systems</b>",
        "ExternalQualificationsStr": "Python",
    }


def test_oracle_is_registered() -> None:
    assert adapter_for_platform(Platform.ORACLE_CLOUD) is not None


@respx.mock
async def test_oracle_paginates_and_maps_public_postings() -> None:
    adapter = OracleCloudAdapter()
    company = _company()

    def response(request: httpx.Request) -> httpx.Response:
        finder = request.url.params["finder"]
        if "offset=0" in finder:
            return httpx.Response(
                200, json=_payload([_posting("101", "Software Engineer Intern")], 2)
            )
        assert "offset=100" in finder
        return httpx.Response(200, json=_payload([_posting("102", "Data Engineering Co-op")], 2))

    route = respx.get(adapter.url_for(company)).mock(side_effect=response)
    async with HttpClient(
        allowed_hosts=adapter.hosts_for((company,)),
        posture=profile(adapter.rate_profile),
        jitter=False,
    ) as client:
        result = await adapter.fetch(company, client, NOW)

    assert route.call_count == 2
    assert result.authoritative
    assert [(job.title_raw, job.location_raw) for job in result.jobs] == [
        ("Software Engineer Intern", "Montreal, QC, CA"),
        ("Data Engineering Co-op", "Montreal, QC, CA"),
    ]
    first = result.jobs[0]
    assert first.apply_url_raw.endswith("/sites/jobsearch/job/101")
    assert first.description == "Build systems Python"
    assert first.source_posted_at == datetime(2026, 8, 1, tzinfo=UTC)


@respx.mock
async def test_oracle_does_not_close_jobs_after_hitting_its_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stage.sources.oracle_cloud.MAX_PAGES", 1)
    adapter = OracleCloudAdapter()
    company = _company()
    respx.get(adapter.url_for(company)).mock(
        return_value=httpx.Response(
            200, json=_payload([_posting("101", "Software Engineer Intern")], 2)
        )
    )

    async with HttpClient(
        allowed_hosts=adapter.hosts_for((company,)),
        posture=profile(adapter.rate_profile),
        jitter=False,
    ) as client:
        result = await adapter.fetch(company, client, NOW)

    assert not result.authoritative
    assert "page cap" in result.degraded


def test_the_page_cap_covers_the_largest_unfiltered_board() -> None:
    from stage.sources.oracle_cloud import MAX_PAGES, PAGE_SIZE

    assert MAX_PAGES * PAGE_SIZE >= 1306, (
        "Honeywell walks unfiltered at 1,306 rows and would truncate below this cap"
    )
