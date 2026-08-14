import base64
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from stage.http import HttpClient, profile
from stage.sources import get_feeds
from stage.sources.base import PayloadValidationError
from stage.sources.speedyapply import SpeedyApplyFeed


def _client(feed: SpeedyApplyFeed) -> HttpClient:
    return HttpClient(allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False)


def _payload(table: str) -> dict[str, str]:
    return {
        "encoding": "base64",
        "content": base64.b64encode(table.encode("utf-8")).decode("ascii"),
    }


def _table(company: str, title: str, location: str, url: str, age: str = "2d") -> str:
    return (
        "| Company | Position | Location | Salary | Posting | Age |\n"
        "|---|---|---|---|---|---|\n"
        f'| <a href="https://{company.lower()}.example"><strong>{company}</strong></a> '
        f'| {title} | {location} | $50/hr | <a href="{url}">Apply</a> | {age} |\n'
    )


def test_the_feed_registers_and_uses_the_upcoming_season(run_time: datetime) -> None:
    feed = SpeedyApplyFeed()
    urls = feed.plan(run_time)
    assert get_feeds()[feed.name] is not None
    assert len(urls) == 4
    assert all(f"{feed.season_year(run_time)}-" in url for url in urls)
    assert any("SWE-College-Jobs" in url for url in urls)
    assert any("AI-College-Jobs" in url for url in urls)


def test_the_five_column_international_table_layout_is_supported() -> None:
    rows, malformed = SpeedyApplyFeed()._rows(
        '<a name="top"></a>\n'
        '  <a href="https://speedyapply.example">SpeedyApply</a>\n'
        "| Company | Position | Location | Posting | Age |\n"
        "|---|---|---|---|---|\n"
        '| <a href="https://acme.example"><strong>Acme</strong></a> | '
        "Software Engineer Intern | Tokyo, Japan | "
        '<a href="https://jobs.example/acme">Apply</a> | 3d |\n',
        "2027",
    )

    assert malformed == 0
    assert rows == [
        {
            "company": "Acme",
            "title": "Software Engineer Intern",
            "location": "Tokyo, Japan",
            "url": "https://jobs.example/acme",
            "age": "3d",
        }
    ]


@respx.mock
async def test_internship_tables_map_to_jobs_and_keep_source_dates(run_time: datetime) -> None:
    feed = SpeedyApplyFeed()
    for index, url in enumerate(feed.plan(run_time)):
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json=_payload(
                    _table(
                        f"Acme {index}",
                        "Software Engineer Intern",
                        "Montréal, QC, Canada",
                        f"https://jobs.example/{index}",
                    )
                ),
            )
        )
    async with _client(feed) as client:
        result = await feed.fetch(client, run_time)

    assert result.authoritative
    assert len(result.jobs) == 4
    assert {job.signals.employment_type for job in result.jobs} == {"internship"}
    assert {job.source_posted_at for job in result.jobs} == {run_time - timedelta(days=2)}


@respx.mock
async def test_a_missing_table_keeps_arrived_jobs_but_cannot_close_the_feed(
    run_time: datetime,
) -> None:
    feed = SpeedyApplyFeed()
    urls = feed.plan(run_time)
    for index, url in enumerate(urls):
        if index == 1:
            respx.get(url).mock(return_value=httpx.Response(404))
        else:
            respx.get(url).mock(
                return_value=httpx.Response(
                    200,
                    json=_payload(
                        _table(
                            f"Acme {index}",
                            "Machine Learning Engineer Intern",
                            "Toronto, ON, Canada",
                            f"https://jobs.example/{index}",
                        )
                    ),
                )
            )
    async with _client(feed) as client:
        result = await feed.fetch(client, run_time)

    assert len(result.jobs) == 3
    assert not result.authoritative
    assert "1 of 4 file(s) unavailable" in result.degraded
    assert result.stale_urls == (urls[1],)


@respx.mock
async def test_a_markup_shape_change_fails_loudly(run_time: datetime) -> None:
    feed = SpeedyApplyFeed()
    url = feed.plan(run_time)[0]
    respx.get(url).mock(return_value=httpx.Response(200, json=_payload("not an internship table")))

    async with _client(feed) as client:
        with pytest.raises(PayloadValidationError, match="no internship table rows"):
            await feed.fetch(client, run_time)
