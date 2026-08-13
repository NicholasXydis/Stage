import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx

from stage.domain import (
    Company,
    DetailFetch,
    Platform,
    WorkdayCrawl,
    WorkdayCrawlStep,
    WorkdayFacet,
)
from stage.http import HttpClient, RatePosture, profile
from stage.sources.base import PayloadValidationError
from stage.sources.workday import (
    MAX_PAGES,
    PAGE_SIZE,
    WorkdayAdapter,
    WorkdayPage,
    facet_still_offered,
    resolve_facet,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
CXS = "https://cae.wd3.myworkdayjobs.com/wday/cxs/cae/career/jobs"


def _company(**kwargs: object) -> Company:
    base: dict[str, object] = {
        "name": "CAE",
        "platform": Platform.WORKDAY,
        "slug": "cae",
        "workday_tenant": "cae",
        "workday_site": "career",
        "workday_dc": "wd3",
    }
    base.update(kwargs)
    return Company(**base)  # type: ignore[arg-type]


def _posting(index: int, title: str = "Software Engineer Intern") -> dict[str, object]:
    return {
        "title": title,
        "externalPath": f"/job/Montreal/{title.replace(' ', '-')}_R{index:05d}",
        "locationsText": "Montréal, Quebec, Canada",
        "postedOn": "Posted 3 Days Ago",
        "bulletFields": [f"R{index:05d}"],
    }


def _facets(intern_id: str = "f-intern") -> list[dict[str, object]]:
    return [
        {
            "facetParameter": "timeType",
            "descriptor": "Time Type",
            "values": [
                {"id": "f-full", "descriptor": "Full time", "count": 400},
                {"id": intern_id, "descriptor": "Intern", "count": 12},
            ],
        }
    ]


def _client(posture: RatePosture | None = None) -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({"cae.wd3.myworkdayjobs.com"}),
        posture=posture or profile("workday"),
        bucket_key="workday",
        jitter=False,
    )


UNPACED = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=500)


def test_all_tenants_share_one_bucket_regardless_of_hostname() -> None:
    assert WorkdayAdapter.bucket_key == "workday"
    assert WorkdayAdapter.hosts == frozenset(), (
        "the allow-list is derived per run from registry rows, not a class constant"
    )


def test_the_allow_list_is_built_from_the_rows_being_synced() -> None:
    adapter = WorkdayAdapter()
    rows = [_company(), _company(name="Adobe", workday_tenant="adobe", workday_dc="wd5")]
    assert adapter.hosts_for(rows) == frozenset(
        {"cae.wd3.myworkdayjobs.com", "adobe.wd5.myworkdayjobs.com"}
    )


def test_one_malformed_row_does_not_take_the_platform_offline() -> None:
    adapter = WorkdayAdapter()
    rows = [_company(), _company(name="Evil", workday_tenant="evil.com/../x")]
    assert adapter.hosts_for(rows) == frozenset({"cae.wd3.myworkdayjobs.com"})


def test_the_crawl_budget_reserves_space_for_retries() -> None:
    posture = profile("workday")
    pages, details = WorkdayAdapter.crawl_budget(14, posture.max_requests_per_run)
    assert (pages, details) == (5, 30)
    assert 14 * pages + details + WorkdayAdapter.retry_reserve <= posture.max_requests_per_run
    assert WorkdayAdapter.crawl_budget(100, posture.max_requests_per_run) == (1, 0)


def test_a_facet_is_resolved_from_the_responses_own_facet_list() -> None:
    page = WorkdayPage.model_validate({"total": 12, "jobPostings": [], "facets": _facets()})
    facet = resolve_facet(page, "cae", "career", NOW)
    assert facet is not None
    assert facet.parameter == "timeType"
    assert facet.facet_ids == ("f-intern",)
    assert facet.descriptor == "Intern"


def test_a_french_facet_value_resolves_too() -> None:
    page = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "timeType",
                    "values": [{"id": "f-st", "descriptor": "Stagiaire"}],
                }
            ],
        }
    )
    facet = resolve_facet(page, "cae", "career", NOW)
    assert facet is not None and facet.facet_ids == ("f-st",)


def test_student_resolves_because_canadian_employers_use_it_for_intern() -> None:
    page = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "workerSubType",
                    "values": [{"id": "f-s", "descriptor": "Student"}],
                }
            ],
        }
    )
    facet = resolve_facet(page, "cae", "career", NOW)
    assert facet is not None and facet.parameter == "workerSubType"


def test_a_tenant_with_no_internship_facet_resolves_to_nothing() -> None:
    page = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "timeType",
                    "values": [{"id": "f-full", "descriptor": "Full time"}],
                }
            ],
        }
    )
    assert resolve_facet(page, "cae", "career", NOW) is None


