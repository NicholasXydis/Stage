import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from stage.classify import screen_internship, screen_level
from stage.domain import SEARCH_PREFIX, Company, DetailFetch, JobLevel, Platform
from stage.http import BucketBlockedError, HttpClient, RatePosture
from stage.lexicon import fold, internship_lexicon, level_lexicon
from stage.services.sync import _searches_due
from stage.sources.base import FetchResult
from stage.sources.workday import (
    BROAD_TEXT,
    CRAWL_PAGE_CAP,
    NARROW_TEXT,
    NEW_GRAD_TERMS,
    NEW_GRAD_TEXT,
    PAGE_SIZE,
    SEARCH_PAGES,
    SEARCH_TERMS,
    SINGLE_TERMS,
    WorkdayAdapter,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
HOST = "big.wd5.myworkdayjobs.com"
CXS = f"https://{HOST}/wday/cxs/big/careers/jobs"
UNPACED = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=500)
BUDGET = CRAWL_PAGE_CAP + SEARCH_PAGES
NO_INTERN = [
    {
        "facetParameter": "timeType",
        "values": [{"id": "f-full", "descriptor": "Full time", "count": 900}],
    }
]
Answer = Callable[[int], httpx.Response]
Asked = list[tuple[str, int]]


def _company(tenant: str = "big") -> Company:
    return Company(
        name=tenant.title(),
        platform=Platform.WORKDAY,
        slug=tenant,
        workday_tenant=tenant,
        workday_site="careers",
        workday_dc="wd5",
    )


def _row(requisition: str, title: str) -> dict[str, object]:
    return {
        "title": title,
        "externalPath": f"/job/Toronto/{title.replace(' ', '-')}_{requisition}",
        "locationsText": "Toronto, ON",
        "postedOn": "Posted Today",
        "bulletFields": [requisition],
    }


def _rows(prefix: str, title: str, count: int) -> list[dict[str, object]]:
    return [_row(f"{prefix}{index:03d}", f"{title} {index}") for index in range(count)]


def _paged(rows: Sequence[dict[str, object]], total: int | None = None) -> Answer:
    def answer(offset: int) -> httpx.Response:
        page = list(rows[offset : offset + PAGE_SIZE])
        claimed = len(rows) if total is None else total
        return httpx.Response(200, json={"total": 0 if offset else claimed, "jobPostings": page})

    return answer


def _serve(answers: Mapping[str, Answer] | None = None) -> Asked:
    asked: Asked = []
    nothing = _paged([])

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text, offset = body["searchText"], int(body["offset"])
        asked.append((text, offset))
        if not text:
            newest = _rows("R", "Analyst", PAGE_SIZE)
            return httpx.Response(
                200, json={"total": 900, "jobPostings": newest, "facets": NO_INTERN}
            )
        assert text in (NARROW_TEXT, BROAD_TEXT, NEW_GRAD_TEXT, *SINGLE_TERMS)
        return (answers or {}).get(text, nothing)(offset)

    respx.post(CXS).mock(side_effect=answer)
    return asked


def _client() -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({HOST, "small.wd5.myworkdayjobs.com"}),
        posture=UNPACED,
        bucket_key="workday",
        jitter=False,
    )


async def _fetch(budget: int = BUDGET, **options: object) -> FetchResult:
    async with _client() as client:
        return await WorkdayAdapter().fetch(
            _company(),
            client,
            NOW,
            page_budget=budget,
            size_known=True,
            **options,  # type: ignore[arg-type]
        )


def _kept(result: FetchResult) -> list[str]:
    return [job.title_raw for job in result.jobs if screen_internship(job.title_raw).is_internship]


def _offsets(asked: Asked, text: str) -> list[int]:
    return [offset for asked_text, offset in asked if asked_text == text]


def test_every_internship_marker_is_named_by_a_search_term() -> None:
    terms = [fold(term) for term in SEARCH_TERMS]
    for marker in internship_lexicon().markers:
        assert any(f" {term}" in f" {fold(marker)}" for term in terms), marker
    assert len(NARROW_TEXT.split(" OR ")) <= 10, "Workday drops matches past 10 clauses"
    assert len(BROAD_TEXT.split(" OR ")) <= 10, "Workday drops matches past 10 clauses"


