from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from stage.domain import RateState
from stage.http import BucketBlockedError, HttpClient, profile
from stage.sources import get_feeds
from stage.sources.base import PayloadValidationError
from stage.sources.speedyapply import SpeedyApplyFeed, SpeedyApplyListing


def _client(feed: SpeedyApplyFeed) -> HttpClient:
    return HttpClient(allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False)


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
                text=(
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
                    text=(
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
    assert "1 of 4 files unavailable" in result.degraded
    assert result.stale_urls == (urls[1],)


@respx.mock
async def test_a_file_answering_not_modified_is_read_again_so_its_rows_stay_open(
    run_time: datetime,
) -> None:
    feed = SpeedyApplyFeed()
    urls = feed.plan(run_time)
    calls = {"round": 0}

    def answer(index: int) -> Callable[[httpx.Request], httpx.Response]:
        def respond(request: httpx.Request) -> httpx.Response:
            if index == 0 and calls["round"] and request.headers.get("if-none-match"):
                return httpx.Response(304, headers={"ETag": '"same"'})
            title = (
                "Software Engineer Intern" if index == 0 or not calls["round"] else "Data Intern"
            )
            body = _table(
                f"Acme {index}", title, "Toronto, ON, Canada", f"https://jobs.example/{index}"
            )
            return httpx.Response(
                200, text=body, headers={"ETag": '"same"' if index == 0 else f'"v{calls["round"]}"'}
            )

        return respond

    for index, url in enumerate(urls):
        respx.get(url).mock(side_effect=answer(index))
    async with _client(feed) as client:
        await feed.fetch(client, run_time)
        calls["round"] = 1
        result = await feed.fetch(client, run_time)

    assert result.authoritative
    assert len(result.jobs) == 4
    assert respx.get(urls[0]).call_count == 3


@respx.mock
async def test_a_markup_shape_change_fails_loudly(run_time: datetime) -> None:
    feed = SpeedyApplyFeed()
    url = feed.plan(run_time)[0]
    respx.get(url).mock(return_value=httpx.Response(200, text="not an internship table"))

    async with _client(feed) as client:
        with pytest.raises(PayloadValidationError, match="no internship table rows"):
            await feed.fetch(client, run_time)


def test_the_tables_come_from_raw_files_not_the_rate_limited_contents_api(
    run_time: datetime,
) -> None:
    feed = SpeedyApplyFeed()

    assert feed.hosts == frozenset({"raw.githubusercontent.com"})
    assert all(
        url.startswith("https://raw.githubusercontent.com/speedyapply/")
        for url in feed.plan(run_time)
    )


@respx.mock
async def test_files_the_client_refused_to_send_defer_the_feed_rather_than_fail_it(
    run_time: datetime,
) -> None:
    feed = SpeedyApplyFeed()
    routes = [
        respx.get(url).mock(return_value=httpx.Response(200, text=""))
        for url in feed.plan(run_time)
    ]
    blocked = {
        "raw.githubusercontent.com": RateState(
            bucket="raw.githubusercontent.com",
            updated_at=run_time,
            blocked_until=run_time + timedelta(hours=1),
            reason="Retry-After: 3600s",
        )
    }
    async with HttpClient(
        allowed_hosts=feed.hosts,
        posture=profile(feed.rate_profile),
        jitter=False,
        rate_state=blocked,
        now=run_time,
    ) as client:
        with pytest.raises(BucketBlockedError):
            await feed.fetch(client, run_time)

    assert not any(route.called for route in routes)


@pytest.mark.parametrize(
    ("location", "url", "expected"),
    [
        (
            "",
            "https://deezee.wd108.myworkdayjobs.com/en-US/deezee_careers/job/Des-Moines/"
            "Software-Development-Intern_REQ00471",
            "Des-Moines",
        ),
        (
            "",
            "https://bakerhughes.wd5.myworkdayjobs.com/bakerhughes/job/"
            "GB-NU-CRAMLINGTON-ATLEY-WAY/Intern---Software-Engineering---2027_R171503",
            "GB-NU-CRAMLINGTON-ATLEY-WAY",
        ),
        ("", "https://ryan.wd1.myworkdayjobs.com/en-US/ryan/job/Summer-Intern_R1", ""),
        ("", "https://jobs.smartrecruiters.com/DeliveryHero/7440-data-analyst-intern", ""),
        ("Austin, TX", "https://acme.wd1.myworkdayjobs.com/a/job/Toronto/Intern_R2", "Austin, TX"),
    ],
)
def test_a_blank_place_is_read_from_a_workday_link(
    run_time: datetime, location: str, url: str, expected: str
) -> None:
    listing = SpeedyApplyListing(company="Acme", title="Intern", location=location, url=url)
    assert SpeedyApplyFeed()._to_job(listing, run_time).location_raw == expected
