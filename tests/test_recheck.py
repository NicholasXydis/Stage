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


def _paused(name: str, until: date | None) -> Company:
    return Company(
        name=name,
        platform=Platform.GREENHOUSE,
        slug=name.lower(),
        enabled=False,
        notes="paused after a rate limit",
        paused_until=until,
    )


def test_a_pause_holds_until_its_date_then_lets_go() -> None:
    row = _paused("Paused", date(2026, 8, 18))
    assert not row.pause_elapsed(date(2026, 8, 17))
    assert row.pause_elapsed(date(2026, 8, 18)), "the date itself counts as elapsed"
    assert row.pause_elapsed(date(2026, 12, 25))


def test_a_row_with_no_pause_is_never_resumed() -> None:
    assert not _paused("Parked", None).pause_elapsed(date(2099, 1, 1)), (
        "a permanently parked row must never resume by itself"
    )


def test_the_loader_resumes_a_paused_row_once_its_date_arrives(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_paused("Paused", date(2026, 8, 18))], target)

    before = load_companies(target, today=date(2026, 8, 17))[0]
    after = load_companies(target, today=date(2026, 8, 18))[0]

    assert not before.enabled and before.paused_until == date(2026, 8, 18)
    assert after.enabled, "a pause is a promise to resume, not a note somebody has to action"
    assert after.paused_until is None, "an elapsed pause is cleared so a write-back cannot re-park"


def test_a_pause_never_resumes_a_row_parked_without_one(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_paused("Dead", None)], target)
    assert not load_companies(target, today=date(2099, 1, 1))[0].enabled


def test_recheck_after_does_not_resume_anything(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_company("Parked", date(2026, 10, 1))], target)
    row = load_companies(target, today=date(2027, 1, 1))[0]
    assert not row.enabled, (
        "recheck_after asks a human to look; only paused_until resumes a row on its own"
    )
    assert row.recheck_after == date(2026, 10, 1)


def test_the_pause_round_trips_through_the_registry(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_paused("Paused", date(2026, 8, 18))], target)
    row = load_companies(target, today=date(2026, 8, 1))[0]
    assert row.paused_until == date(2026, 8, 18)
    assert row.notes, "the prose reason survives beside the structured date"


def test_a_paused_row_returns_on_its_own_date_and_not_before(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_paused("Paused", date(2026, 8, 18))], target)

    def enabled(when: date) -> int:
        return sum(1 for row in load_companies(target, today=when) if row.enabled)

    assert enabled(date(2026, 8, 17)) == 0, "a paused row stays out for every day before its date"
    assert enabled(date(2026, 8, 18)) == 1, "and comes back on its own, with no action from anyone"


def test_resuming_a_row_clears_the_date_so_a_write_back_cannot_re_park_it(tmp_path: Path) -> None:
    target = tmp_path / "companies.yaml"
    write_registry([_paused("Paused", date(2026, 8, 18))], target)
    resumed = load_companies(target, today=date(2026, 8, 18))
    assert resumed[0].paused_until is None, "the elapsed date is cleared on the way out"

    write_registry(list(resumed), target)
    again = load_companies(target, today=date(2026, 8, 1))
    assert again[0].enabled, "a write-back of resumed rows keeps them enabled, never re-parks them"
    assert again[0].paused_until is None, "and the spent pause does not come back to life"