@respx.mock
async def test_a_search_that_finds_nothing_leaves_the_quick_look() -> None:
    asked = _serve()
    result = await _fetch()
    assert [text for text, _ in asked if text] == [NARROW_TEXT, BROAD_TEXT]
    assert len(result.jobs) == PAGE_SIZE
    assert not result.authoritative
    assert "searched it for internships" in result.degraded


@respx.mock
async def test_a_board_whose_search_ignores_or_says_so() -> None:
    _serve()
    result = await _fetch()
    assert "ignores OR" in result.degraded


@respx.mock
async def test_the_narrow_search_walks_every_page() -> None:
    asked = _serve({NARROW_TEXT: _paged(_rows("S", "Summer Associate", 45))})
    result = await _fetch()
    assert _offsets(asked, NARROW_TEXT) == [0, 20, 40]
    assert len(_kept(result)) == 45


@respx.mock
async def test_the_broad_search_stops_after_two_pages_naming_no_term() -> None:
    rows = _rows("I", "Data Intern", 20) + _rows("D", "Accountant", 200)
    asked = _serve({BROAD_TEXT: _paged(rows)})
    result = await _fetch()
    assert _offsets(asked, BROAD_TEXT) == [0, 20, 40]
    assert len(_kept(result)) == 20


@respx.mock
async def test_one_page_naming_no_term_does_not_stop_the_broad_search() -> None:
    rows = _rows("I", "Data Intern", 20) + _rows("D", "Accountant", 20) + _rows("J", "Intern", 5)
    asked = _serve({BROAD_TEXT: _paged(rows)})
    result = await _fetch()
    assert _offsets(asked, BROAD_TEXT) == [0, 20, 40]
    assert len(_kept(result)) == 25


@respx.mock
async def test_a_posting_found_by_a_search_and_the_quick_look_is_kept_once() -> None:
    both = [_row("R000", "Analyst 0"), _row("I1", "Data Intern")]
    _serve({NARROW_TEXT: _paged(both), BROAD_TEXT: _paged(both)})
    result = await _fetch()
    assert len(result.jobs) == PAGE_SIZE + 1
    assert len({job.id for job in result.jobs}) == len(result.jobs)


@respx.mock
async def test_internal_and_international_matches_are_screened_out() -> None:
    rows = [
        _row("A1", "Internal Auditor"),
        _row("A2", "International Tax Analyst"),
        _row("A3", "Software Engineering Intern"),
    ]
    _serve({BROAD_TEXT: _paged(rows)})
    result = await _fetch()
    assert _kept(result) == ["Software Engineering Intern"]


@respx.mock
async def test_french_stage_and_stagiaire_postings_are_found() -> None:
    stagiaire = [_row("F2", "Stagiaire en technologies de l'information")]
    stage = [_row("F1", "Stage - Développeur logiciel (hiver 2027)")]
    _serve({NARROW_TEXT: _paged(stagiaire), BROAD_TEXT: _paged(stage)})
    result = await _fetch()
    assert sorted(_kept(result)) == sorted(str(row["title"]) for row in stage + stagiaire)


@respx.mock
async def test_a_search_failing_midway_still_lands_the_quick_look() -> None:
    interns = _rows("I", "Data Intern", 20)

    def flaky(offset: int) -> httpx.Response:
        return _paged(interns, total=40)(offset) if not offset else httpx.Response(404)

    asked = _serve({BROAD_TEXT: flaky})
    result = await _fetch()
    assert _offsets(asked, BROAD_TEXT) == [0, 20]
    assert len(result.jobs) == PAGE_SIZE + 20
    assert "internship search failed" in result.degraded
    assert not result.authoritative


@respx.mock
async def test_a_block_during_the_search_stops_it_and_holds_the_bucket() -> None:
    asked = _serve({NARROW_TEXT: lambda _: httpx.Response(429, headers={"Retry-After": "3600"})})
    async with _client() as client:
        result = await WorkdayAdapter().fetch(
            _company(), client, NOW, page_budget=BUDGET, size_known=True
        )
        with pytest.raises(BucketBlockedError):
            await WorkdayAdapter().fetch(_company("small"), client, NOW)
    assert [text for text, _ in asked if text] == [NARROW_TEXT]
    assert len(result.jobs) == PAGE_SIZE
    assert "internship search failed" in result.degraded