def test_time_type_outranks_a_job_family_that_merely_mentions_internship() -> None:
    page = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "jobFamily",
                    "values": [{"id": "f-jf", "descriptor": "Intern"}],
                },
                {
                    "facetParameter": "timeType",
                    "values": [{"id": "f-tt", "descriptor": "Intern"}],
                },
            ],
        }
    )
    facet = resolve_facet(page, "cae", "career", NOW)
    assert facet is not None and facet.facet_ids == ("f-tt",)


def test_staleness_is_the_facet_vanishing_never_a_zero_result_count() -> None:

    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("f-intern",)
    )

    empty_but_valid = WorkdayPage.model_validate(
        {"total": 0, "jobPostings": [], "facets": _facets()}
    )
    assert facet_still_offered(empty_but_valid, cached), (
        "an empty roster with the facet still advertised is not a stale facet"
    )

    reconfigured = WorkdayPage.model_validate(
        {"total": 40, "jobPostings": [], "facets": _facets(intern_id="f-renamed")}
    )
    assert not facet_still_offered(reconfigured, cached)


@respx.mock
async def test_postings_map_to_jobs_with_the_requisition_as_identity() -> None:
    route = respx.post(CXS).mock(
        return_value=httpx.Response(
            200, json={"total": 2, "jobPostings": [_posting(1), _posting(2)], "facets": _facets()}
        )
    )
    async with _client() as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert route.called
    assert len(result.jobs) == 2
    first = result.jobs[0]
    assert first.source == "workday"
    assert first.company == "CAE"
    assert first.location_raw == "Montréal, Quebec, Canada"
    assert first.id.endswith("r00001")
    assert first.apply_url_raw.startswith("https://cae.wd3.myworkdayjobs.com/career/job/Montreal/")


@respx.mock
async def test_two_requisitions_under_one_title_stay_two_jobs() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [_posting(1), _posting(2)],
                "facets": _facets(),
            },
        )
    )
    async with _client() as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert len({job.id for job in result.jobs}) == 2


@respx.mock
async def test_pagination_walks_offsets_and_stops_on_the_reported_total() -> None:
    pages = [
        {"total": 25, "jobPostings": [_posting(i) for i in range(PAGE_SIZE)], "facets": _facets()},
        {"total": 25, "jobPostings": [_posting(100 + i) for i in range(5)], "facets": _facets()},
    ]
    route = respx.post(CXS).mock(side_effect=[httpx.Response(200, json=page) for page in pages])
    async with _client() as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert route.call_count == 2
    assert len(result.jobs) == 25
    assert not result.degraded


@respx.mock
async def test_a_server_ignoring_offset_cannot_loop_forever() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 999_999,
                "jobPostings": [_posting(i) for i in range(PAGE_SIZE)],
                "facets": _facets(),
            },
        )
    )
    async with _client(UNPACED) as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert len(result.jobs) == MAX_PAGES * PAGE_SIZE
    assert "cap" in result.degraded, "hitting the cap is reported, never silently truncated"


@respx.mock
async def test_a_malformed_total_terminates_rather_than_spinning() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 0,
                "jobPostings": [_posting(i) for i in range(PAGE_SIZE)],
                "facets": _facets(),
            },
        )
    )
    async with _client(UNPACED) as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert respx.calls.call_count == MAX_PAGES, "the page cap is the backstop, not the total"
    assert "cap" in result.degraded
    assert not result.authoritative


@respx.mock
async def test_a_changed_total_retains_resumable_crawl_progress() -> None:
    company = _company(workday_facet="timeType:f-intern")
    crawl = WorkdayCrawl(
        board=WorkdayAdapter().board_key(company),
        next_offset=20,
        total=40,
        facet_parameter="timeType",
        facet_ids=("f-intern",),
    )
    offsets: list[int] = []

    def page(request: httpx.Request) -> httpx.Response:
        offsets.append(int(json.loads(request.content)["offset"]))
        return httpx.Response(
            200,
            json={
                "total": 41,
                "jobPostings": [_posting(index) for index in range(PAGE_SIZE)],
                "facets": _facets(),
            },
        )

    respx.post(CXS).mock(side_effect=page)

    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(company, client, NOW, crawl=crawl, page_budget=1)

    step = cast(WorkdayCrawlStep, result.workday_crawl)
    assert not step.discard
    assert not step.complete
    assert (step.next_offset, step.total) == (40, 41)
    assert offsets == [20], (
        "one-page allocations advance instead of consuming their only page on overlap"
    )
    assert not result.authoritative
    assert "progress was retained" in result.degraded


