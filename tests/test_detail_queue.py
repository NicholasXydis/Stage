from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx

from stage.domain import (
    UNKNOWN_TERM,
    Company,
    DetailFetch,
    Job,
    Platform,
    RoleCategory,
    job_id,
)
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _job(ident: str, **kwargs: object) -> Job:
    base = Job(
        id=ident,
        source="smartrecruiters",
        company="Acme",
        title_raw="Software Engineer Intern",
        title_normalized="software engineer intern",
        apply_url_raw="",
        description="",
        term=UNKNOWN_TERM,
        role=RoleCategory.UNKNOWN,
        first_seen=NOW,
        last_seen=NOW,
    )
    return replace(base, **kwargs)  # type: ignore[arg-type]


async def test_only_rows_a_description_could_change_are_queued(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW,
                jobs=(
                    _job("needs-both"),
                    _job("needs-term", role=RoleCategory.SWE),
                    _job("needs-role", term="summer-2027"),
                    _job("resolved", term="summer-2027", role=RoleCategory.SWE),
                    _job(
                        "has-body",
                        description="a prose body",
                        term="summer-2027",
                        role=RoleCategory.SWE,
                    ),
                ),
            )
        )
        queued = await repository.detail_queue("smartrecruiters", 50)

    assert set(queued) == {"needs-both", "needs-term", "needs-role"}
    assert "resolved" not in queued
    assert "has-body" not in queued


async def test_a_fetched_row_leaves_the_queue_even_when_it_resolved_nothing(
    db_path: Path,
) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW,
                jobs=(_job("a"), _job("b")),
            )
        )
        assert await repository.detail_queue_size("smartrecruiters") == 2

        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW + timedelta(minutes=1),
                detail_fetches=(DetailFetch(id="a", resolved=False),),
            )
        )
        assert await repository.detail_queue("smartrecruiters", 50) == ["b"]
        assert await repository.detail_queue_size("smartrecruiters") == 1


async def test_the_queue_is_scoped_to_one_source(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="smartrecruiters", run_started_at=NOW, jobs=(_job("sr"),))
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="simplify",
                run_started_at=NOW,
                jobs=(_job("feed", source="simplify"),),
            )
        )
        assert await repository.detail_queue("smartrecruiters", 50) == ["sr"]
        assert await repository.detail_queue("simplify", 50) == ["feed"]


async def test_a_queue_larger_than_the_ceiling_is_served_newest_first_and_the_rest_wait(
    db_path: Path,
) -> None:
    jobs = tuple(
        _job(f"job-{index:03d}", first_seen=NOW - timedelta(days=index)) for index in range(200)
    )
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="smartrecruiters", run_started_at=NOW, jobs=jobs)
        )
        assert await repository.detail_queue_size("smartrecruiters") == 200

        first = await repository.detail_queue("smartrecruiters", 150)
        assert len(first) == 150, "a run takes the ceiling, never the whole queue"
        assert first[0] == "job-000", "newest first"

        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW + timedelta(minutes=1),
                detail_fetches=tuple(DetailFetch(id=ident) for ident in first),
            )
        )
        second = await repository.detail_queue("smartrecruiters", 150)

    assert len(second) == 50, "the remainder waits for the next run rather than being lost"
    assert not set(second) & set(first), "no row is served twice"


@respx.mock
async def test_a_queued_posting_gets_its_body_merged_before_it_leaves_the_adapter(
    db_path: Path,
) -> None:
    from stage.http import HttpClient, RatePosture
    from stage.sources.smartrecruiters import SmartRecruitersAdapter

    unpaced = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=50)
    listing = "https://api.smartrecruiters.com/v1/companies/acme/postings"
    respx.get(f"{listing}/P1").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>Build things.</p>"},
                        "qualifications": {"text": "<p>Summer 2027, Bachelor's.</p>"},
                    }
                }
            },
        )
    )
    respx.get(url__startswith=listing).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {
                        "id": "P1",
                        "name": "Software Engineer Intern",
                        "location": {"city": "Montreal"},
                    }
                ],
            },
        )
    )

    company = Company(name="Acme", platform=Platform.SMARTRECRUITERS, slug="acme")
    async with HttpClient(
        allowed_hosts=frozenset({"api.smartrecruiters.com"}), posture=unpaced, jitter=False
    ) as client:
        job_ident = job_id("smartrecruiters", "acme", "P1")
        result = await SmartRecruitersAdapter().fetch(company, client, NOW, None, [job_ident])

    assert len(result.jobs) == 1
    assert "Build things." in result.jobs[0].description
    assert "Summer 2027" in result.jobs[0].description, (
        "sections join in order: role, then degree and term signals"
    )
    assert result.detail_fetches == (DetailFetch(id=job_ident, resolved=True),)