@respx.mock
async def test_the_search_stops_when_the_board_runs_out_of_pages() -> None:
    asked = _serve({BROAD_TEXT: _paged(_rows("I", "Data Intern", 200))})
    result = await _fetch(budget=3)
    assert len(asked) == 3
    assert "stopped after 2 pages" in result.degraded


@respx.mock
async def test_the_weekly_whole_read_needs_no_search() -> None:
    def whole(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert not body["searchText"]
        offset = int(body["offset"])
        rows = _rows("R", "Analyst", 300)[offset : offset + PAGE_SIZE]
        return httpx.Response(200, json={"total": 300, "jobPostings": rows, "facets": NO_INTERN})

    respx.post(CXS).mock(side_effect=whole)
    result = await _fetch(budget=20, full_read=True)
    assert len(result.jobs) == 300
    assert result.authoritative


def test_a_known_huge_board_gets_pages_for_its_searches() -> None:
    huge, small = _company(), _company("small")
    budgets, _ = WorkdayAdapter.crawl_budgets(
        [huge, small], {}, {}, 500, glances=[huge.registry_key]
    )
    assert budgets[huge.registry_key] == CRAWL_PAGE_CAP + SEARCH_PAGES
    assert budgets[small.registry_key] == CRAWL_PAGE_CAP


@respx.mock
async def test_a_board_searched_this_window_gets_only_its_quick_look() -> None:
    asked = _serve()
    result = await _fetch(search=False)
    assert [text for text, _ in asked if text] == []
    assert "searched for internships within 5.5 hours" in result.degraded
    assert not any(
        isinstance(entry, DetailFetch) and entry.id.startswith(SEARCH_PREFIX)
        for entry in result.detail_fetches
    )


@respx.mock
async def test_a_finished_search_is_remembered_and_a_failed_one_is_not() -> None:
    _serve({BROAD_TEXT: _paged(_rows("I", "Data Intern", 3))})
    done = await _fetch()
    assert DetailFetch(id=f"{SEARCH_PREFIX}workday:big-careers") in done.detail_fetches
    respx.reset()
    _serve({NARROW_TEXT: lambda _: httpx.Response(404)})
    failed = await _fetch()
    assert not any(
        isinstance(entry, DetailFetch) and entry.id.startswith(SEARCH_PREFIX)
        for entry in failed.detail_fetches
    )


def test_searches_come_due_once_per_quiet_window() -> None:
    adapter = WorkdayAdapter()
    fresh, stale, never = _company("fresh"), _company("stale"), _company("never")
    searched = {
        "workday:fresh-careers": (NOW - timedelta(hours=2), False, False),
        "workday:stale-careers": (NOW - timedelta(hours=5.5), False, False),
    }
    due = _searches_due(adapter, [fresh, stale, never], searched, NOW)
    assert due == [stale.registry_key, never.registry_key]


@respx.mock
async def test_a_listed_board_too_big_to_read_whole_is_searched_to_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stage.sources.workday.WHOLE_READ_BOARDS", frozenset({"workday:big-careers"})
    )
    rows = _rows("I", "Data Intern", 5) + _rows("D", "Accountant", 300) + _rows("J", "Intern", 5)
    asked = _serve({BROAD_TEXT: _paged(rows)})
    result = await _fetch(budget=40, full_read=True)
    assert len(_offsets(asked, BROAD_TEXT)) == 16, "Jabil's results are not ranked"
    assert len(_kept(result)) == 10


def test_every_whole_read_board_is_a_shipped_workday_board() -> None:
    from stage.companies import load_companies
    from stage.sources.workday import WHOLE_READ_BOARDS

    adapter = WorkdayAdapter()
    shipped = {
        adapter.board_key(company)
        for company in load_companies()
        if company.platform is Platform.WORKDAY and company.enabled
    }
    assert sorted(WHOLE_READ_BOARDS - shipped) == []


