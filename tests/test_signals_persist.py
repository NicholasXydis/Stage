from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.classify import screen_is_internship
from stage.domain import (
    Job,
    JobFilters,
    JobStatus,
    LocationBucket,
    RemoteScope,
    RoleCategory,
    SourceSignals,
)
from stage.services.maintenance import rescreen
from stage.storage import open_repository
from stage.storage.repository import SourceBatch
from stage.storage.sqlite_repo import SqliteRepository

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

MARKERLESS = "AI Compiler and Library Engineer"


def _job(
    identifier: str,
    title: str,
    *,
    employment_type: str = "",
    category: str = "",
    location: LocationBucket = LocationBucket.MONTREAL,
    location_raw: str = "Montreal, QC",
    remote_scope: RemoteScope | None = None,
) -> Job:
    return Job(
        id=identifier,
        source="workday",
        company="Example Corp",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=f"https://boards.example.test/{identifier}",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        location_raw=location_raw,
        location=location,
        remote_scope=remote_scope,
        role=RoleCategory.SWE,
        status=JobStatus.OPEN,
        signals=SourceSignals(employment_type=employment_type, category=category),
    )


def test_a_structured_employment_type_admits_a_markerless_title() -> None:
    without = screen_is_internship(_job("a", MARKERLESS))
    with_signal = screen_is_internship(_job("b", MARKERLESS, employment_type="Intern"))
    assert without is not None, "a markerless title alone must not read as an internship"
    assert with_signal is None, "the source's employment type must admit a markerless title"


def test_employment_type_survives_a_store_and_reload(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(
                _job(
                    "workday:x:1",
                    MARKERLESS,
                    employment_type="Intern",
                    category="Data & Analytics",
                ),
            ),
        )
    )
    stored = repository.list_jobs(JobFilters(status=None))
    repository.close()
    assert len(stored) == 1, "the posting must be stored"
    assert stored[0].signals.employment_type == "Intern", (
        "employment_type must round-trip through the database or rescreen re-screens blind"
    )
    assert stored[0].signals.category == "Data & Analytics", (
        "source_category must round-trip too; screen_is_cs_role reads it"
    )


@pytest.mark.asyncio
async def test_rescreen_keeps_a_row_ingestion_admitted_on_a_structured_signal(
    db_path: Path,
) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(_job("workday:x:1", MARKERLESS, employment_type="Intern"),),
        )
    )
    repository.close()

    async with open_repository(db_path) as store:
        result = await rescreen(store, now=NOW)

    assert result.quarantined == 0, "rescreen must not eject a row ingestion correctly kept"
    check = SqliteRepository.connect(db_path)
    kept = [row["id"] for row in check._conn.execute("SELECT id FROM jobs")]
    check.close()
    assert kept == ["workday:x:1"], "the posting must remain in the jobs table"


@pytest.mark.asyncio
async def test_rescreen_is_idempotent(db_path: Path) -> None:
    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(
                _job("workday:x:1", MARKERLESS, employment_type="Intern"),
                _job("workday:x:2", "Software Engineer Intern"),
            ),
        )
    )
    repository.close()

    async with open_repository(db_path) as store:
        first = await rescreen(store, now=NOW)
        second = await rescreen(store, now=NOW)

    assert first.quarantined == 0, "the first pass must eject nothing"
    assert second.quarantined == 0, "a second pass must change nothing"
    assert second.released == 0, "a second pass must release nothing"


def test_a_remote_cs_internship_is_kept() -> None:
    from stage.services.sync import normalize_batch

    remote = _job(
        "greenhouse:acme:1",
        "Software Engineer Internship",
        location=LocationBucket.UNKNOWN,
        location_raw="Remote",
        remote_scope=RemoteScope.UNSPECIFIED,
    )
    kept, rejected = normalize_batch((remote,))
    assert len(kept) == 1, "a remote CS internship must be admitted under worldwide scope"
    assert not rejected, "naming no place is not a rejection when every place is in scope"


def test_a_same_board_link_is_never_persisted(db_path: Path) -> None:
    from stage.dedup.resolve import DuplicateLink

    board = "workday:acme-acme"
    first = _job(f"{board}:jr1", "Software Engineering Intern")
    second = _job(f"{board}:jr2", "Software Engineering Intern")

    def merge_them(incoming: Sequence[Job], existing: Sequence[Job]) -> tuple[DuplicateLink, ...]:
        return (
            DuplicateLink(
                duplicate_id=f"{board}:jr1",
                canonical_id=f"{board}:jr2",
                kind="same-language",
                evidence="identical titles",
            ),
        )

    repository = SqliteRepository.connect(db_path)
    repository.apply_source_batch(
        SourceBatch(
            source="workday",
            run_started_at=NOW,
            jobs=(first, second),
            resolve_duplicates=merge_them,
        )
    )
    linked = {
        str(row["id"]): str(row["duplicate_of"])
        for row in repository._conn.execute(
            "SELECT id, duplicate_of FROM jobs WHERE duplicate_of IS NOT NULL"
        )
    }
    repository.close()

    assert not linked, f"two requisitions on one board must never be merged: {linked}"