@respx.mock
async def test_a_failed_detail_fetch_never_touches_listing_authority(db_path: Path) -> None:
    from stage.http import HttpClient, RatePosture
    from stage.sources.smartrecruiters import SmartRecruitersAdapter

    unpaced = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=50)
    listing = "https://api.smartrecruiters.com/v1/companies/acme/postings"
    respx.get(f"{listing}/P1").mock(return_value=httpx.Response(500))
    respx.get(url__startswith=listing).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [{"id": "P1", "name": "Intern", "location": {"city": "Montreal"}}],
            },
        )
    )

    company = Company(name="Acme", platform=Platform.SMARTRECRUITERS, slug="acme")
    async with HttpClient(
        allowed_hosts=frozenset({"api.smartrecruiters.com"}), posture=unpaced, jitter=False
    ) as client:
        job_ident = job_id("smartrecruiters", "acme", "P1")
        result = await SmartRecruitersAdapter().fetch(company, client, NOW, None, [job_ident])

    assert result.authoritative, "a detail failure is not a listing failure"
    assert result.jobs[0].description == ""
    assert result.detail_fetches == (DetailFetch(id=job_ident, resolved=False, failed=True),)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW,
                jobs=result.jobs,
                detail_fetches=result.detail_fetches,
            )
        )
        assert await repository.detail_queue("smartrecruiters", 50) == [job_ident], (
            "the adapter's own failure outcome must leave the row retryable"
        )


def test_the_detail_budget_is_ordered_by_density_not_volume() -> None:
    from stage.sources import get_adapter

    assert get_adapter("smartrecruiters").detail_budget > 0
    assert get_adapter("greenhouse").detail_budget == 0, "its listing already carries bodies"
    assert get_adapter("lever").detail_budget == 0


async def test_a_row_that_already_has_a_body_never_enters_the_queue(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(
                    _job(
                        "described-unknown-term",
                        source="greenhouse",
                        description="a full prose body",
                        role=RoleCategory.SWE,
                    ),
                    _job(
                        "described-unknown-role",
                        source="greenhouse",
                        description="a full prose body",
                        term="summer-2027",
                    ),
                    _job("thin", source="greenhouse"),
                ),
            )
        )
        queued = await repository.detail_queue("greenhouse", 50)

    assert queued == ["thin"], "a description that exists cannot be improved by fetching it again"


async def test_a_failed_fetch_is_retried_while_an_answered_one_is_not(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW,
                jobs=(_job("failed"), _job("empty-body"), _job("answered")),
            )
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="smartrecruiters",
                run_started_at=NOW,
                detail_fetches=(
                    DetailFetch(id="failed", resolved=False, failed=True),
                    DetailFetch(id="empty-body", resolved=False),
                    DetailFetch(id="answered", resolved=True),
                ),
            )
        )
        queued = await repository.detail_queue("smartrecruiters", 50)

    assert queued == ["failed"], "only the row that never received an answer may be tried again"


async def test_a_retry_is_bounded_so_a_dead_endpoint_cannot_hold_the_queue(
    db_path: Path,
) -> None:
    from stage.storage.sqlite_repo import MAX_DETAIL_ATTEMPTS

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="smartrecruiters", run_started_at=NOW, jobs=(_job("doomed"),))
        )
        for _ in range(MAX_DETAIL_ATTEMPTS):
            assert await repository.detail_queue("smartrecruiters", 50) == ["doomed"]
            await repository.apply_source_batch(
                SourceBatch(
                    source="smartrecruiters",
                    run_started_at=NOW,
                    detail_fetches=(DetailFetch(id="doomed", resolved=False, failed=True),),
                )
            )

        assert await repository.detail_queue("smartrecruiters", 50) == []
        assert await repository.detail_queue_size("smartrecruiters") == 0, (
            "the size must be computed by the same predicate as the queue itself"
        )
