import os
import stat
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx

from stage.companies import (
    PERSONAL_LIMIT,
    RegistryError,
    load_boards,
    load_personal,
    personal_kept,
    update_personal,
    write_registry,
)
from stage.domain import (
    Company,
    Platform,
    ProbeResult,
    ProbeVerdict,
    SourceOfRecord,
    SyncFinished,
)
from stage.paths import boards_path, data_root

if TYPE_CHECKING:
    from stage.services.boards import PruneReport

PACKAGED = (
    Company(name="Coveo", platform=Platform.GREENHOUSE, slug="coveo"),
    Company(name="Meta", platform=Platform.GREENHOUSE, slug="meta", enabled=False),
    Company(
        name="RBC",
        platform=Platform.WORKDAY,
        slug="rbc",
        workday_tenant="rbc",
        workday_site="External",
        workday_dc="wd3",
    ),
)


def _row(name: str, slug: str, **fields: object) -> Company:
    return Company(
        name=name,
        platform=Platform.GREENHOUSE,
        slug=slug,
        source_of_record=SourceOfRecord.DISCOVER,
        **fields,  # type: ignore[arg-type]
    )


def _packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: Sequence[Company]) -> Path:
    target = tmp_path / "packaged.yaml"
    write_registry(rows, target)
    monkeypatch.setattr("stage.companies.registry_path", lambda: target)
    return target


def _mine(*rows: Company, path: Path | None = None) -> Path:
    target, _ = update_personal(lambda _: (list(rows), None), path)
    return target


def test_the_list_lives_next_to_the_database_unless_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STAGE_BOARDS", raising=False)
    assert boards_path() == data_root() / "boards.yaml"
    monkeypatch.setenv("STAGE_BOARDS", str(tmp_path / "My Boards" / "list.yaml"))
    assert boards_path() == (tmp_path / "My Boards" / "list.yaml").resolve()
    monkeypatch.setenv("STAGE_BOARDS", "~/lists/boards.yaml")
    assert boards_path() == (Path.home() / "lists" / "boards.yaml").resolve()
    monkeypatch.setenv("STAGE_BOARDS", "")
    assert boards_path().name == "boards.yaml"


def test_a_missing_list_means_no_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _packaged(tmp_path, monkeypatch, PACKAGED)
    personal = load_personal()
    assert personal.rows == ()
    assert not personal.problem
    merged, _ = load_boards()
    assert [row.name for row in merged] == ["Coveo", "Meta", "RBC"]
    assert not boards_path().exists()


@pytest.mark.parametrize(
    "text",
    [
        "- name: [unclosed\n",
        "name: not a list\n",
        (
            "- name: Fresh\n  platform: greenhouse\n  slug: fresh\n"
            "- name: Typo\n  platform: greenhose\n  slug: typo\n"
        ),
        b"\xff\xfe not utf-8",
    ],
)
def test_a_broken_list_is_ignored_whole_and_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | bytes
) -> None:
    _packaged(tmp_path, monkeypatch, PACKAGED)
    target = boards_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = text if isinstance(text, bytes) else text.encode()
    target.write_bytes(raw)

    merged, personal = load_boards()

    assert personal.problem
    assert personal.rows == ()
    assert len(merged) == len(PACKAGED)
    with pytest.raises(RegistryError):
        update_personal(lambda rows: ([*rows, _row("New", "new")], None))
    assert target.read_bytes() == raw


def test_a_folder_where_the_list_should_be_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAGE_BOARDS", str(tmp_path))
    assert "folder" in load_personal().problem


def test_the_packaged_row_always_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _packaged(tmp_path, monkeypatch, PACKAGED)
    _mine(
        _row("Coveo Solutions", "COVEO"),
        _row("Meta Platforms", "meta"),
        Company(
            name="RBC Capital Markets",
            platform=Platform.WORKDAY,
            slug="rbc",
            workday_tenant="rbc",
            workday_site="CapitalMarkets",
            workday_dc="wd3",
        ),
        _row("Fresh", "fresh"),
    )

    merged, personal = load_boards()

    assert len(personal.rows) == 4
    assert [row.name for row in merged] == ["Coveo", "Meta", "RBC", "Fresh", "RBC Capital Markets"]
    meta = [row for row in merged if row.slug.lower() == "meta"]
    assert [(row.name, row.enabled) for row in meta] == [("Meta", False)]


