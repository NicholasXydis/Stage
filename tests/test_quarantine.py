from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.domain import (
    Job,
    LocationBucket,
    QuarantinedJob,
    QuarantineFilters,
    RejectionReason,
    RemoteScope,
    location_agrees,
    term_agrees,
)
from stage.services.screening import normalize_batch
from stage.storage import SourceBatch, open_repository

FULL_TIME = "Senior Software Engineer"


def _job(
    job_id: str, location_raw: str, when: datetime, title: str = "Software Engineering Intern"
) -> Job:
    return Job(
        id=job_id,
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title,
        apply_url_raw=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        description="",
        location_raw=location_raw,
        first_seen=when,
        last_seen=when,
    )


@pytest.fixture
def run_time() -> datetime:
    return datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_international_locations_are_quarantined(run_time: datetime) -> None:
    kept, rejected = normalize_batch(
        [
            _job("1", "Bengaluru, Karnataka, India", run_time),
            _job("2", "Montreal, QC, Canada", run_time),
            _job("3", "Austin, TX", run_time),
            _job("4", "London, England, United Kingdom", run_time),
        ]
    )
    assert [job.id for job in kept] == ["2", "3"]
    assert [job.id for job in rejected] == ["1", "4"]
    assert {job.reason for job in rejected} == {RejectionReason.OUT_OF_SCOPE_LOCATION}


def test_a_rejection_explains_the_evidence(run_time: datetime) -> None:
    _, rejected = normalize_batch([_job("1", "Remote", run_time, FULL_TIME)])
    assert rejected[0].reason is RejectionReason.NOT_AN_INTERNSHIP
    assert rejected[0].matched_phrase, "a rejection must record why"
    assert rejected[0].location_raw == "Remote"


def test_remote_postings_are_kept(run_time: datetime) -> None:
    kept, rejected = normalize_batch(
        [
            _job("1", "Remote", run_time),
            _job("2", "Distributed", run_time),
            _job("3", "Remote job", run_time),
        ]
    )
    assert rejected == (), "remote names no place, and under worldwide scope that excludes nothing"
    assert [job.id for job in kept] == ["1", "2", "3"]


def test_a_location_that_could_not_be_read_is_kept_and_stays_visible(run_time: datetime) -> None:
    kept, rejected = normalize_batch(
        [
            _job("1", "Hybrid", run_time),
            _job("2", "Multiple Locations", run_time),
            _job("3", "", run_time),
            _job("4", "2 Locations", run_time),
        ]
    )
    assert rejected == (), (
        "an unreadable location is missing evidence, not evidence of a foreign place"
    )
    assert [job.id for job in kept] == ["1", "2", "3", "4"]
    assert all(job.location is LocationBucket.UNKNOWN for job in kept)


@pytest.mark.parametrize("bucket", list(LocationBucket))
@pytest.mark.parametrize("scope", [None, RemoteScope.UNSPECIFIED])
def test_no_unreadable_location_is_ever_a_rejection(
    bucket: LocationBucket, scope: RemoteScope | None, run_time: datetime
) -> None:
    job = replace(
        _job("x", "Remote", run_time),
        title_raw="Software Engineer Intern",
        location=bucket,
        remote_scope=scope,
    )
    kept, rejected = normalize_batch([job])
    assert len(kept) == 1, f"{bucket} with scope {scope} must be admitted"
    assert not rejected


def test_remote_keeps_the_country_it_names(run_time: datetime) -> None:
    kept, rejected = normalize_batch(
        [
            _job("1", "Remote - United States", run_time),
            _job("2", "Montreal, QC; Remote", run_time),
        ]
    )
    assert rejected == (), "a remote posting that still names a place has a location"
    assert [job.location for job in kept] == [LocationBucket.USA, LocationBucket.MONTREAL]