@respx.mock
async def test_a_listed_board_that_ignores_or_is_searched_one_word_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stage.sources.workday.WHOLE_READ_BOARDS", frozenset({"workday:big-careers"})
    )
    interns = _rows("I", "Store Operations Internship", 25)
    stage = _rows("S", "Stage Manager", 0) + _rows("D", "Associate", 4660)
    asked = _serve({"internship": _paged(interns), "stage": _paged(stage)})
    result = await _fetch(budget=40, full_read=True)
    searched = [text for text, _ in asked if text]
    assert searched[:2] == [NARROW_TEXT, BROAD_TEXT], "Lowe's answers 0 to every OR"
    assert set(searched[2:]) == set(SINGLE_TERMS)
    assert searched.count("stage") == 2, "Lowe's matches stage in 4,660 descriptions"
    assert len(_kept(result)) == 25
    assert "ignores OR" not in result.degraded


def test_spare_pages_go_to_boards_without_a_facet_first() -> None:
    from stage.domain import WorkdayFacet

    faceted = [_company(f"f{index}") for index in range(20)]
    bare = _company("bare")
    facets = {
        (company.workday_tenant or "", "careers"): WorkdayFacet(
            tenant=company.workday_tenant or "", site="careers", parameter="p", facet_ids=("i",)
        )
        for company in faceted
    }
    ceiling = WorkdayAdapter.retry_reserve + 21 + 5
    budgets, _ = WorkdayAdapter.crawl_budgets([*faceted, bare], {}, facets, ceiling)
    assert budgets[bare.registry_key] == CRAWL_PAGE_CAP, "a faceted board rarely needs page 2"


def test_every_new_grad_search_word_is_a_level_marker_and_the_text_fits_one_query() -> None:
    lexicon = level_lexicon()
    markers = lexicon.new_grad | lexicon.entry
    for term in NEW_GRAD_TERMS:
        assert fold(term) in markers, term
    assert len(NEW_GRAD_TEXT.split(" OR ")) <= 10, "Workday drops matches past 10 clauses"
    assert not set(NEW_GRAD_TERMS) & set(SEARCH_TERMS), "the internship search keeps its own words"
    assert NEW_GRAD_TEXT not in (NARROW_TEXT, BROAD_TEXT)


@respx.mock
async def test_a_board_whose_or_works_is_then_searched_for_new_grad_roles() -> None:
    asked = _serve(
        {
            BROAD_TEXT: _paged(_rows("I", "Data Intern", 3)),
            NEW_GRAD_TEXT: _paged(
                [
                    _row("G1", "Software Engineer, New Grad"),
                    _row("G2", "Junior Developer"),
                    _row("G3", "Senior Software Engineer"),
                ]
            ),
        }
    )
    result = await _fetch()

    assert [text for text, _ in asked if text] == [NARROW_TEXT, BROAD_TEXT, NEW_GRAD_TEXT]
    levels = {job.title_raw: screen_level(job.title_raw).level for job in result.jobs}
    assert levels["Software Engineer, New Grad"] is JobLevel.NEW_GRAD
    assert levels["Junior Developer"] is JobLevel.ENTRY
    assert levels["Senior Software Engineer"] is JobLevel.UNKNOWN
    assert {job.signals.employment_type for job in result.jobs} == {""}
    assert "internships and new-grad roles" in result.degraded
    assert DetailFetch(id=f"{SEARCH_PREFIX}workday:big-careers") in result.detail_fetches


@respx.mock
async def test_a_new_grad_search_that_finds_nothing_costs_one_request() -> None:
    asked = _serve({BROAD_TEXT: _paged(_rows("I", "Data Intern", 3))})
    await _fetch()

    assert _offsets(asked, NEW_GRAD_TEXT) == [0]
    assert len([text for text, _ in asked if text]) == 3


@respx.mock
async def test_a_board_that_ignores_or_is_not_asked_a_new_grad_or() -> None:
    asked = _serve()
    result = await _fetch()

    assert NEW_GRAD_TEXT not in [text for text, _ in asked]
    assert "new-grad" not in result.degraded