def test_a_board_the_packaged_list_later_ships_drops_out_of_the_personal_rows() -> None:
    mine = (_row("Zyx", "zyx"), _row("Other", "other"))
    assert personal_kept((), mine) == mine
    shipped = (Company(name="Zyx Robotics", platform=Platform.GREENHOUSE, slug="zyx"),)
    assert personal_kept(shipped, mine) == (mine[1],)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_list_is_written_for_its_owner_only(tmp_path: Path) -> None:
    target = _mine(_row("Fresh", "fresh"), path=tmp_path / "boards.yaml")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.chmod(0o644)
    _mine(_row("Fresh", "fresh"), _row("Second", "second"), path=target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_folder_with_spaces_is_created_on_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Application Support" / "stage data"
    monkeypatch.setenv("STAGE_BOARDS", str(folder / "my boards.yaml"))
    target = _mine(_row("Fresh", "fresh"))
    assert target == (folder / "my boards.yaml").resolve()
    assert [row.name for row in load_personal().rows] == ["Fresh"]
    assert sorted(path.name for path in folder.iterdir()) == [
        ".my boards.yaml.lock",
        "my boards.yaml",
    ]


def test_an_update_that_changes_nothing_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "boards.yaml"
    update_personal(lambda rows: (rows, None), target)
    assert not target.exists()


@pytest.mark.parametrize("failing", ["os.fsync", "pathlib.Path.replace"])
def test_a_failed_write_leaves_the_old_list_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    target = _mine(_row("Kept", "kept"), path=tmp_path / "boards.yaml")
    before = target.read_bytes()

    def refuse(*_: object) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(failing, refuse)
        with pytest.raises(RegistryError, match="could not be written"):
            update_personal(lambda rows: ([*rows, _row("New", "new")], None), target)

    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_read_only_board_list_is_left_as_it_was(tmp_path: Path) -> None:
    target = _mine(_row("Kept", "kept"), path=tmp_path / "boards.yaml")
    before = target.read_bytes()
    target.chmod(0o444)

    with pytest.raises(RegistryError, match="read-only"):
        update_personal(lambda rows: ([*rows, _row("New", "new")], None), target)

    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o444


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_board_list_keeps_its_link(tmp_path: Path) -> None:
    real = _mine(_row("Kept", "kept"), path=tmp_path / "dotfiles" / "boards.yaml")
    target = tmp_path / "boards.yaml"
    target.symlink_to(real)

    update_personal(lambda rows: ([*rows, _row("New", "new")], None), target)

    assert target.is_symlink()
    assert [row.name for row in load_personal(target).rows] == ["Kept", "New"]


def test_two_adopters_racing_both_land(tmp_path: Path) -> None:
    target = tmp_path / "boards.yaml"
    inside = threading.Event()

    def slow(rows: tuple[Company, ...]) -> tuple[list[Company], None]:
        inside.set()
        time.sleep(0.2)
        return [*rows, _row("First", "first")], None

    def quick(rows: tuple[Company, ...]) -> tuple[list[Company], None]:
        return [*rows, _row("Second", "second")], None

    first = threading.Thread(target=update_personal, args=(slow, target))
    first.start()
    assert inside.wait(5)
    update_personal(quick, target)
    first.join(5)

    assert {row.name for row in load_personal(target).rows} == {"First", "Second"}


def test_the_cap_counts_only_polled_rows_of_your_own() -> None:
    from stage.services.boards import room_left

    rows = [_row(f"Board {index}", f"board-{index}") for index in range(PERSONAL_LIMIT - 2)]
    rows.append(_row("Off", "off", enabled=False))
    rows.append(_row("Coveo Again", "coveo"))
    assert room_left(rows, PACKAGED) == 2
    assert room_left([*rows, _row("One", "one"), _row("Two", "two")], PACKAGED) == 0


TODAY = date(2026, 11, 4)


def _answer(row: Company, verdict: ProbeVerdict, status: int | None = None) -> ProbeResult:
    from stage.services.discover import candidate_for

    return ProbeResult(
        company=row.name,
        candidate=candidate_for(row),
        verdict=verdict,
        url="https://boards-api.greenhouse.io/",
        job_count=3 if verdict is not ProbeVerdict.MISS else None,
        detail="HttpStatusError: greenhouse returned 404" if status else "",
        status=status,
    )


def _adopted(name: str, slug: str, added: date) -> Company:
    return _row(name, slug, last_verified=added, notes=f"{added}: seen in feed postings, 4 jobs")


def _prune(
    rows: Sequence[Company],
    results: Sequence[ProbeResult],
    productive: Sequence[Company] | None = None,
) -> tuple[dict[str, Company], "PruneReport"]:
    from stage.services.boards import prune_personal

    useful = frozenset(row.registry_key for row in (rows if productive is None else productive))
    kept, report = prune_personal(rows, results, today=TODAY, productive=useful)
    return {row.name: row for row in kept}, report


def test_a_gone_board_is_switched_off_first_and_removed_a_week_later() -> None:
    from stage.services.boards import added_on, struck_on

    row = _adopted("Gone", "gone", TODAY - timedelta(days=20))
    kept, report = _prune([row], [_answer(row, ProbeVerdict.MISS, 404)])
    struck = kept["Gone"]
    assert not struck.enabled
    assert report.switched_off == ("Gone",)
    assert struck_on(struck) == TODAY
    assert added_on(struck) == TODAY - timedelta(days=20)
    assert (struck.notes or "").splitlines()[0] == row.notes

    week_ago = replace(struck, notes=f"{row.notes}\n2026-10-28: switched off, answered 404")
    kept, report = _prune([week_ago], [_answer(week_ago, ProbeVerdict.MISS, 410)])
    assert kept == {}
    assert report.removed == ("Gone",)


def test_a_second_strike_inside_the_week_waits() -> None:
    row = replace(
        _adopted("Gone", "gone", TODAY - timedelta(days=20)),
        enabled=False,
        notes=f"{TODAY - timedelta(days=20)}: seen\n{TODAY - timedelta(days=2)}: switched off, x",
    )
    kept, report = _prune([row], [_answer(row, ProbeVerdict.MISS, 404)])
    assert kept == {"Gone": row}
    assert report == type(report)()


def test_a_board_that_answers_again_comes_back_with_its_adoption_note() -> None:
    adopted = _adopted("Back", "back", TODAY - timedelta(days=30))
    struck = replace(
        adopted, enabled=False, notes=f"{adopted.notes}\n2026-10-28: switched off, answered 404"
    )
    for verdict in (ProbeVerdict.MATCH, ProbeVerdict.UNVERIFIED, ProbeVerdict.EMPTY):
        kept, report = _prune([struck], [_answer(struck, verdict)])
        assert kept["Back"].enabled
        assert kept["Back"].notes == adopted.notes
        assert kept["Back"].last_verified == TODAY
        assert report.back_on == ("Back",)


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (ProbeVerdict.ERROR, None),
        (ProbeVerdict.MISS, 403),
        (ProbeVerdict.MISS, 401),
        (ProbeVerdict.MISS, None),
    ],
)
def test_an_unclear_answer_changes_nothing(verdict: ProbeVerdict, status: int | None) -> None:
    row = _adopted("Unclear", "unclear", TODAY - timedelta(days=20))
    kept, report = _prune([row], [_answer(row, verdict, status)])
    assert kept == {"Unclear": row}
    assert report == type(report)()