@respx.mock
async def test_a_changed_total_at_the_end_defers_closures_and_starts_a_fresh_pass() -> None:
    company = _company(workday_facet="timeType:f-intern")
    crawl = WorkdayCrawl(
        board=WorkdayAdapter().board_key(company),
        next_offset=20,
        total=40,
        facet_parameter="timeType",
        facet_ids=("f-intern",),
    )
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [_posting(1)],
                "facets": _facets(),
            },
        )
    )

    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(company, client, NOW, crawl=crawl, page_budget=1)

    step = cast(WorkdayCrawlStep, result.workday_crawl)
    assert step.discard
    assert not step.complete
    assert not result.authoritative
    assert "next sync starts a fresh pass" in result.degraded


@respx.mock
async def test_resumable_crawl_reconciles_only_after_the_terminal_page(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import Job, JobStatus, job_id
    from stage.services import sync as sync_module
    from stage.storage import SourceBatch, open_repository

    company = _company(workday_facet="timeType:f-intern")
    offsets: list[int] = []

    def page(request: httpx.Request) -> httpx.Response:
        offset = int(json.loads(request.content)["offset"])
        offsets.append(offset)
        count = 5 if offset == 100 else PAGE_SIZE
        return httpx.Response(
            200,
            json={
                "total": 105,
                "jobPostings": [_posting(offset + index) for index in range(count)],
                "facets": _facets(),
            },
        )

    respx.post(CXS).mock(side_effect=page)
    monkeypatch.setattr(sync_module, "resolve", lambda *_: UNPACED)
    old = Job(
        id=job_id("workday", "cae-career", "R99999"),
        source="workday",
        company=company.name,
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw="https://example.test/old",
        description="",
        first_seen=NOW - timedelta(days=1),
        last_seen=NOW - timedelta(days=1),
    )
    later = NOW + timedelta(days=1)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="workday", run_started_at=old.last_seen, jobs=(old,))
        )
        async for _ in sync_module.sync(
            repository, [company], sources=("workday",), now_fn=lambda: NOW
        ):
            pass

        first_crawl = (await repository.load_workday_crawls())[WorkdayAdapter().board_key(company)]
        assert first_crawl.next_offset == 100
        before_completion = await repository.get_job(old.id)
        assert before_completion is not None and before_completion.status is JobStatus.OPEN

        async for _ in sync_module.sync(
            repository, [company], sources=("workday",), now_fn=lambda: later
        ):
            pass

        first = await repository.get_job(job_id("workday", "cae-career", "R00000"))
        closed = await repository.get_job(old.id)
        assert first is not None and first.last_seen == later
        assert closed is not None and closed.status is JobStatus.CLOSED
        assert await repository.load_workday_crawls() == {}

    assert offsets == [0, 20, 40, 60, 80, 80, 100]


@respx.mock
async def test_no_facet_falls_back_to_search_text_and_says_so() -> None:
    captured: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:

        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [_posting(1)],
                "facets": [
                    {
                        "facetParameter": "timeType",
                        "values": [{"id": "f-full", "descriptor": "Full time"}],
                    }
                ],
            },
        )

    respx.post(CXS).mock(side_effect=record)
    async with _client() as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW)

    assert captured[0]["appliedFacets"] == {}
    assert captured[0]["searchText"] == "", (
        "an English keyword cannot match stagiaire, stage or alternance"
    )
    assert "whole" in result.degraded


@respx.mock
async def test_a_pinned_facet_is_sent_and_never_overwritten_by_resolution() -> None:
    captured: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"total": 0, "jobPostings": [], "facets": _facets()})

    respx.post(CXS).mock(side_effect=record)
    async with _client() as client:
        company = _company(workday_facet="workerSubType:pinned-id")
        await WorkdayAdapter().fetch(company, client, NOW)

    assert captured[0]["appliedFacets"] == {"workerSubType": ["pinned-id"]}


@respx.mock
async def test_a_shape_change_fails_loudly_at_a_named_field() -> None:
    respx.post(CXS).mock(return_value=httpx.Response(200, json={"jobPostings": "not-a-list"}))
    async with _client() as client:
        with pytest.raises(PayloadValidationError, match="jobPostings"):
            company = _company(workday_facet="timeType:f-intern")
            await WorkdayAdapter().fetch(company, client, NOW)


@respx.mock
async def test_the_post_path_stores_no_validator() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={"total": 0, "jobPostings": [], "facets": _facets()},
            headers={"ETag": '"page-1"'},
        )
    )
    async with _client() as client:
        company = _company(workday_facet="timeType:f-intern")
        await WorkdayAdapter().fetch(company, client, NOW)
        assert client.cache.pending == {}


