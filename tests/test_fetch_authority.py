
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx

from stage.domain import Company, Job, JobStatus, Platform, QuarantinedJob, RejectionReason
from stage.http import HttpClient, RatePosture
from stage.sources.base import FetchResult
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
UNPACED = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=500)


def _sr_company() -> Company:
    return Company(name="Acme", platform=Platform.SMARTRECRUITERS, slug="acme")


def _sr_url(offset: int) -> str:
    return (
        "https://api.smartrecruiters.com/v1/companies/acme/postings"
        f"?limit=100&offset={offset}"
    )


def _sr_page(ids: list[int], total: int) -> dict[str, object]:
    return {
        "totalFound": total,
        "content": [
            {"id": str(i), "name": "Software Engineer Intern", "location": {"city": "Montreal"}}
            for i in ids
        ],
    }


def test_a_complete_result_is_authoritative_by_default() -> None:
    assert FetchResult().authoritative


@respx.mock
async def test_a_page_two_304_ends_the_walk_without_authority_and_says_so() -> None:
    from stage.sources.smartrecruiters import SmartRecruitersAdapter

    respx.get(_sr_url(0)).mock(
        return_value=httpx.Response(200, json=_sr_page(list(range(100)), 250))
    )
    respx.get(_sr_url(100)).mock(return_value=httpx.Response(304))

    async with HttpClient(
        allowed_hosts=frozenset({"api.smartrecruiters.com"}), posture=UNPACED, jitter=False
    ) as client:
        result = await SmartRecruitersAdapter().fetch(_sr_company(), client, NOW)

    assert len(result.jobs) == 100
    assert not result.authoritative
    assert "304" in result.degraded


@respx.mock
async def test_the_page_cap_costs_authority_too() -> None:
    from stage.sources.smartrecruiters import MAX_PAGES, SmartRecruitersAdapter

    for page in range(MAX_PAGES + 1):
        respx.get(_sr_url(page * 100)).mock(
            return_value=httpx.Response(200, json=_sr_page(list(range(100)), 999_999))
        )

    async with HttpClient(
        allowed_hosts=frozenset({"api.smartrecruiters.com"}), posture=UNPACED, jitter=False
    ) as client:
        result = await SmartRecruitersAdapter().fetch(_sr_company(), client, NOW)

    assert not result.authoritative
    assert "cap" in result.degraded


@respx.mock
async def test_a_workday_facet_fallback_costs_authority() -> None:
    from stage.sources.workday import WorkdayAdapter

    company = Company(
        name="CAE",
        platform=Platform.WORKDAY,
        slug="cae",
        workday_tenant="cae",
        workday_site="career",
        workday_dc="wd3",
    )
    respx.post("https://cae.wd3.myworkdayjobs.com/wday/cxs/cae/career/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [{"title": "Intern", "bulletFields": ["R1"]}],
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
    async with HttpClient(
        allowed_hosts=frozenset({"cae.wd3.myworkdayjobs.com"}),
        posture=UNPACED,
        bucket_key="workday",
        jitter=False,
    ) as client:
        result = await WorkdayAdapter().fetch(company, client, NOW)

    assert result.jobs, "the postings are real and kept"
    assert "no internship facet" in result.degraded
    assert not result.authoritative, "a fallback listing must close nothing"


async def test_a_non_authoritative_result_closes_nothing(db_path: Path) -> None:
    def job(ident: str, seen: datetime) -> Job:
        return Job(
            id=ident,
            source="smartrecruiters",
            company="Acme",
            title_raw="Intern",
            title_normalized="intern",
            apply_url_raw="",
            description="",
            first_seen=seen,
            last_seen=seen,
        )

    board = "smartrecruiters:acme"
    first, second = f"{board}:a", f"{board}:b"
    later = NOW + timedelta(days=1)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW,
                jobs=(job(first, NOW), job(second, NOW)),
                closable_boards=(board,),
            )
        )
        partial = await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=later,
                jobs=(job(first, later),),
                closable_boards=(),
            )
        )
        assert partial.closed == 0
        missing = await repository.get_job(second)
        assert missing is not None and missing.status is JobStatus.OPEN

        final = later + timedelta(days=1)
        complete = await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=final,
                jobs=(job(first, final),),
                closable_boards=(board,),
            )
        )
        assert complete.closed == 1, "a complete listing must still close what is absent"


async def test_first_seen_survives_the_jobs_to_quarantine_direction(db_path: Path) -> None:
    original = NOW
    later = NOW + timedelta(days=9)

    def job(seen: datetime) -> Job:
        return Job(
            id="acme-1",
            source="greenhouse",
            company="Acme",
            title_raw="Intern",
            title_normalized="intern",
            apply_url_raw="",
            description="",
            first_seen=seen,
            last_seen=seen,
        )

    def rejected(seen: datetime) -> QuarantinedJob:
        return QuarantinedJob(
            id="acme-1",
            source="greenhouse",
            company="Acme",
            title_raw="Intern",
            reason=RejectionReason.NOT_AN_INTERNSHIP,
            first_seen=seen,
            last_seen=seen,
            matched_phrase="intern",
        )

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=original, jobs=(job(original),))
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=later, quarantined=(rejected(later),)
            )
        )
        held = await repository.list_quarantined(
            __import__("stage.domain", fromlist=["QuarantineFilters"]).QuarantineFilters()
        )
        assert len(held) == 1
        assert held[0].first_seen == original, "quarantine must not stamp today's date"

        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=later + timedelta(days=1),
                jobs=(job(later + timedelta(days=1)),),
            )
        )
        restored = await repository.get_job("acme-1")

    assert restored is not None
    assert restored.first_seen == original, "a full round trip must return the original date"