def test_a_board_naming_another_company_counts_as_gone() -> None:
    row = _adopted("Renamed", "renamed", TODAY - timedelta(days=20))
    kept, _ = _prune([row], [_answer(row, ProbeVerdict.REJECTED)])
    assert not kept["Renamed"].enabled


def test_a_board_you_switched_off_yourself_stays_off_when_it_answers() -> None:
    row = replace(_adopted("Mine", "mine", TODAY - timedelta(days=20)), enabled=False)
    kept, report = _prune([row], [_answer(row, ProbeVerdict.MATCH)])
    assert not kept["Mine"].enabled
    assert report.verified == 1


def test_a_year_without_one_kept_internship_removes_a_board() -> None:
    old = _adopted("Barren", "barren", TODAY - timedelta(days=365))
    fresh = _adopted("Young", "young", TODAY - timedelta(days=364))
    useful = _adopted("Useful", "useful", TODAY - timedelta(days=400))
    handmade = _row("Handmade", "handmade", notes="picked by hand")
    kept, report = _prune([old, fresh, useful, handmade], [], [useful])
    assert sorted(kept) == ["Handmade", "Useful", "Young"]
    assert report.barren == ("Barren",)


SYNCED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"


async def _sync(db: Path, moment: datetime) -> SyncFinished:
    from stage.services.sync import sync
    from stage.storage import open_repository

    merged, personal = load_boards()
    async with open_repository(db) as repository:
        events = [
            event
            async for event in sync(
                repository,
                merged,
                sources=["greenhouse"],
                now_fn=lambda: moment,
                close_orphans=not personal.problem,
            )
        ]
    finished = events[-1]
    assert isinstance(finished, SyncFinished)
    return finished


