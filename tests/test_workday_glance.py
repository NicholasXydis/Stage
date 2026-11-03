import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import FULL_READ_PREFIX, Company, DetailFetch, Platform, WorkdayCrawl
from stage.http import HttpClient, RatePosture
from stage.services.sync import _full_reads_due
from stage.sources.base import FetchResult
from stage.sources.workday import (
    CRAWL_PAGE_CAP,
    PAGE_SIZE,
    WorkdayAdapter,
    full_read_day,
)
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
CXS = "https://big.wd5.myworkdayjobs.com/wday/cxs/big/careers/jobs"
UNPACED = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=500)
BOARD = "workday:big-careers"
NO_INTERN = [
    {
        "facetParameter": "timeType",
        "descriptor": "Time Type",
        "values": [{"id": "f-full", "descriptor": "Full time", "count": 900}],
    }
]


def _company() -> Company:
    return Company(
        name="Big",
        platform=Platform.WORKDAY,
        slug="big",
        workday_tenant="big",
        workday_site="careers",
        workday_dc="wd5",
    )


def _label(index: int, french: bool, shuffled: bool = False) -> str:
    days = (index * 7) % 5 if shuffled else index // 40
    if french:
        return "Publié aujourd'hui" if days == 0 else f"Publié il y a {days} jours"
    return "Posted Today" if days == 0 else f"Posted {days} Days Ago"


