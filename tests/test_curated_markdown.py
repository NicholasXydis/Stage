from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import CompanyFailed
from stage.http import HttpClient, HttpStatusError, profile
from stage.sources import get_feeds
from stage.sources.base import PayloadValidationError
from stage.sources.curated_markdown import HanziliFeed, NegarFeed, NorthwesternQuantFeed
from stage.sources.zshah import ZshahFeed


def test_public_markdown_feeds_register_and_keep_only_current_internship_rows() -> None:
    feeds = get_feeds()
    assert {"negar", "hanzili", "northwestern-quant", "zshah101"} <= set(feeds)

    nested_apply = "[![Apply](https://image)](https://jobs.example/acme)"
    negar, malformed = NegarFeed().rows(
        "| Company | Role | Location | Apply | Date Posted |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Acme | Software Engineer Intern | Montreal, QC | "
        f"{nested_apply} | Aug 1, 2026 |\n"
        "| ↳ | Data Engineer Co-op | Toronto, ON | "
        "[Apply](https://jobs.example/data) | Aug 1, 2026 |\n"
        "| Acme | Closed | Montreal, QC | Closed🔒 | Aug 1, 2026 |\n"
    )
    assert malformed == 0
    assert [(row.company, row.title, row.url) for row in negar] == [
        ("Acme", "Software Engineer Intern", "https://jobs.example/acme"),
        ("Acme", "Data Engineer Co-op", "https://jobs.example/data"),
    ]

    hanzili, malformed = HanziliFeed().rows(
        "| Title | Company | Role | Company Info | Details | Location | Apply |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| Software Developer Intern | Acme | Build APIs | Company | Intern · 4mo | "
        "Montreal, QC | [Apply](<https://jobs.example/acme>) |\n"
        "| Junior Developer | Acme | Maintain systems | Company | Contract · 3mo | "
        "Montreal, QC | [Apply](https://jobs.example/contract) |\n"
    )
    assert malformed == 0
    assert [(row.title, row.description) for row in hanzili] == [
        ("Software Developer Intern", "Build APIs")
    ]

    quant, malformed = NorthwesternQuantFeed().rows(
        "## Quant Firm\n"
        "**Locations**: Chicago, IL\n"
        "|Role|Links|\n"
        "|---|---|\n"
        "|SWE|[✅ C++](https://jobs.example/cpp)|\n"
        "|QR|[✅ PhD](https://jobs.example/phd)|\n"
        "|QR Fellowship|[✅](https://jobs.example/fellowship)|\n"
    )
    assert malformed == 0
    assert [(row.title, row.location) for row in quant] == [
        ("Software Engineer Intern — C++", "Chicago, IL"),
        ("Quantitative Research Intern — PhD", "Chicago, IL"),
    ]


@respx.mock
async def test_zshah_feed_accepts_only_explicit_internships(run_time: datetime) -> None:
    feed = ZshahFeed()
    respx.get(feed.plan(run_time)[0]).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "one",
                        "company": "Acme",
                        "title": "Software Engineer Intern",
                        "location": "Montreal, QC",
                        "url": "https://jobs.example/one",
                        "program": "Internship",
                    },
                    {
                        "id": "two",
                        "company": "Acme",
                        "title": "Software Engineer",
                        "location": "Montreal, QC",
                        "url": "https://jobs.example/two",
                        "program": "New Grad",
                    },
                ]
            },
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert [job.title_raw for job in result.jobs] == ["Software Engineer Intern"]
    assert result.jobs[0].signals.employment_type == "internship"