def _postings(db: Path, board: str) -> dict[str, str]:
    import sqlite3

    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT id, status FROM jobs WHERE id GLOB ?", (f"{board}:*",)
        ).fetchall()
    return {str(identifier): str(status) for identifier, status in rows}


def _boards_answer() -> None:
    import json

    import httpx

    respx.get("https://boards-api.greenhouse.io/v1/boards/coveo/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/zyx/jobs").mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
    )


@respx.mock
async def test_your_board_is_synced_and_closes_only_once_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _packaged(tmp_path, monkeypatch, PACKAGED[:1])
    _mine(_row("Zyx", "zyx"))
    _boards_answer()
    db = tmp_path / "stage.db"

    await _sync(db, SYNCED)
    first = _postings(db, "greenhouse:zyx")
    assert first and set(first.values()) == {"open"}

    await _sync(db, SYNCED + timedelta(days=1))
    assert _postings(db, "greenhouse:zyx") == first

    _mine()
    await _sync(db, SYNCED + timedelta(days=2))
    assert set(_postings(db, "greenhouse:zyx").values()) == {"closed"}


@respx.mock
async def test_a_damaged_list_closes_none_of_your_boards_postings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _packaged(tmp_path, monkeypatch, PACKAGED[:1])
    target = _mine(_row("Zyx", "zyx"))
    _boards_answer()
    db = tmp_path / "stage.db"
    await _sync(db, SYNCED)
    first = _postings(db, "greenhouse:zyx")

    target.write_text("- name: [broken\n", encoding="utf-8")
    await _sync(db, SYNCED + timedelta(days=1))
    assert _postings(db, "greenhouse:zyx") == first, "a list Stage cannot read removes nothing"


@respx.mock
async def test_a_board_stage_starts_shipping_keeps_its_postings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _packaged(tmp_path, monkeypatch, PACKAGED[:1])
    _mine(_row("Zyx", "zyx"))
    _boards_answer()
    db = tmp_path / "stage.db"
    await _sync(db, SYNCED)
    first = _postings(db, "greenhouse:zyx")

    shipped = Company(name="Zyx Robotics", platform=Platform.GREENHOUSE, slug="zyx")
    _packaged(tmp_path, monkeypatch, (*PACKAGED[:1], shipped))
    merged, personal = load_boards()
    assert [row.name for row in merged] == ["Coveo", "Zyx Robotics"]
    assert len(personal.rows) == 1

    finished = await _sync(db, SYNCED + timedelta(days=1))
    assert (finished.added, finished.closed) == (0, 0)
    assert _postings(db, "greenhouse:zyx") == first
