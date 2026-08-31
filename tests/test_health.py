from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stage.domain import (
    MIN_VOLUME_HISTORY,
    UNRECORDED_VOLUME,
    Job,
    JobStatus,
    SourceRunStats,
    SyncOutcome,
    SyncRun,
    VisitState,
    VolumePoint,
    VolumeVerdict,
    WorkdayCrawl,
    WorkdayCrawlStep,
    assess_volume,
    classify_visit,
)
from stage.services.health import doctor, statistics
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _points(*stored: int, deferred: int = 0, blocked: bool = False) -> list[VolumePoint]:
    return [VolumePoint(stored=value, deferred=deferred, blocked=blocked) for value in stored]


def test_a_source_that_goes_to_zero_from_a_steady_baseline_is_an_alert() -> None:
    signal = assess_volume("greenhouse", _points(0, 74, 74, 73, 75))
    assert signal.verdict is VolumeVerdict.COLLAPSED
    assert signal.is_alert
    assert "74" in signal.detail


def test_a_sharp_drop_short_of_zero_is_still_an_alert() -> None:
    assert assess_volume("lever", _points(8, 40, 41, 39)).verdict is VolumeVerdict.DROPPED


def test_ordinary_churn_is_not_an_alert() -> None:
    assert assess_volume("lever", _points(37, 40, 41, 39)).verdict is VolumeVerdict.HEALTHY


def test_a_rotated_run_cannot_raise_a_volume_alert() -> None:
    history = _points(0, 74, 74, 74)
    history[0] = VolumePoint(stored=0, deferred=41)
    signal = assess_volume("workday", history)
    assert signal.verdict is not VolumeVerdict.COLLAPSED, "rotation is a designed drop"


def test_a_blocked_run_cannot_raise_a_volume_alert() -> None:
    history = _points(0, 74, 74, 74)
    history[0] = VolumePoint(stored=0, blocked=True)
    signal = assess_volume("workday", history)
    assert signal.verdict is not VolumeVerdict.COLLAPSED, "a throttle is not drift"


def test_a_run_predating_the_stored_column_is_not_a_baseline_of_zero() -> None:
    history = [VolumePoint(stored=12)] + _points(*([UNRECORDED_VOLUME] * 6))
    signal = assess_volume("greenhouse", history)
    assert signal.verdict is VolumeVerdict.UNPROVEN, "-1 is unrecorded, not a zero baseline"


def test_a_drop_needs_enough_history_to_mean_anything() -> None:
    signal = assess_volume("greenhouse", _points(0, 74))
    assert signal.verdict is VolumeVerdict.UNPROVEN
    assert str(MIN_VOLUME_HISTORY) in signal.detail


def test_a_source_that_never_stored_anything_has_nothing_to_drop_from() -> None:
    assert assess_volume("workable", _points(0, 0, 0, 0)).verdict is VolumeVerdict.HEALTHY


@pytest.mark.parametrize(
    ("last_success", "failures", "expected"),
    [
        (None, 0, VisitState.FAILING),
        (None, 4, VisitState.FAILING),
        (NOW - timedelta(days=1), 1, VisitState.FAILING),
        (NOW - timedelta(days=30), 0, VisitState.STALE),
        (NOW - timedelta(days=1), 0, VisitState.HEALTHY),
    ],
)
def test_visit_states_separate_failing_from_stale(
    last_success: datetime | None, failures: int, expected: VisitState
) -> None:
    assert classify_visit(last_success, failures, NOW) is expected


def _job(job_id: str, source: str, company: str) -> Job:
    return Job(
        id=job_id,
        source=source,
        company=company,
        title_raw="Software Engineer Intern",
        title_normalized="Software Engineer Intern",
        apply_url_raw="https://example.test/1",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        status=JobStatus.OPEN,
    )


async def test_doctor_reports_a_clean_database_as_clean(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(_job("greenhouse:acme:1", "greenhouse", "Acme"),),
            )
        )
        report = await doctor(repository, now=NOW)

    assert report.integrity_problems == ()
    assert report.is_healthy
    assert report.schema_version > 0