def _serve(
    total: int, *, shuffled: bool = False, french: bool = False, grows_at: int | None = None
) -> list[int]:
    offsets: list[int] = []

    def page(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["searchText"]:
            return httpx.Response(200, json={"total": 0, "jobPostings": []})
        offset = int(body["offset"])
        offsets.append(offset)
        reported = total + 1 if grows_at is not None and offset >= grows_at else total
        rows = [
            {
                "title": f"Analyst {index}",
                "externalPath": f"/job/Toronto/Analyst_R{index:05d}",
                "locationsText": "Toronto, ON",
                "postedOn": _label(index, french, shuffled),
                "bulletFields": [f"R{index:05d}"],
            }
            for index in range(offset, min(offset + PAGE_SIZE, total))
        ]
        return httpx.Response(
            200, json={"total": reported, "jobPostings": rows, "facets": NO_INTERN}
        )

    respx.post(CXS).mock(side_effect=page)
    return offsets


async def _fetch(**options: object) -> FetchResult:
    async with HttpClient(
        allowed_hosts=frozenset({"big.wd5.myworkdayjobs.com"}),
        posture=UNPACED,
        bucket_key="workday",
        jitter=False,
    ) as client:
        return await WorkdayAdapter().fetch(_company(), client, NOW, **options)  # type: ignore[arg-type]


def _marker(result: FetchResult) -> DetailFetch | None:
    return next(
        (
            entry
            for entry in result.detail_fetches
            if isinstance(entry, DetailFetch) and entry.id == f"{FULL_READ_PREFIX}{BOARD}"
        ),
        None,
    )


@respx.mock
async def test_a_huge_unfiltered_board_newest_first_gets_a_one_page_quick_look() -> None:
    offsets = _serve(900)
    result = await _fetch(page_budget=CRAWL_PAGE_CAP)
    assert offsets == [0]
    assert not result.authoritative
    assert result.workday_crawl is None, "a quick look leaves no crawl behind"
    assert "weekly full read" in result.degraded
    marker = _marker(result)
    assert marker is not None and not marker.resolved and not marker.failed


@respx.mock
async def test_french_dates_count_as_newest_first() -> None:
    offsets = _serve(900, french=True)
    await _fetch(page_budget=CRAWL_PAGE_CAP)
    assert offsets == [0]


@respx.mock
async def test_a_board_not_sorted_by_date_gets_a_deeper_look() -> None:
    offsets = _serve(900, shuffled=True)
    result = await _fetch(page_budget=CRAWL_PAGE_CAP)
    assert len(offsets) == CRAWL_PAGE_CAP
    assert not result.authoritative


@respx.mock
async def test_a_known_board_is_not_marked_again_between_full_reads() -> None:
    _serve(900)
    result = await _fetch(page_budget=CRAWL_PAGE_CAP, size_known=True)
    assert _marker(result) is None


@respx.mock
async def test_the_weekly_full_read_walks_the_whole_board_and_may_close() -> None:
    offsets = _serve(300)
    result = await _fetch(page_budget=20, full_read=True, size_known=True)
    assert offsets == list(range(0, 300, PAGE_SIZE))
    assert len(result.jobs) == 300
    assert result.authoritative
    marker = _marker(result)
    assert marker is not None and marker.resolved and not marker.failed


@respx.mock
async def test_a_total_that_changes_mid_read_closes_nothing_and_retries() -> None:
    _serve(300, grows_at=100)
    result = await _fetch(page_budget=20, full_read=True, size_known=True)
    assert not result.authoritative
    marker = _marker(result)
    assert marker is not None and marker.failed


@respx.mock
async def test_a_budget_too_small_for_the_whole_board_waits_for_the_next_day() -> None:
    offsets = _serve(600)
    result = await _fetch(page_budget=CRAWL_PAGE_CAP, full_read=True, size_known=True)
    assert offsets == [0]
    marker = _marker(result)
    assert marker is not None and marker.failed and not marker.resolved


@respx.mock
async def test_a_board_over_two_thousand_postings_never_tries_a_full_read() -> None:
    offsets = _serve(2500)
    result = await _fetch(page_budget=100, full_read=True, size_known=True)
    assert offsets == [0]
    marker = _marker(result)
    assert marker is not None and not marker.failed and not marker.resolved


@respx.mock
async def test_a_total_of_two_thousand_is_workdays_cap_not_the_boards_size() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["searchText"]:
            return httpx.Response(200, json={"total": 0, "jobPostings": []})
        offset = int(body["offset"])
        offsets.append(offset)
        start = offset if offset < 2000 else 0
        rows = [
            {"title": f"Analyst {index}", "bulletFields": [f"R{index:05d}"]}
            for index in range(start, start + PAGE_SIZE)
        ]
        return httpx.Response(200, json={"total": 2000, "jobPostings": rows, "facets": NO_INTERN})

    offsets: list[int] = []
    respx.post(CXS).mock(side_effect=page)
    result = await _fetch(page_budget=100, full_read=True, size_known=True)
    assert not result.authoritative, "Jabil holds more than the 2,000 it will list"
    assert max(offsets) < 2000, "past 2,000 Workday serves the first page again"


@respx.mock
async def test_an_old_unfiltered_crawl_is_dropped_for_a_quick_look_from_the_top() -> None:
    offsets = _serve(900)
    crawl = WorkdayCrawl(board=BOARD, next_offset=400, total=900)
    result = await _fetch(page_budget=CRAWL_PAGE_CAP, crawl=crawl)
    assert offsets == [0]
    step = result.workday_crawl
    assert step is not None and step.discard


def test_full_reads_land_on_the_boards_own_weekday_and_retry_after_a_failure() -> None:
    adapter = WorkdayAdapter()
    company = _company()
    day = full_read_day(BOARD)
    on_day = NOW + timedelta(days=(day - NOW.weekday()) % 7)
    off_day = on_day + timedelta(days=1)
    week_ago = on_day - timedelta(days=7)
    assert _full_reads_due(adapter, [company], {}, on_day) == []
    assert _full_reads_due(adapter, [company], {BOARD: (week_ago, True, False)}, on_day) == [
        company.registry_key
    ]
    assert _full_reads_due(adapter, [company], {BOARD: (week_ago, True, False)}, off_day) == []
    recent = on_day - timedelta(hours=3)
    assert _full_reads_due(adapter, [company], {BOARD: (recent, False, True)}, on_day) == []
    failed = off_day - timedelta(hours=21)
    assert _full_reads_due(adapter, [company], {BOARD: (failed, False, True)}, off_day) == [
        company.registry_key
    ]


async def test_full_read_state_round_trips_and_never_disturbs_description_fetches(
    tmp_path: Path,
) -> None:
    async with open_repository(tmp_path / "s.db") as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=NOW,
                detail_fetches=(
                    DetailFetch(id=f"{FULL_READ_PREFIX}{BOARD}", resolved=True),
                    DetailFetch(id="workday:big-careers:r1", resolved=True),
                ),
            )
        )
        states = await repository.full_reads("workday", FULL_READ_PREFIX)
    assert states == {BOARD: (NOW, True, False)}