@respx.mock
async def test_seasonal_feed_falls_back_when_the_rolled_file_is_missing(
    run_time: datetime,
) -> None:
    feed = NegarFeed()
    seasonal, current = feed.plan(run_time)
    assert seasonal != current, "the seasonal file and the current file must be distinct urls"
    respx.get(seasonal).mock(return_value=httpx.Response(404))
    respx.get(current).mock(
        return_value=httpx.Response(
            200,
            text=(
                "| Company | Role | Location | Apply | Date Posted |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| Acme | Software Engineer Intern | Montreal, QC | "
                "[Apply](https://jobs.example/acme) | Aug 1, 2026 |\n"
            ),
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert [job.apply_url_raw for job in result.jobs] == ["https://jobs.example/acme"]


NEGAR_TABLE = (
    "| Company | Role | Location | Apply | Date Posted |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| Acme | Software Engineer Intern | Montreal, QC | "
    "[Apply](https://jobs.example/acme) | Aug 1, 2026 |\n"
)


async def _negar_run(db_path: Path, moment: datetime) -> list[object]:
    from stage.services.sync import sync
    from stage.storage import open_repository

    async with open_repository(db_path) as repository:
        return [
            event
            async for event in sync(
                repository, [], sources=["negar"], now_fn=lambda: moment, still_open=False
            )
        ]


@respx.mock
async def test_a_missing_season_file_is_asked_once_a_season_not_every_tick(
    db_path: Path, run_time: datetime
) -> None:
    seasonal, current = NegarFeed().plan(run_time)
    missing = respx.get(seasonal).mock(return_value=httpx.Response(404))
    served = respx.get(current).mock(
        return_value=httpx.Response(200, text=NEGAR_TABLE, headers={"ETag": '"v1"'})
    )
    for tick in range(3):
        await _negar_run(db_path, run_time + timedelta(minutes=30 * tick))

    assert missing.call_count == 1, "the season's 404 is remembered between runs"
    assert served.call_count == 3

    served.mock(return_value=httpx.Response(404))
    missing.mock(return_value=httpx.Response(200, text=NEGAR_TABLE))
    events = await _negar_run(db_path, run_time + timedelta(hours=2))

    assert missing.call_count == 2, "with the current file gone the season file is asked again"
    assert not [event for event in events if isinstance(event, CompanyFailed)]


@respx.mock
async def test_seasonal_feed_reports_a_status_that_is_not_a_missing_file(
    run_time: datetime,
) -> None:
    feed = NegarFeed()
    seasonal, current = feed.plan(run_time)
    respx.get(seasonal).mock(return_value=httpx.Response(410))
    fallback = respx.get(current).mock(return_value=httpx.Response(200, text=""))
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        with pytest.raises(HttpStatusError):
            await feed.fetch(client, run_time)

    assert not fallback.called, "a status other than 404 must not fall through to the next url"


@respx.mock
async def test_a_feed_that_declares_itself_empty_is_not_drift(run_time: datetime) -> None:
    feed = HanziliFeed()
    respx.get(feed.plan(run_time)[0]).mock(
        return_value=httpx.Response(
            200,
            text=(
                "# 2026 Canadian CS Internship Positions\n\n"
                "## Software Engineering\n_No roles currently_\n\n"
                "## Data / ML\n_No roles currently_\n"
            ),
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert result.jobs == (), "the publisher lists no roles, so the feed carries none"
    assert not result.authoritative, "an empty feed must never close what it cannot see"
    assert "no roles right now" in result.degraded
    assert result.declared_empty


@respx.mock
async def test_a_feed_that_loses_its_table_still_raises(run_time: datetime) -> None:
    feed = HanziliFeed()
    respx.get(feed.plan(run_time)[0]).mock(
        return_value=httpx.Response(
            200,
            text=(
                "# 2026 Canadian CS Internship Positions\n\n"
                "## Software Engineering\n\nRoles have moved to postings.json.\n"
            ),
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        with pytest.raises(PayloadValidationError):
            await feed.fetch(client, run_time)


ZAPPLY = (
    "| Company | Role | Location | Posted | Visa | **Apply** |\n"
    "|---------|------|----------|--------|------|----------|\n"
    '| **Acme** | Software Engineer Intern | Montreal, QC | 1h | | [<img src="images/apply.png" '
    'width="80" alt="Apply">](https://zapply.jobs/l/d/greenhouse-acme-1?s=gh-internships-2027) |\n'
    "| **Acme** | Software Developer I (Full Time) | Toronto, ON | 2d | | "
    "[Apply](https://zapply.jobs/l/d/greenhouse-acme-2) |\n"
)


def test_zapply_lists_read_each_table_row_with_its_apply_link() -> None:
    from stage.sources.curated_markdown import ZapplyCanadaFeed, ZapplyFeed

    assert {"zapply", "zapply-canada"} <= set(get_feeds())
    rows, malformed = ZapplyFeed().rows(ZAPPLY)
    assert malformed == 0
    assert [(row.company, row.title, row.location) for row in rows][0] == (
        "Acme",
        "Software Engineer Intern",
        "Montreal, QC",
    )
    assert rows[0].url == "https://job-boards.greenhouse.io/acme/jobs/1"
    plan = ZapplyCanadaFeed().plan(datetime(2026, 11, 1, tzinfo=UTC))
    assert plan[0] == (
        "https://raw.githubusercontent.com/zapplyjobs/Canada-Internships-2027/main/README.md"
    )


@pytest.mark.parametrize(
    ("link", "direct"),
    [
        (
            "https://zapply.jobs/l/d/lever-fullscript-f2682bc2-454c-4bcd-b71c-639236d09a8e?s=x",
            "https://jobs.lever.co/fullscript/f2682bc2-454c-4bcd-b71c-639236d09a8e",
        ),
        (
            "https://zapply.jobs/l/d/ashby-base-power-0594dd17-1291-4802-90fb-14e0d775d2d6",
            "https://jobs.ashbyhq.com/base-power/0594dd17-1291-4802-90fb-14e0d775d2d6",
        ),
        (
            "https://zapply.jobs/l/d/workday-cisco-cisco-careers-2024136?s=x",
            "https://zapply.jobs/l/d/workday-cisco-cisco-careers-2024136?s=x",
        ),
        ("https://jobs.example/acme", "https://jobs.example/acme"),
    ],
)
def test_a_zapply_link_opens_the_employer_board_when_it_names_one(link: str, direct: str) -> None:
    from stage.sources.curated_markdown import _zapply_direct

    assert _zapply_direct(link) == direct


def test_the_canada_list_mixes_in_full_time_jobs_so_the_title_must_say_intern() -> None:
    from stage.classify.scope import screen_is_internship
    from stage.sources.curated_markdown import ZapplyCanadaFeed

    rows, _ = ZapplyCanadaFeed().rows(ZAPPLY)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    intern, full_time = (ZapplyCanadaFeed()._to_job(row, now) for row in rows)
    assert screen_is_internship(intern) is None
    assert screen_is_internship(full_time) is not None


@respx.mock
async def test_a_new_season_without_its_list_yet_reads_last_seasons_list(
    run_time: datetime,
) -> None:
    feed = NegarFeed()
    for url in feed.plan(run_time):
        respx.get(url).mock(return_value=httpx.Response(404))
    seasonal, current = feed.fallback(run_time)
    respx.get(seasonal).mock(return_value=httpx.Response(404))
    respx.get(current).mock(return_value=httpx.Response(200, text=NEGAR_TABLE))
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert result.jobs


@respx.mock
async def test_zshah_reads_last_seasons_list_until_the_new_one_exists(run_time: datetime) -> None:
    feed = ZshahFeed()
    upcoming, last = feed.plan(run_time)
    respx.get(upcoming).mock(return_value=httpx.Response(404))
    respx.get(last).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "one",
                        "company": "Acme",
                        "title": "Software Engineer Intern",
                        "url": "https://jobs.example/one",
                        "program": "Internship",
                    }
                ]
            },
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert len(result.jobs) == 1
    assert "Fall-" in upcoming and upcoming != last