def test_the_facet_vocabulary_lives_in_data_lexicon_not_in_python() -> None:

    from stage.lexicon import workday_facet_lexicon
    from stage.paths import lexicon_dir

    root = lexicon_dir()
    assert (root / "workday_facets.yaml").exists()

    parameters, descriptors = workday_facet_lexicon()
    assert parameters[0] == "timeType"
    for term in ("stage etudiant", "alternance", "student", "stagiaire"):
        assert term in descriptors, f"{term} must be reachable without touching Python"


def test_a_registry_row_missing_a_field_says_what_to_do_about_it() -> None:
    from stage.sources.platforms import SlugRejectedError, workday_target

    with pytest.raises(SlugRejectedError, match=r"discover --url") as caught:
        workday_target("cae", "", "wd3")
    assert "workday_site" in str(caught.value)

    with pytest.raises(SlugRejectedError, match=r"not a usable Workday site"):
        workday_target("cae", "care er/../x", "wd3")


@respx.mock
async def test_one_unplannable_row_fails_alone_and_never_kills_the_source(
    db_path: "object", monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from stage.domain import CompanyFailed, CompanyFinished
    from stage.services import sync as sync_module
    from stage.storage import open_repository

    adapter = WorkdayAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.post(CXS).mock(
        return_value=httpx.Response(200, json={"total": 0, "jobPostings": [], "facets": _facets()})
    )

    rows = [_company(), _company(name="Incomplete", slug="broken", workday_site="")]

    async with open_repository(Path(str(db_path))) as repository:
        events = [event async for event in sync_module.sync(repository, rows, now_fn=lambda: NOW)]

    failed = [event for event in events if isinstance(event, CompanyFailed)]
    finished = [event for event in events if isinstance(event, CompanyFinished)]

    assert [event.company for event in finished] == ["CAE"], "the healthy row still ran"
    assert [event.company for event in failed] == ["Incomplete"]
    assert "workday_site" in failed[0].error
    assert "discover --url" in failed[0].error


@respx.mock
async def test_a_dry_run_reports_the_bad_row_instead_of_dying_on_it(
    db_path: "object", monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from stage.domain import CompanyFailed, PlannedRequest
    from stage.services import sync as sync_module
    from stage.storage import open_repository

    adapter = WorkdayAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    rows = [_company(), _company(name="Incomplete", slug="broken", workday_site="")]

    async with open_repository(Path(str(db_path))) as repository:
        events = [
            event
            async for event in sync_module.sync(repository, rows, dry_run=True, now_fn=lambda: NOW)
        ]

    assert [event.company for event in events if isinstance(event, PlannedRequest)] == ["CAE"]
    assert [event.company for event in events if isinstance(event, CompanyFailed)] == ["Incomplete"]


def test_the_dry_run_bound_is_reported_per_bucket_against_that_buckets_ceiling() -> None:
    from stage.http import profile
    from stage.services.sync import _bucket_plans

    plans = _bucket_plans(
        [
            (
                "workday",
                "workday",
                14,
                100,
            )
        ],
        {"workday": profile("workday")},
    )
    assert len(plans) == 1
    plan = plans[0]
    assert plan.planned == 14
    assert plan.worst_case == 100
    assert plan.ceiling == 120
    assert not plan.exceeds_ceiling


def test_sources_sharing_a_bucket_have_their_bounds_summed_not_reported_separately() -> None:
    from stage.http import profile
    from stage.services.sync import _bucket_plans

    plans = _bucket_plans(
        [
            ("raw.githubusercontent.com", "simplify", 1, 1),
            ("raw.githubusercontent.com", "vanshb03", 2, 2),
        ],
        {"raw.githubusercontent.com": profile("feeds")},
    )
    assert len(plans) == 1
    assert plans[0].sources == ("simplify", "vanshb03")
    assert plans[0].planned == 3
    assert plans[0].worst_case == 3
    assert not plans[0].exceeds_ceiling


def test_a_single_request_adapter_has_a_bound_equal_to_its_planned_count() -> None:
    from stage.http import profile
    from stage.services.sync import _bucket_plans

    plans = _bucket_plans(
        [("api.lever.co", "lever", 35, 35)], {"api.lever.co": profile("standard")}
    )
    assert plans[0].planned == plans[0].worst_case == 35


def test_two_boards_of_one_tenant_do_not_collide_on_job_id() -> None:
    from stage.sources.workday import WorkdayPosting, _to_job

    posting = WorkdayPosting.model_validate(_posting(7))
    one = _to_job(
        _company(
            name="Mastercard",
            workday_tenant="mastercard",
            workday_site="CUOReqSite",
            workday_dc="wd1",
        ),
        posting,
        NOW,
    )
    two = _to_job(
        _company(
            name="Mastercard",
            workday_tenant="mastercard",
            workday_site="CorporateCareers",
            workday_dc="wd1",
        ),
        posting,
        NOW,
    )

    assert one.id != two.id
    assert "cuoreqsite" in one.id and "corporatecareers" in two.id


async def test_a_resolved_facet_persists_and_reloads_keyed_by_tenant_and_site(
    db_path: "object",
) -> None:
    from pathlib import Path

    from stage.storage import SourceBatch, open_repository

    one = WorkdayFacet(
        tenant="mastercard",
        site="CUOReqSite",
        parameter="timeType",
        facet_ids=("a",),
        resolved_at=NOW,
    )
    two = WorkdayFacet(
        tenant="mastercard",
        site="CorporateCareers",
        parameter="workerSubType",
        facet_ids=("b",),
        resolved_at=NOW,
    )
    async with open_repository(Path(str(db_path))) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="workday", run_started_at=NOW, workday_facets=(one, two))
        )
        stored = await repository.load_workday_facets()

    assert stored[("mastercard", "CUOReqSite")].facet_ids == ("a",)
    assert stored[("mastercard", "CorporateCareers")].facet_ids == ("b",)


