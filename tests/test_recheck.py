import multiprocessing
import time
from datetime import UTC, date, datetime
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from stage.companies import load_companies, update_registry, write_registry
from stage.domain import Company, Platform
from stage.services.health import doctor
from stage.storage import open_repository

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _company(name: str, recheck: date | None) -> Company:
    return Company(
        name=name,
        platform=Platform.GREENHOUSE,
        slug=name.lower(),
        enabled=False,
        last_verified=date(2026, 8, 11),
        notes="parked pending a re-measure",
        recheck_after=recheck,
    )


def _concurrent_registry_update(
    target: str, name: str, delay_s: float, entered: Event | None
) -> None:
    def add_latest(rows: tuple[Company, ...]) -> tuple[list[Company], None]:
        if entered is not None:
            entered.set()
        time.sleep(delay_s)
        return [*rows, _company(name, None)], None

    update_registry(add_latest, Path(target))


def test_a_row_is_due_only_once_its_date_has_arrived() -> None:
    row = _company("Parked", date(2026, 10, 1))
    assert not row.due_for_recheck(date(2026, 8, 11))
    assert not row.due_for_recheck(date(2026, 9, 30))
    assert row.due_for_recheck(date(2026, 10, 1)), "the date itself counts as due"
    assert row.due_for_recheck(date(2026, 12, 25))


def test_a_row_with_no_date_is_never_due() -> None:
    assert not _company("Undated", None).due_for_recheck(date(2099, 1, 1)), (
        "a prose-only reason must not be silently treated as an expiry"
    )


def test_the_date_round_trips_through_the_registry(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_company("Parked", date(2026, 10, 1)), _company("Plain", None)], target)
    rows = {row.name: row for row in load_companies(target)}
    assert rows["Parked"].recheck_after == date(2026, 10, 1)
    assert rows["Plain"].recheck_after is None
    assert rows["Parked"].notes, "the prose reason survives beside the structured date"


def test_a_registry_update_reads_the_latest_rows_while_holding_the_write_lock(
    tmp_path: Path,
) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_company("Existing", None)], target)

    def add_latest(rows: tuple[Company, ...]) -> tuple[list[Company], str]:
        return [*rows, _company("Added", None)], "updated"

    written, result = update_registry(add_latest, target)

    assert written == target
    assert result == "updated"
    assert {row.name for row in load_companies(target)} == {"Existing", "Added"}


def test_concurrent_registry_updates_preserve_both_changes(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_company("Existing", None)], target)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    first = context.Process(
        target=_concurrent_registry_update,
        args=(str(target), "First", 1.0, entered),
    )
    second = context.Process(
        target=_concurrent_registry_update,
        args=(str(target), "Second", 0.0, None),
    )

    first.start()
    assert entered.wait(10)
    second.start()
    first.join(10)
    second.join(10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert {row.name for row in load_companies(target)} == {"Existing", "First", "Second"}


def test_a_registry_update_preserves_its_callback_error(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_company("Existing", None)], target)

    def fail(_: tuple[Company, ...]) -> tuple[list[Company], None]:
        raise OSError("callback failed")

    with pytest.raises(OSError, match="callback failed"):
        update_registry(fail, target)


@pytest.mark.asyncio
async def test_doctor_warns_about_a_due_row_and_stays_silent_about_a_future_one(
    db_path: Path,
) -> None:
    rows = [_company("DueNow", date(2026, 8, 1)), _company("NotYet", date(2099, 1, 1))]
    async with open_repository(db_path) as repository:
        report = await doctor(repository, now=NOW, companies=rows)

    assert [entry.split(" (")[0] for entry in report.due_for_recheck] == ["DueNow"]
    assert report.warnings >= 1, "an expired reason must reach the warning count"
    assert report.is_healthy, "a due re-check is a warning, never an error that exits 1"


@pytest.mark.asyncio
async def test_doctor_without_a_registry_reports_nothing_due(db_path: Path) -> None:
    async with open_repository(db_path) as repository:
        report = await doctor(repository, now=NOW)
    assert report.due_for_recheck == ()


def test_the_shipped_registry_carries_structured_dates_not_only_prose() -> None:
    dated = [row for row in load_companies() if row.recheck_after is not None]
    assert len(dated) >= 50, f"only {len(dated)} rows carry a machine-readable expiry"
