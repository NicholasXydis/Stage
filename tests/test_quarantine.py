
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.classify import screen_location
from stage.domain import (
    Job,
    LocationBucket,
    QuarantinedJob,
    QuarantineFilters,
    RejectionReason,
    location_agrees,
    term_agrees,
)
from stage.services.sync import normalize_batch
from stage.storage import SourceBatch, open_repository


def _job(job_id: str, location_raw: str, when: datetime) -> Job:
    return Job(
        id=job_id,
        source="greenhouse",
        company="Acme",
        title_raw="Software Engineering Intern",
        title_normalized="Software Engineering Intern",
        apply_url_raw=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        description="",
        location_raw=location_raw,
        first_seen=when,
        last_seen=when,
    )


@pytest.fixture
def run_time() -> datetime:
    return datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_out_of_scope_locations_are_rejected_not_dropped(run_time: datetime) -> None:
    kept, rejected = normalize_batch(
        [
            _job("1", "Bengaluru, Karnataka, India", run_time),
            _job("2", "Montreal, QC, Canada", run_time),
            _job("3", "Austin, TX", run_time),
            _job("4", "London, England, United Kingdom", run_time),
        ]
    )
    assert [job.id for job in kept] == ["2", "3"]
    assert [entry.id for entry in rejected] == ["1", "4"]
    assert all(entry.reason is RejectionReason.OUT_OF_SCOPE_LOCATION for entry in rejected)


def test_the_rejection_carries_the_phrase_it_matched(run_time: datetime) -> None:
    _, rejected = normalize_batch([_job("1", "Bengaluru, Karnataka, India", run_time)])
    assert "india" in rejected[0].matched_phrase
    assert rejected[0].location_raw == "Bengaluru, Karnataka, India"


def test_unknown_is_never_rejected(run_time: datetime) -> None:
    kept, rejected = normalize_batch(
        [
            _job("1", "Hybrid", run_time),
            _job("2", "Multiple Locations", run_time),
            _job("3", "", run_time),
        ]
    )
    assert len(kept) == 3
    assert rejected == ()
    assert all(job.location is LocationBucket.UNKNOWN for job in kept)


@pytest.mark.parametrize("bucket", list(LocationBucket))
def test_screen_location_rejects_other_and_nothing_else(
    bucket: LocationBucket, run_time: datetime
) -> None:
    job = replace(_job("x", "", run_time), location=bucket)
    rejected = screen_location(job) is not None
    assert rejected is (bucket is LocationBucket.OTHER), bucket


async def test_quarantined_postings_leave_the_jobs_table(
    db_path: Path, run_time: datetime
) -> None:
    async with open_repository(db_path) as repository:
        kept, rejected = normalize_batch(
            [
                _job("keep", "Toronto, ON, Canada", run_time),
                _job("reject", "Bengaluru, India", run_time),
            ]
        )
        result = await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time,
                jobs=kept,
                quarantined=rejected,
                closable_boards=("greenhouse:acme",),
            )
        )
        assert result.quarantined == 1
        assert result.added == 1

        assert await repository.get_job("reject") is None
        assert await repository.get_job("keep") is not None

        entries = await repository.list_quarantined(QuarantineFilters())
        assert [entry.id for entry in entries] == ["reject"]
        assert await repository.quarantine_reason_counts() == {"out-of-scope-location": 1}


async def test_a_posting_already_stored_is_moved_when_a_rule_starts_rejecting_it(
    db_path: Path, run_time: datetime
) -> None:
    async with open_repository(db_path) as repository:
        stored = _job("row", "Bengaluru, India", run_time)
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=run_time, jobs=(stored,))
        )
        assert await repository.get_job("row") is not None

        kept, rejected = normalize_batch([stored])
        assert kept == ()
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time + timedelta(hours=1),
                jobs=kept,
                quarantined=rejected,
            )
        )
        assert await repository.get_job("row") is None
        assert len(await repository.list_quarantined(QuarantineFilters())) == 1