async def test_a_pinned_facet_is_never_written_back(db_path: "object") -> None:
    from pathlib import Path

    from stage.storage import SourceBatch, open_repository

    pinned = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("pin",), pinned=True
    )
    async with open_repository(Path(str(db_path))) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="workday", run_started_at=NOW, workday_facets=(pinned,))
        )
        assert await repository.load_workday_facets() == {}


async def test_zero_results_does_not_invalidate_but_a_vanished_facet_does(
    db_path: "object",
) -> None:
    from pathlib import Path

    from stage.storage import SourceBatch, open_repository

    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("f-intern",), resolved_at=NOW
    )
    empty = WorkdayPage.model_validate({"total": 0, "jobPostings": [], "facets": _facets()})
    gone = WorkdayPage.model_validate({"total": 9, "jobPostings": [], "facets": _facets("other")})

    async with open_repository(Path(str(db_path))) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="workday", run_started_at=NOW, workday_facets=(cached,))
        )
        assert facet_still_offered(empty, cached)
        assert not facet_still_offered(gone, cached)


@respx.mock
async def test_a_cached_facet_is_applied_without_re_resolving() -> None:

    captured: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"total": 0, "jobPostings": [], "facets": _facets()})

    respx.post(CXS).mock(side_effect=record)
    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("cached-id",)
    )
    async with _client() as client:
        await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert captured[0]["appliedFacets"] == {"timeType": ["cached-id"]}


@pytest.mark.parametrize(
    ("model", "payload", "missing"),
    [
        ("stage.sources.greenhouse:GreenhouseBoard", {}, "jobs"),
        ("stage.sources.smartrecruiters:SmartRecruitersPage", {"totalFound": 3}, "content"),
        ("stage.sources.workday:WorkdayPage", {"facets": []}, "jobPostings"),
    ],
)
def test_a_missing_payload_carrier_fails_loudly_rather_than_reading_as_empty(
    model: str, payload: dict[str, object], missing: str
) -> None:
    import importlib

    from pydantic import ValidationError

    module_name, _, attr = model.partition(":")
    cls = getattr(importlib.import_module(module_name), attr)

    with pytest.raises(ValidationError, match=missing):
        cls.model_validate(payload)

    complete = {**payload, missing: []}
    if missing == "content":
        complete["totalFound"] = 0
    cls.model_validate(complete)


def test_real_workday_descriptors_carry_qualifiers_and_must_still_match() -> None:
    page = WorkdayPage.model_validate(
        {
            "total": 273,
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "timeType",
                    "values": [
                        {"id": "ft", "descriptor": "Full time", "count": 254},
                        {"id": "pt", "descriptor": "Part time", "count": 19},
                    ],
                },
                {
                    "facetParameter": "workerSubType",
                    "values": [
                        {"id": "reg", "descriptor": "Regular", "count": 242},
                        {"id": "tmp", "descriptor": "Temporary (Fixed Term)", "count": 12},
                        {"id": "call", "descriptor": "On Call (Fixed Term)", "count": 11},
                        {"id": "stu", "descriptor": "Student (Fixed Term)", "count": 4},
                        {"id": "coop", "descriptor": "COOP-Student (Fixed Term)", "count": 4},
                    ],
                },
            ],
        }
    )

    facet = resolve_facet(page, "cae", "career", NOW)
    assert facet is not None, "the tenant that produced four Montreal internships must resolve"
    assert facet.parameter == "workerSubType"
    assert facet.facet_ids == ("stu", "coop"), (
        "both values must be applied; taking one lost half of CAE's internships"
    )