def test_a_board_whose_search_cannot_be_trusted_gets_a_whole_read_every_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stage.services.sync as sync_module

    adapter = WorkdayAdapter()
    company = _company()
    monkeypatch.setattr(sync_module, "WHOLE_READ_BOARDS", frozenset({BOARD}))
    just_read = {BOARD: (NOW - timedelta(hours=1), True, False)}
    assert _full_reads_due(adapter, [company], {}, NOW) == [company.registry_key]
    assert _full_reads_due(adapter, [company], just_read, NOW) == [company.registry_key]


@pytest.mark.parametrize(
    ("label", "days"),
    [
        ("Posted Today", 0),
        ("Posted Yesterday", 1),
        ("Posted 3 Days Ago", 3),
        ("Posted 30+ Days Ago", 31),
        ("Publié hier", 1),
        ("Publié il y a plus de 30 jours", 31),
        ("Recently", None),
        ("", None),
    ],
)
def test_posting_ages_are_read_in_english_and_french(label: str, days: int | None) -> None:
    from stage.sources.workday import _age_in_days

    assert _age_in_days(label) == days


async def test_purge_drops_detail_rows_of_postings_that_are_gone(tmp_path: Path) -> None:
    import sqlite3

    from stage.domain import Job

    posting = Job(
        id="workday:big-careers:r1",
        source="workday",
        company="Big",
        title_raw="Software Intern",
        title_normalized="software intern",
        apply_url_raw="https://big.wd5.myworkdayjobs.com/careers/job/Toronto/Intern_R1",
        description="",
        first_seen=NOW,
        last_seen=NOW,
    )
    async with open_repository(tmp_path / "p.db") as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=NOW,
                jobs=(posting,),
                detail_fetches=(
                    DetailFetch(id=f"{FULL_READ_PREFIX}{BOARD}", resolved=True),
                    DetailFetch(id=posting.id, resolved=True),
                    DetailFetch(id="workday:gone-site:r9", resolved=True),
                ),
            )
        )
        await repository.purge(NOW)
    connection = sqlite3.connect(tmp_path / "p.db")
    kept = {row[0] for row in connection.execute("SELECT id FROM detail_fetches")}
    connection.close()
    assert kept == {f"{FULL_READ_PREFIX}{BOARD}", posting.id}


def test_a_pinned_facet_nested_inside_a_location_group_is_still_offered() -> None:
    from stage.domain import WorkdayFacet
    from stage.sources.workday import WorkdayPage, facet_still_offered

    page = WorkdayPage.model_validate(
        {
            "total": 154,
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "locationMainGroup",
                    "values": [
                        {
                            "facetParameter": "locationCountry",
                            "descriptor": "Location Country",
                            "values": [
                                {"id": "ca", "descriptor": "Canada", "count": 87},
                                {"id": "us", "descriptor": "United States", "count": 67},
                            ],
                        }
                    ],
                }
            ],
        }
    )
    pinned = WorkdayFacet(
        tenant="thales", site="Careers", parameter="locationCountry", facet_ids=("ca", "us")
    )
    assert facet_still_offered(page, pinned)
    gone = WorkdayFacet(
        tenant="thales", site="Careers", parameter="locationCountry", facet_ids=("fr",)
    )
    assert not facet_still_offered(page, gone)


@respx.mock
async def test_a_country_filter_does_not_make_every_posting_an_internship() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["appliedFacets"] == {"locationCountry": ["ca"]}
        row = {
            "title": "Software Developer",
            "externalPath": "/job/Ottawa/Software-Developer_R1",
            "locationsText": "Ottawa",
            "postedOn": "Posted Today",
            "bulletFields": ["R1"],
        }
        return httpx.Response(200, json={"total": 1, "jobPostings": [row], "facets": []})

    respx.post(CXS).mock(side_effect=page)
    company = Company(
        name="Big",
        platform=Platform.WORKDAY,
        slug="big",
        workday_tenant="big",
        workday_site="careers",
        workday_dc="wd5",
        workday_facet="locationCountry:ca",
    )
    async with HttpClient(
        allowed_hosts=frozenset({"big.wd5.myworkdayjobs.com"}),
        posture=UNPACED,
        bucket_key="workday",
        jitter=False,
    ) as client:
        result = await WorkdayAdapter().fetch(company, client, NOW)
    assert [job.signals.employment_type for job in result.jobs] == [""]