async def test_a_released_posting_keeps_its_original_first_seen(
    db_path: Path, run_time: datetime
) -> None:
    later = run_time + timedelta(days=9)
    async with open_repository(db_path) as repository:
        _, rejected = normalize_batch([_job("row", "Bengaluru, India", run_time)])
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=run_time, quarantined=rejected
            )
        )

        released = _job("row", "Montreal, QC, Canada", later)
        kept, still_rejected = normalize_batch([released])
        assert still_rejected == ()
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=later, jobs=kept)
        )

        restored = await repository.get_job("row")
        assert restored is not None
        assert restored.first_seen == run_time, "released posting resurfaced as new"
        assert restored.last_seen == later
        assert await repository.list_quarantined(QuarantineFilters()) == []


async def test_quarantine_filters(db_path: Path, run_time: datetime) -> None:
    async with open_repository(db_path) as repository:
        _, rejected = normalize_batch(
            [
                _job("a", "Bengaluru, India", run_time),
                _job("b", "Tokyo, Japan", run_time),
            ]
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=run_time, quarantined=rejected
            )
        )
        assert await repository.count_quarantined(QuarantineFilters()) == 2
        assert await repository.count_quarantined(QuarantineFilters(source="lever")) == 0
        assert (
            await repository.count_quarantined(
                QuarantineFilters(reason=RejectionReason.OUT_OF_SCOPE_LOCATION)
            )
            == 2
        )
        assert await repository.count_quarantined(QuarantineFilters(company="Nope")) == 0


@pytest.mark.parametrize(
    ("left", "right", "agrees"),
    [
        (LocationBucket.MONTREAL, LocationBucket.MONTREAL, True),
        (LocationBucket.CANADA, LocationBucket.CANADA, True),
        (LocationBucket.CANADA, LocationBucket.MONTREAL, False),
        (LocationBucket.UNKNOWN, LocationBucket.UNKNOWN, False),
        (LocationBucket.OTHER, LocationBucket.OTHER, False),
        (LocationBucket.OTHER, LocationBucket.UNKNOWN, False),
    ],
)
def test_other_and_unknown_never_satisfy_the_cross_language_guardrail(
    left: LocationBucket, right: LocationBucket, agrees: bool
) -> None:
    assert location_agrees(left, right) is agrees


def test_unknown_term_never_satisfies_the_guardrail_either() -> None:
    assert term_agrees("summer-2027", "summer-2027") is True
    assert term_agrees("summer-2027", "fall-2027") is False
    assert term_agrees("unknown", "unknown") is False


async def test_quarantining_a_canonical_promotes_its_follower(
    db_path: Path, run_time: datetime
) -> None:
    from stage.dedup.resolve import DuplicateLink
    from stage.domain import JobFilters

    canonical = _job("greenhouse:acme:1", "Toronto, ON, Canada", run_time)
    follower = _job("simplify:acme:2", "Toronto, ON, Canada", run_time)
    follower = replace(follower, source="simplify")

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time,
                jobs=(canonical, follower),
                resolve_duplicates=lambda _stored, _incoming: [
                    DuplicateLink(
                        duplicate_id=follower.id,
                        canonical_id=canonical.id,
                        kind="canonical-url",
                        evidence="same posting from two sources",
                    )
                ],
            )
        )
        assert await repository.count_duplicates() == 1

        rejected = QuarantinedJob(
            id=canonical.id,
            source=canonical.source,
            company=canonical.company,
            title_raw=canonical.title_raw,
            reason=RejectionReason.NOT_AN_INTERNSHIP,
            matched_phrase="staff engineer",
            first_seen=run_time,
            last_seen=run_time,
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=run_time,
                jobs=(),
                quarantined=(rejected,),
            )
        )

        assert await repository.get_job(canonical.id) is None
        surviving = await repository.list_jobs(JobFilters())

    assert [job.id for job in surviving] == [follower.id], (
        "quarantine deleted the canonical, so every copy vanished behind duplicate_of IS NULL"
    )