@respx.mock
async def test_the_new_grad_search_stops_after_two_pages_naming_no_term_even_on_a_whole_read_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stage.sources.workday.WHOLE_READ_BOARDS", frozenset({"workday:big-careers"})
    )
    rows = _rows("G", "New Grad Engineer", 20) + _rows("D", "Accountant", 400)
    asked = _serve({BROAD_TEXT: _paged(_rows("I", "Data Intern", 3)), NEW_GRAD_TEXT: _paged(rows)})
    await _fetch(budget=40, full_read=True)

    assert _offsets(asked, NEW_GRAD_TEXT) == [0, 20, 40]


@respx.mock
async def test_the_new_grad_search_never_takes_pages_the_internship_searches_need() -> None:
    asked = _serve(
        {
            NARROW_TEXT: _paged(_rows("S", "Summer Associate", 3)),
            BROAD_TEXT: _paged(_rows("I", "Data Intern", 3)),
            NEW_GRAD_TEXT: _paged(_rows("G", "New Grad Engineer", 200)),
        }
    )
    result = await _fetch(budget=3)

    assert [text for text, _ in asked if text] == [NARROW_TEXT, BROAD_TEXT]
    assert "The internship search" not in result.degraded
    assert "The new-grad search did not run, since the internship searches used" in result.degraded
    assert "searched it for internships, since" in result.degraded, "no new-grad query was sent"
    assert "new-grad roles" not in result.degraded.split("The new-grad search")[0]
    assert "next sync" not in result.degraded
    assert DetailFetch(id=f"{SEARCH_PREFIX}workday:big-careers") in result.detail_fetches


@respx.mock
async def test_a_new_grad_search_cut_short_is_not_repeated_every_sync() -> None:
    asked = _serve(
        {
            BROAD_TEXT: _paged(_rows("I", "Data Intern", 3)),
            NEW_GRAD_TEXT: _paged(_rows("G", "New Grad Engineer", 200)),
        }
    )
    result = await _fetch(budget=4)

    assert _offsets(asked, NEW_GRAD_TEXT) == [0]
    assert "The new-grad search stopped after 3 pages" in result.degraded
    assert "searched it for internships, since" in result.degraded, "the new-grad query was cut"
    assert "next sync" not in result.degraded
    assert "The internship search" not in result.degraded
    assert DetailFetch(id=f"{SEARCH_PREFIX}workday:big-careers") in result.detail_fetches


@respx.mock
async def test_a_new_grad_search_that_fails_keeps_what_the_internship_searches_found() -> None:
    interns = _rows("I", "Data Intern", 3)
    asked = _serve({BROAD_TEXT: _paged(interns), NEW_GRAD_TEXT: lambda _: httpx.Response(404)})
    result = await _fetch()

    assert _offsets(asked, NEW_GRAD_TEXT) == [0]
    assert len(_kept(result)) == 3
    assert "The new-grad search failed" in result.degraded
    assert "searched it for internships, since" in result.degraded
    assert "The internship search" not in result.degraded
    assert DetailFetch(id=f"{SEARCH_PREFIX}workday:big-careers") in result.detail_fetches


@respx.mock
async def test_a_failed_internship_search_still_skips_the_new_grad_search() -> None:
    asked = _serve({BROAD_TEXT: lambda _: httpx.Response(404)})
    result = await _fetch()

    assert [text for text, _ in asked if text] == [NARROW_TEXT, BROAD_TEXT]
    assert "internship search failed" in result.degraded
    assert not any(
        isinstance(entry, DetailFetch) and entry.id.startswith(SEARCH_PREFIX)
        for entry in result.detail_fetches
    )


@respx.mock
async def test_a_board_with_an_internship_facet_is_never_searched_for_new_grad_roles() -> None:
    facets = [
        {
            "facetParameter": "workerSubType",
            "values": [{"id": "f-intern", "descriptor": "Intern", "count": 2}],
        }
    ]
    asked: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        rows = _rows("I", "Data Intern", 2) if body["appliedFacets"] else _rows("R", "Analyst", 20)
        return httpx.Response(200, json={"total": len(rows), "jobPostings": rows, "facets": facets})

    respx.post(CXS).mock(side_effect=answer)
    result = await _fetch()

    assert {body["searchText"] for body in asked} == {""}
    assert {job.signals.employment_type for job in result.jobs} == {"internship"}