def test_a_qualifier_alone_never_makes_a_match() -> None:
    from stage.lexicon import fold, workday_facet_lexicon
    from stage.sources.workday import _matches

    _, descriptors = workday_facet_lexicon()
    for value in ("Regular", "Temporary (Fixed Term)", "On Call (Fixed Term)", "Full time"):
        assert not _matches(fold(value), descriptors), value
    assert not _matches(fold("Internal Auditor"), descriptors), "§5.3's substring trap"
    assert not _matches(fold("Student Barista"), descriptors), (
        "blocked bigrams are shared with internship.yaml; the marker lists are not"
    )


@respx.mock
async def test_every_matching_picklist_value_is_applied_not_just_the_first() -> None:
    captured: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:

        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "total": 0,
                "jobPostings": [],
                "facets": [
                    {
                        "facetParameter": "workerSubType",
                        "values": [
                            {"id": "reg", "descriptor": "Regular"},
                            {"id": "stu", "descriptor": "Student (Fixed Term)"},
                            {"id": "coop", "descriptor": "COOP-Student (Fixed Term)"},
                        ],
                    }
                ],
            },
        )

    respx.post(CXS).mock(side_effect=record)

    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="workerSubType", facet_ids=("stu", "coop")
    )
    async with _client() as client:
        await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert captured[0]["appliedFacets"] == {"workerSubType": ["stu", "coop"]}


def test_a_partially_offered_facet_is_stale_because_the_gap_is_silent() -> None:
    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="workerSubType", facet_ids=("stu", "coop")
    )
    partial = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {"facetParameter": "workerSubType", "values": [{"id": "stu", "descriptor": "x"}]}
            ],
        }
    )
    gone = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {"facetParameter": "workerSubType", "values": [{"id": "reg", "descriptor": "x"}]}
            ],
        }
    )
    assert not facet_still_offered(partial, cached), (
        "a partially offered facet is indistinguishable from a renamed one"
    )
    assert not facet_still_offered(gone, cached)

    intact = WorkdayPage.model_validate(
        {
            "jobPostings": [],
            "facets": [
                {
                    "facetParameter": "workerSubType",
                    "values": [
                        {"id": "stu", "descriptor": "x"},
                        {"id": "coop", "descriptor": "y"},
                    ],
                }
            ],
        }
    )
    assert facet_still_offered(intact, cached), (
        "a fully advertised facet must not re-resolve every run"
    )


@respx.mock
async def test_one_malformed_posting_is_dropped_captured_and_costs_the_page_authority() -> None:
    from stage.paths import capture_dir

    before = set(capture_dir().glob("workday-posting-*.json"))
    good = [_posting(i) for i in range(3)]
    bad = {"externalPath": "/job/x_JR9", "bulletFields": ["JR9"]}
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200, json={"total": 4, "jobPostings": [*good, bad], "facets": _facets()}
        )
    )

    async with _client() as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert len(result.jobs) == 3, "the good rows still ingest"
    assert not result.authoritative, "an incomplete listing must close nothing"
    assert "1 posting(s) failed validation" in result.degraded

    after = set(capture_dir().glob("workday-posting-*.json")) - before
    assert len(after) == 1, "the dropped row is captured individually, per §17"
    payload = json.loads(next(iter(after)).read_text(encoding="utf-8"))
    assert payload["bulletFields"] == ["JR9"], "captured verbatim, not summarised"
    for path in after:
        path.unlink()


@respx.mock
async def test_a_broken_page_shape_still_fails_loudly() -> None:
    respx.post(CXS).mock(return_value=httpx.Response(200, json={"total": 1}))
    async with _client() as client:
        with pytest.raises(PayloadValidationError, match="jobPostings"):
            await WorkdayAdapter().fetch(_company(workday_facet="timeType:f-intern"), client, NOW)


@respx.mock
async def test_a_tenants_own_total_is_evidence_not_truth() -> None:
    pages = [
        {"total": 0, "jobPostings": [_posting(i) for i in range(PAGE_SIZE)], "facets": []},
        {"total": 0, "jobPostings": [_posting(100 + i) for i in range(3)], "facets": []},
    ]
    respx.post(CXS).mock(side_effect=[httpx.Response(200, json=page) for page in pages])
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(
            _company(workday_facet="timeType:f-intern"), client, NOW
        )

    assert len(result.jobs) == PAGE_SIZE + 3, (
        "a non-positive total is unknown, so the walk continues to the short page"
    )
    assert respx.calls.call_count == 2