async def test_doctor_reports_an_incomplete_workday_crawl(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=NOW,
                workday_crawls=(WorkdayCrawlStep(board="acme/careers", next_offset=40, total=117),),
            )
        )
        report = await doctor(repository, now=NOW)

    assert report.workday_crawls == (WorkdayCrawl(board="acme/careers", next_offset=40, total=117),)


async def test_doctor_names_the_source_whose_volume_collapsed(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        for stored in (40, 41, 40, 0):
            await repository.record_sync_run(
                SyncRun(
                    started_at=NOW,
                    finished_at=NOW,
                    outcome=SyncOutcome.SUCCESS,
                    sources=(SourceRunStats(source="lever", stored=stored),),
                )
            )
        report = await doctor(repository, now=NOW)

    assert [source.source for source in report.volume_alerts] == ["lever"]
    assert not report.is_healthy, "a collapsed source is an error, not a warning"


async def test_doctor_treats_a_failing_board_as_a_warning_not_an_error(db_path: Path) -> None:
    from stage.domain import CompanyVisit

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                visits=(
                    CompanyVisit(
                        board="greenhouse:dead",
                        succeeded=False,
                        error="HTTPStatusError: 404",
                        label="AeroSpike",
                    ),
                ),
            )
        )
        report = await doctor(repository, now=NOW)

    assert [board.label for board in report.failing_boards] == ["AeroSpike"]
    assert report.warnings == 1
    assert report.is_healthy, "a dead registry row is a warning, not an error"


async def test_stats_counts_duplicates_without_hiding_them(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(
                    _job("greenhouse:acme:1", "greenhouse", "Acme"),
                    _job("greenhouse:acme:2", "greenhouse", "Acme"),
                ),
            )
        )
        report = await statistics(repository)

    assert report.total_jobs == 2
    assert report.composition["source"]["greenhouse"] == 2
    assert report.schema_version > 0


async def test_success_rate_counts_boards_not_requests(db_path: Path) -> None:
    from stage.domain import CompanyVisit

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                visits=(
                    CompanyVisit(board="greenhouse:ok", succeeded=True, label="Acme"),
                    CompanyVisit(board="greenhouse:dead", succeeded=False, error="404"),
                ),
            )
        )
        report = await doctor(repository, now=NOW)

    health = next(source for source in report.sources if source.source == "greenhouse")
    assert health.success_rate == 0.5
    assert len(health.boards) == 2


async def test_a_source_with_no_boards_reports_no_success_rate(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        await repository.record_sync_run(
            SyncRun(
                started_at=NOW,
                finished_at=NOW,
                outcome=SyncOutcome.SUCCESS,
                sources=(SourceRunStats(source="simplify", stored=1340, requests=1),),
            )
        )
        report = await doctor(repository, now=NOW)

    health = next(source for source in report.sources if source.source == "simplify")
    assert health.success_rate is None, "a feed has no boards; 0% would read as failure"


async def test_the_json_views_round_trip_through_a_parser(db_path: Path) -> None:
    import json

    from stage.cli.serialize import health_to_json, stats_to_json
    from stage.services.health import statistics

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=(_job("greenhouse:acme:1", "greenhouse", "Acme"),),
            )
        )
        report = await doctor(repository, now=NOW)
        stats = await statistics(repository)

    health = json.loads(health_to_json(report))
    assert health["healthy"] is True
    assert health["schema_version"] > 0
    assert isinstance(health["sources"], list)

    parsed = json.loads(stats_to_json(stats))
    assert parsed["total_jobs"] == 1
    assert parsed["composition"]["source"]["greenhouse"] == 1


def test_the_breakdown_skips_a_column_that_is_always_unknown() -> None:
    from stage.services.health import COMPOSITION_COLUMNS

    assert "degree_requirement" not in COMPOSITION_COLUMNS
    assert "role" in COMPOSITION_COLUMNS