async def test_quarantined_postings_leave_the_jobs_table(db_path: Path, run_time: datetime) -> None:
    async with open_repository(db_path) as repository:
        kept, rejected = normalize_batch(
            [
                _job("keep", "Toronto, ON, Canada", run_time),
                _job("reject", "Remote", run_time, FULL_TIME),
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
        assert await repository.quarantine_reason_counts() == {"not-an-internship": 1}


async def test_a_posting_already_stored_is_moved_when_a_rule_starts_rejecting_it(
    db_path: Path, run_time: datetime
) -> None:
    async with open_repository(db_path) as repository:
        stored = _job("row", "Remote", run_time, FULL_TIME)
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
        _, rejected = normalize_batch([_job("row", "Remote", run_time, FULL_TIME)])
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=run_time, quarantined=rejected)
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
                _job("a", "Remote", run_time, FULL_TIME),
                _job("b", "Distributed", run_time, FULL_TIME),
            ]
        )
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=run_time, quarantined=rejected)
        )
        assert await repository.count_quarantined(QuarantineFilters()) == 2
        assert await repository.count_quarantined(QuarantineFilters(source="lever")) == 0
        assert (
            await repository.count_quarantined(
                QuarantineFilters(reason=RejectionReason.NOT_AN_INTERNSHIP)
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
        (LocationBucket.INTERNATIONAL, LocationBucket.INTERNATIONAL, False),
        (LocationBucket.INTERNATIONAL, LocationBucket.UNKNOWN, False),
    ],
)
def test_international_and_unknown_never_satisfy_the_cross_language_guardrail(
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


async def test_a_quarantine_page_starts_at_its_offset_and_counts_follow_the_filters(
    db_path: Path, run_time: datetime
) -> None:
    abroad = [_job(f"abroad-{n}", "Berlin, Germany", run_time) for n in range(3)]
    full_time = replace(_job("full", "Toronto, ON", run_time, FULL_TIME), company="Beta")
    async with open_repository(db_path) as repository:
        _, rejected = normalize_batch([*abroad, full_time])
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=run_time, quarantined=rejected)
        )
        page = await repository.list_quarantined(
            QuarantineFilters(company="acme", limit=2, offset=1)
        )
        counts = await repository.quarantine_reason_counts(QuarantineFilters(company="Acme"))

    assert [entry.id for entry in page] == ["abroad-1", "abroad-2"]
    assert counts == {"out-of-scope-location": 3}


async def test_a_first_seen_from_a_clock_that_ran_ahead_is_pulled_back(
    db_path: Path, run_time: datetime
) -> None:
    ahead = run_time + timedelta(days=90)
    async with open_repository(db_path) as repository:
        for when in (ahead, run_time):
            kept, _ = normalize_batch([_job("keep", "Toronto, ON", when)])
            await repository.apply_source_batch(
                SourceBatch(source="greenhouse", run_started_at=when, jobs=kept)
            )
        stored = await repository.get_job("keep")

    assert stored is not None
    assert stored.first_seen == run_time - timedelta(microseconds=1), "just before, not at, the run"


@pytest.mark.parametrize("whole_source", [False, True])
async def test_a_posting_last_seen_by_a_clock_that_ran_ahead_still_closes(
    db_path: Path, run_time: datetime, whole_source: bool
) -> None:
    from stage.domain import JobStatus

    ahead = run_time + timedelta(days=7)
    gone, kept = "greenhouse:acme:1", "greenhouse:acme:2"
    async with open_repository(db_path) as repository:
        for when, ids in ((ahead, (gone, kept)), (run_time, (kept,))):
            jobs, _ = normalize_batch([_job(ident, "Toronto, ON", when) for ident in ids])
            await repository.apply_source_batch(
                SourceBatch(
                    source="greenhouse",
                    run_started_at=when,
                    jobs=jobs,
                    closable_boards=() if whole_source else ("greenhouse:acme",),
                    closes_whole_source=whole_source,
                )
            )
        states = await repository.posting_states([gone, kept])

    assert states == {gone: JobStatus.CLOSED, kept: JobStatus.OPEN}


async def test_purge_drops_only_rejections_no_board_lists_any_more(db_path: Path) -> None:
    from stage.domain import QUARANTINE_RETENTION_DAYS

    start = datetime(2026, 9, 1, tzinfo=UTC)
    later = start + timedelta(days=QUARANTINE_RETENTION_DAYS + 5)

    def rejected(ident: str) -> QuarantinedJob:
        return QuarantinedJob(
            id=ident,
            source="greenhouse",
            company="Acme",
            title_raw="Staff Engineer",
            reason=RejectionReason.NOT_AN_INTERNSHIP,
            matched_phrase="staff engineer",
            first_seen=start,
            last_seen=start,
        )

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=start,
                quarantined=(rejected("greenhouse:still:1"), rejected("greenhouse:gone:1")),
            )
        )
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse", run_started_at=later, unchanged_boards=("greenhouse:still",)
            )
        )
        result = await repository.purge(later)
        left = await repository.list_quarantined(QuarantineFilters(limit=None))

    assert result.forgotten == 1
    assert [entry.id for entry in left] == ["greenhouse:still:1"]