@respx.mock
async def test_a_facet_resolved_on_first_contact_is_applied_to_the_same_run() -> None:
    bodies: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        faceted = bool(body["appliedFacets"])
        return httpx.Response(
            200,
            json={
                "total": 1 if faceted else 500,
                "jobPostings": [] if faceted and len(bodies) > 2 else [_posting(len(bodies))],
                "facets": [
                    {
                        "facetParameter": "workerSubType",
                        "values": [
                            {"id": "reg", "descriptor": "Regular"},
                            {"id": "stu", "descriptor": "Student (Fixed Term)"},
                        ],
                    }
                ],
            },
        )

    respx.post(CXS).mock(side_effect=record)
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW)

    assert bodies[0]["appliedFacets"] == {}, "first contact cannot know the facet yet"
    assert bodies[1]["appliedFacets"] == {"workerSubType": ["stu"]}, "the walk restarts faceted"
    assert result.facets, "the resolved facet is returned for the caller to persist"
    assert len(bodies) < 25, "the restart replaces the unfaceted walk rather than adding to it"


@respx.mock
async def test_the_restart_happens_at_most_once_per_tenant_per_run() -> None:
    bodies: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "total": 500,
                "jobPostings": [_posting(len(bodies))],
                "facets": [
                    {
                        "facetParameter": "workerSubType",
                        "values": [{"id": "stu", "descriptor": "Student (Fixed Term)"}],
                    }
                ],
            },
        )

    respx.post(CXS).mock(side_effect=record)
    async with _client(UNPACED) as client:
        await WorkdayAdapter().fetch(_company(), client, NOW)

    unfaceted = [b for b in bodies if not b["appliedFacets"]]
    assert len(unfaceted) == 1, "exactly one page is spent discovering the facet"
    assert len(bodies) <= MAX_PAGES + 1, "the page cap still bounds the restarted walk"


@respx.mock
async def test_a_cached_facet_needs_no_discovery_page_at_all() -> None:

    bodies: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"total": 0, "jobPostings": [], "facets": _facets()})

    respx.post(CXS).mock(side_effect=record)
    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("f-intern",)
    )
    async with _client(UNPACED) as client:
        await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert len(bodies) == 1
    assert bodies[0]["appliedFacets"] == {"timeType": ["f-intern"]}


@respx.mock
async def test_a_queued_workday_posting_gets_its_body_on_the_shared_bucket() -> None:
    detail = "https://cae.wd3.myworkdayjobs.com/wday/cxs/cae/career/job/Montreal/x_R00001"
    respx.get(detail).mock(
        return_value=httpx.Response(
            200,
            json={"jobPostingInfo": {"jobDescription": "<p>Stage d'&#233;t&#233; 2027.</p>"}},
        )
    )
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Stagiaire",
                        "externalPath": "/job/Montreal/x_R00001",
                        "locationsText": "Montreal",
                        "bulletFields": ["R00001"],
                    }
                ],
                "facets": _facets(),
            },
        )
    )

    from stage.domain import job_id

    ident = job_id("workday", "cae-career", "R00001")
    async with _client(UNPACED) as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW, None, [ident])

    assert "2027" in result.jobs[0].description
    assert result.detail_fetches == (DetailFetch(id=ident, resolved=True),)


@respx.mock
@pytest.mark.parametrize("status", (404, 500))
async def test_a_workday_detail_failure_leaves_the_listing_authoritative(status: int) -> None:
    detail = "https://cae.wd3.myworkdayjobs.com/wday/cxs/cae/career/job/Montreal/x_R00001"
    respx.get(detail).mock(return_value=httpx.Response(status))
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Stagiaire",
                        "externalPath": "/job/Montreal/x_R00001",
                        "bulletFields": ["R00001"],
                    }
                ],
                "facets": _facets(),
            },
        )
    )

    from stage.domain import job_id

    ident = job_id("workday", "cae-career", "R00001")
    async with _client(UNPACED) as client:
        company = _company(workday_facet="timeType:f-intern")
        result = await WorkdayAdapter().fetch(company, client, NOW, None, [ident])

    assert result.authoritative, "a detail failure is not a listing failure"
    assert result.detail_fetches == (DetailFetch(id=ident, resolved=False, failed=True),), (
        f"a {status} is no answer, so the row stays retryable"
    )


def test_the_workday_detail_budget_is_derived_from_what_the_walk_leaves() -> None:
    from stage.http import profile

    assert WorkdayAdapter.detail_budget + 27 < profile("workday").max_requests_per_run


@respx.mock
async def test_a_vanished_facet_is_re_resolved_in_the_same_run() -> None:

    bodies: list[dict[str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [_posting(1)],
                "facets": _facets(intern_id="f-renamed"),
            },
        )

    respx.post(CXS).mock(side_effect=record)
    cached = WorkdayFacet(tenant="cae", site="career", parameter="timeType", facet_ids=("f-gone",))
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert bodies[0]["appliedFacets"] == {"timeType": ["f-gone"]}
    assert bodies[1]["appliedFacets"] == {}, "the walk restarts unfaceted to re-resolve"
    assert [cast(WorkdayFacet, f).facet_ids for f in result.facets] == [("f-renamed",)]
    assert result.forgotten_facets == (), (
        "a facet that re-resolved needs no delete; the upsert overwrites the row"
    )
    assert not result.authoritative, (
        "postings were gathered under the old facet, so the first run closes nothing"
    )


@respx.mock
async def test_a_facet_that_cannot_be_re_resolved_is_forgotten() -> None:

    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [_posting(1)],
                "facets": [
                    {
                        "facetParameter": "timeType",
                        "descriptor": "Time Type",
                        "values": [{"id": "f-full", "descriptor": "Full time", "count": 9}],
                    }
                ],
            },
        )
    )
    cached = WorkdayFacet(tenant="cae", site="career", parameter="timeType", facet_ids=("f-gone",))
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert [cast(WorkdayFacet, f).facet_ids for f in result.forgotten_facets] == [("f-gone",)]
    assert result.facets == ()
    assert not result.authoritative


@respx.mock
async def test_a_pinned_facet_is_never_re_resolved_behind_a_humans_back() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [_posting(1)],
                "facets": _facets(intern_id="f-renamed"),
            },
        )
    )
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(
            _company(workday_facet="timeType:f-pinned"), client, NOW
        )

    assert result.forgotten_facets == ()
    assert result.facets == ()
    assert "pinned facet" in result.degraded
    assert "stage discover --url" in result.degraded
    assert not result.authoritative


@respx.mock
async def test_a_cached_facet_with_no_facet_list_is_drift_not_a_clean_run() -> None:
    from stage.domain import WorkdayFacet
    from stage.paths import capture_dir

    respx.post(CXS).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [_posting(1)]})
    )
    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("f-intern",)
    )
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert result.jobs, "the postings that did arrive are still kept"
    assert not result.authoritative, (
        "an undecidable facet list is not a complete roster and must close nothing"
    )
    assert "no facet list at all" in result.degraded
    assert result.forgotten_facets == (), "an undecidable response must not drop the cache"
    assert result.facets == (), "nor re-resolve behind a facet that may still be correct"

    captured = sorted(capture_dir().glob("workday-nofacets-*.json"))
    assert captured, "the payload must reach disk or the drift cannot be diagnosed"
    for path in captured:
        path.unlink()


@respx.mock
async def test_a_pinned_facet_with_no_facet_list_is_also_drift() -> None:
    from stage.paths import capture_dir

    respx.post(CXS).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [_posting(1)]})
    )
    async with _client(UNPACED) as client:
        result = await WorkdayAdapter().fetch(
            _company(workday_facet="timeType:f-pinned"), client, NOW
        )

    assert not result.authoritative
    assert "no facet list at all" in result.degraded
    for path in capture_dir().glob("workday-nofacets-*.json"):
        path.unlink()


@respx.mock
async def test_a_faceted_walk_marks_its_postings_as_structurally_internships() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Analyste de données",
                        "externalPath": "/job/Montreal/Analyste_R1",
                        "bulletFields": ["R1"],
                        "locationsText": "Montreal, QC",
                    }
                ],
                "facets": _facets(),
            },
        )
    )
    cached = WorkdayFacet(
        tenant="cae", site="career", parameter="timeType", facet_ids=("cached-id",)
    )
    async with _client() as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW, {("cae", "career"): cached})

    assert result.jobs[0].signals.employment_type == "internship", (
        "a walk filtered to the tenant's internship facet is positive structured evidence"
    )


@respx.mock
async def test_an_unfaceted_fallback_walk_claims_no_structured_evidence() -> None:
    respx.post(CXS).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Analyste de données",
                        "externalPath": "/job/Montreal/Analyste_R1",
                        "bulletFields": ["R1"],
                        "locationsText": "Montreal, QC",
                    }
                ],
                "facets": [],
            },
        )
    )
    async with _client() as client:
        result = await WorkdayAdapter().fetch(_company(), client, NOW)

    assert result.jobs[0].signals.employment_type == "", (
        "a whole-board walk says nothing about whether a posting is an internship"
    )
