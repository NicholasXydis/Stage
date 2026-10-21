import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.domain import (
    ExportFormat,
    Job,
    JobFilters,
    JobStatus,
    LocationBucket,
    SyncOutcome,
    SyncRun,
)
from stage.domain.tracker import (
    ApplicationStatus,
    PostingState,
    Snapshot,
    StatusChange,
    TrackedPosting,
    TrackerError,
    applied_on,
    moves_back,
    parse_day,
    parse_note,
    parse_status,
    summarize,
)
from stage.services.tracker import (
    LIST_PROBLEM,
    ChangeReport,
    TrackerRow,
    export_tracker,
    record_status,
    remove_tracked,
    tracked_for,
    tracker_csv,
    tracker_listing,
    tracker_markdown,
    tracker_path,
    tracker_view,
)
from stage.storage.tracker import TrackerStore, TrackerUnreadableError

ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOW = datetime.now(UTC)
TODAY = NOW.astimezone().date()
SAVED, APPLIED, HIDDEN = (
    ApplicationStatus.SAVED,
    ApplicationStatus.APPLIED,
    ApplicationStatus.HIDDEN,
)


def _plain(text: str) -> str:
    return " ".join(ANSI.sub("", text).split())


def _id(name: str, source: str = "greenhouse") -> str:
    return f"{source}:acme:{name}"


def _job(
    name: str,
    *,
    hours: int = 1,
    company: str = "Acme",
    title: str = "",
    url: str = "",
    source: str = "greenhouse",
    duplicate_of: str | None = None,
) -> Job:
    seen = NOW - timedelta(hours=hours)
    heading = title or f"Intern {name}"
    return Job(
        id=_id(name, source),
        source=source,
        company=company,
        title_raw=heading,
        title_normalized=heading.lower(),
        apply_url_raw=url or f"https://example.com/{name}",
        description="",
        first_seen=seen,
        last_seen=seen,
        location_raw="Montreal, QC",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        duplicate_of=duplicate_of,
    )


def _store(path: Path, jobs: list[Job]) -> str:
    from stage.storage.repository import SourceBatch
    from stage.storage.sqlite_repo import SqliteRepository

    repository = SqliteRepository.connect(path)
    repository.apply_source_batch(
        SourceBatch(source="greenhouse", run_started_at=NOW, jobs=tuple(jobs))
    )
    repository.close()
    return str(path)


def _sql(path: Path | str, statement: str, *params: object) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(statement, params)
        conn.commit()


def _synced(path: Path | str) -> None:
    from stage.storage.sqlite_repo import SqliteRepository

    repository = SqliteRepository.connect(Path(path))
    repository.record_sync_run(
        SyncRun(
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC), outcome=SyncOutcome.SUCCESS
        )
    )
    repository.close()


def _catalogue() -> list[Job]:
    return [_job(name, hours=hours) for hours, name in enumerate("abcde", start=1)]


def _run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, _plain(result.stdout), result.stderr


def _listed(db: str, *extra: str) -> str:
    code, out, _ = _run("list", "--db", db, *extra)
    assert code == 0
    return out


def _entries() -> list[TrackedPosting]:
    with TrackerStore.connect(tracker_path()) as store:
        return store.entries()


def _status_of(posting_id: str) -> ApplicationStatus | None:
    found = {entry.posting_id: entry.status for entry in _entries()}
    return found.get(posting_id)


def _snapshot(title: str = "Intern") -> Snapshot:
    return Snapshot("Acme", title, "Montreal, QC", "https://example.com/x", "greenhouse", "")


def _tracked(
    posting_id: str,
    status: ApplicationStatus = SAVED,
    *,
    on: date = TODAY,
    moment: datetime = NOW,
    title: str = "Intern",
) -> TrackedPosting:
    change = StatusChange(status, on, moment)
    return TrackedPosting(posting_id, status, on, moment, "", _snapshot(title), (change,))


def _seed(*entries: TrackedPosting) -> None:
    with TrackerStore.connect(tracker_path()) as store:
        store.write(
            entries,
            [(entry.posting_id, change) for entry in entries for change in entry.history],
            {},
        )


def test_status_names_are_forgiving_and_suggest() -> None:
    assert parse_status(" Applied ") is APPLIED
    with pytest.raises(TrackerError, match="Did you mean 'saved'"):
        parse_status("sav")
    with pytest.raises(TrackerError, match="Choose from: saved, applied"):
        parse_status("zzz")


@pytest.mark.parametrize(
    ("value", "said"),
    [
        ("2030-01-01", "is in the future"),
        ("1999-12-31", "too far back"),
        ("2026-02-30", "is not a real date"),
        ("yesterday", "Write dates like"),
        ("2026-1-5", "Write dates like"),
    ],
)
def test_dates_must_be_real_past_and_recent(value: str, said: str) -> None:
    with pytest.raises(TrackerError, match=said):
        parse_day(value, date(2026, 10, 21))


def test_a_good_date_and_today_are_accepted() -> None:
    assert parse_day(" 2026-10-21 ", date(2026, 10, 21)) == date(2026, 10, 21)


def test_notes_are_cleaned_and_bounded() -> None:
    assert parse_note("call\u202e Tuesday\n") == "call Tuesday"
    with pytest.raises(TrackerError, match="at most 1,000"):
        parse_note("x" * 1_001)


def test_quick_commands_only_move_forward() -> None:
    assert moves_back(ApplicationStatus.OFFER, SAVED)
    assert moves_back(ApplicationStatus.INTERVIEWING, APPLIED)
    assert not moves_back(SAVED, APPLIED)
    assert not moves_back(HIDDEN, SAVED)
    assert not moves_back(APPLIED, APPLIED)


def test_stats_count_recent_applications_and_replies() -> None:
    def entry(name: str, *steps: tuple[ApplicationStatus, int]) -> TrackedPosting:
        history = tuple(StatusChange(s, TODAY - timedelta(days=d), NOW) for s, d in steps)
        last = history[-1]
        return TrackedPosting(name, last.status, last.on, NOW, "", _snapshot(), history)

    entries = [
        entry("a", (SAVED, 9), (APPLIED, 3), (ApplicationStatus.INTERVIEWING, 1)),
        entry("b", (APPLIED, 20)),
        entry("c", (APPLIED, 40), (ApplicationStatus.REJECTED, 2)),
        entry("d", (SAVED, 1)),
        entry("e", (HIDDEN, 1)),
    ]

    stats = summarize(entries, TODAY)

    assert stats.tracked == 4
    assert stats.by_status[HIDDEN] == 1
    assert (stats.applied_last_7, stats.applied_last_30) == (1, 2)
    assert (stats.applications, stats.heard_back) == (3, 2)
    assert stats.replies == {
        ApplicationStatus.INTERVIEWING: 1,
        ApplicationStatus.OFFER: 0,
        ApplicationStatus.REJECTED: 1,
    }


def test_a_jump_past_applied_counts_as_applying_that_day() -> None:
    def entry(*steps: tuple[ApplicationStatus, int]) -> TrackedPosting:
        history = tuple(StatusChange(s, TODAY - timedelta(days=d), NOW) for s, d in steps)
        last = history[-1]
        return TrackedPosting("x", last.status, last.on, NOW, "", _snapshot(), history)

    jumped = entry((SAVED, 9), (ApplicationStatus.REJECTED, 2))
    corrected = entry((APPLIED, 0), (APPLIED, 12))
    offered = entry((APPLIED, 5), (ApplicationStatus.INTERVIEWING, 3), (ApplicationStatus.OFFER, 1))

    assert applied_on(jumped) == TODAY - timedelta(days=2)
    assert applied_on(corrected) == TODAY - timedelta(days=12)
    stats = summarize([jumped, corrected, offered], TODAY)
    assert (stats.applications, stats.heard_back) == (3, 2)
    assert (stats.applied_last_7, stats.applied_last_30) == (2, 3)
    assert stats.replies[ApplicationStatus.OFFER] == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_tracker_file_is_private_and_versioned() -> None:
    with TrackerStore.connect(tracker_path()):
        pass

    assert tracker_path().stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(tracker_path())) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_entries_round_trip_with_history_in_order() -> None:
    first = _tracked("one", SAVED, moment=NOW - timedelta(hours=2))
    _seed(first)
    later = StatusChange(APPLIED, TODAY, NOW)
    with TrackerStore.connect(tracker_path()) as store:
        store.write([replace(first, status=APPLIED, changed_at=NOW)], [("one", later)], {})
        store.write([_tracked("two", moment=NOW - timedelta(hours=1))], [], {})
        found = store.entries()

    assert [entry.posting_id for entry in found] == ["one", "two"]
    assert [change.status for change in found[0].history] == [SAVED, APPLIED]
    assert found[0].snapshot == _snapshot()


def test_renaming_a_posting_carries_its_history() -> None:
    _seed(_tracked("old"))
    with TrackerStore.connect(tracker_path()) as store:
        store.write([replace(_tracked("new"), history=())], [], {"old": "new"})
        found = store.entries()

    assert [entry.posting_id for entry in found] == ["new"]
    assert len(found[0].history) == 1


def test_removing_a_posting_removes_its_history() -> None:
    _seed(_tracked("gone"), _tracked("kept"))
    with TrackerStore.connect(tracker_path()) as store:
        store.remove(["gone", "never-there"])

    with closing(sqlite3.connect(tracker_path())) as conn:
        assert conn.execute("SELECT DISTINCT posting_id FROM history").fetchall() == [("kept",)]


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        ("PRAGMA user_version = 2", "newer version of Stage"),
        ("UPDATE tracked SET status = 'dreaming'", "status this Stage does not know"),
        ("UPDATE tracked SET on_day = 'soon'", "date Stage cannot read"),
        ("UPDATE tracked SET changed_at = 'later'", "time Stage cannot read"),
    ],
)
def test_a_tracker_stage_cannot_trust_is_refused(damage: str, said: str) -> None:
    _seed(_tracked("one"))
    _sql(tracker_path(), damage)

    with pytest.raises(TrackerUnreadableError, match=said):
        _entries()


def test_a_file_that_is_not_a_database_is_refused() -> None:
    tracker_path().write_bytes(b"not a database at all" * 100)

    with pytest.raises(TrackerUnreadableError):
        _entries()


def test_excluded_ids_drop_rows_and_keep_counts_true(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import sqlite_repo
    from stage.storage.sqlite_repo import SqliteRepository

    _store(db_path, _catalogue())
    repository = SqliteRepository.connect(db_path)
    try:
        small = JobFilters(limit=None, excluded_ids=frozenset({_id("a"), _id("c")}))
        listed = [job.id for job in repository.list_jobs(small)]
        assert listed == [_id("b"), _id("d"), _id("e")]
        assert repository.count_jobs(small) == 3
        monkeypatch.setattr(sqlite_repo, "_ID_CHUNK", 1)
        assert [job.id for job in repository.list_jobs(small)] == listed
        assert repository.count_search("intern", small) == 3
    finally:
        repository.close()


def test_canonical_ids_and_posting_states(db_path: Path) -> None:
    from stage.storage.sqlite_repo import SqliteRepository

    _store(db_path, [_job("main"), _job("copy", source="lever", duplicate_of=_id("main"))])
    _sql(db_path, "UPDATE jobs SET status = 'closed' WHERE id = ?", _id("main"))
    repository = SqliteRepository.connect(db_path)
    try:
        canonical = repository.canonical_ids([_id("main"), _id("copy", "lever"), "x"])
        states = repository.posting_states([_id("main"), _id("copy", "lever"), "x"])
    finally:
        repository.close()

    assert canonical == {_id("copy", "lever"): _id("main")}
    assert states == {_id("main"): JobStatus.CLOSED, _id("copy", "lever"): JobStatus.OPEN}


async def _record(
    db_path: Path,
    ids: list[str],
    status: ApplicationStatus | None,
    *,
    on: date | None = None,
    note: str | None = None,
    now: datetime | None = None,
    forward_only: bool = False,
) -> ChangeReport:
    from stage.storage import open_repository

    async with open_repository(db_path) as repository:
        return await record_status(
            repository, ids, status, on=on, note=note, now=now, forward_only=forward_only
        )


async def test_saving_records_a_snapshot_and_history(db_path: Path) -> None:
    _store(db_path, [_job("a", title="Backend Intern", url="https://acme.test/apply")])

    report = await _record(db_path, [_id("a")], SAVED)

    [entry] = _entries()
    assert entry.status is SAVED and entry.on == TODAY
    assert entry.snapshot.title == "Backend Intern"
    assert entry.snapshot.apply_url == "https://acme.test/apply"
    assert [change.status for change in entry.history] == [SAVED]
    assert len(report.changed) == 1


async def test_tracking_a_duplicate_tracks_its_main_posting(db_path: Path) -> None:
    _store(db_path, [_job("main"), _job("copy", source="lever", duplicate_of=_id("main"))])

    await _record(db_path, [_id("copy", "lever"), _id("main")], APPLIED)

    assert [entry.posting_id for entry in _entries()] == [_id("main")]


async def test_a_posting_that_becomes_a_duplicate_keeps_its_record(db_path: Path) -> None:
    _store(db_path, [_job("feed", source="simplify")])
    await _record(db_path, [_id("feed", "simplify")], SAVED, now=NOW - timedelta(hours=1))
    _store(db_path, [_job("board")])
    _sql(
        db_path,
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
        _id("board"),
        _id("feed", "simplify"),
    )

    await _record(db_path, [_id("board")], APPLIED)

    [entry] = _entries()
    assert entry.posting_id == _id("board")
    assert [change.status for change in entry.history] == [SAVED, APPLIED]


async def test_quick_commands_never_move_back_but_track_does(db_path: Path) -> None:
    _store(db_path, [_job("a")])
    await _record(db_path, [_id("a")], ApplicationStatus.OFFER)

    kept = await _record(db_path, [_id("a")], SAVED, forward_only=True)
    assert _status_of(_id("a")) is ApplicationStatus.OFFER
    assert len(kept.unchanged) == 1

    await _record(db_path, [_id("a")], SAVED)
    assert _status_of(_id("a")) is SAVED


async def test_hidden_can_always_be_replaced(db_path: Path) -> None:
    _store(db_path, [_job("a")])
    await _record(db_path, [_id("a")], HIDDEN)

    await _record(db_path, [_id("a")], SAVED, forward_only=True)

    assert _status_of(_id("a")) is SAVED


async def test_repeating_a_status_changes_nothing_but_a_date_or_note_does(db_path: Path) -> None:
    _store(db_path, [_job("a")])
    await _record(db_path, [_id("a")], APPLIED)

    same = await _record(db_path, [_id("a")], APPLIED)
    dated = await _record(db_path, [_id("a")], APPLIED, on=date(2026, 1, 5))
    noted = await _record(db_path, [_id("a")], None, note="referral")

    assert len(same.unchanged) == 1
    assert len(dated.changed) == len(noted.changed) == 1
    [entry] = _entries()
    assert entry.on == date(2026, 1, 5) and entry.note == "referral"
    assert len(entry.history) == 2


async def test_a_missing_posting_changes_nothing_and_creates_no_file(db_path: Path) -> None:
    _store(db_path, [_job("a")])

    report = await _record(db_path, [_id("a"), "greenhouse:nope:1"], SAVED)

    assert report.missing == ("greenhouse:nope:1",)
    assert not tracker_path().exists()


async def test_a_note_on_an_untracked_posting_is_refused(db_path: Path) -> None:
    _store(db_path, [_job("a")])

    report = await _record(db_path, [_id("a")], None, note="hello")

    assert report.untracked == (_id("a"),)
    assert not tracker_path().exists()


async def test_untrack_removes_every_copy_and_reports_the_rest(db_path: Path) -> None:
    from stage.storage import open_repository

    _store(
        db_path, [_job("main"), _job("copy", source="lever", duplicate_of=_id("main")), _job("b")]
    )
    _seed(_tracked(_id("copy", "lever")))

    async with open_repository(db_path) as repository:
        missing = await remove_tracked(repository, [_id("main"), "nope:x:1"])
        report = await remove_tracked(repository, [_id("main"), _id("b")])

    assert missing.missing == ("nope:x:1",)
    assert [entry.posting_id for entry in report.removed] == [_id("copy", "lever")]
    assert report.not_tracked == (_id("b"),)
    assert _entries() == []


async def test_untrack_with_no_tracker_says_nothing_was_tracked(db_path: Path) -> None:
    from stage.storage import open_repository

    _store(db_path, [_job("a")])

    async with open_repository(db_path) as repository:
        report = await remove_tracked(repository, [_id("a")])

    assert report.not_tracked == (_id("a"),)
    assert not tracker_path().exists()


async def test_the_view_hides_and_marks_through_duplicates(db_path: Path) -> None:
    from stage.storage import open_repository

    _store(
        db_path, [_job("main"), _job("copy", source="lever", duplicate_of=_id("main")), _job("b")]
    )
    _seed(_tracked(_id("copy", "lever"), HIDDEN), _tracked(_id("b"), APPLIED))

    async with open_repository(db_path) as repository:
        view = await tracker_view(repository)
        jobs = await repository.list_jobs(JobFilters(limit=None))

    assert _id("main") in view.hidden
    assert view.marks(jobs) == {_id("b"): "[A]"}


async def test_the_view_is_empty_without_a_tracker_and_never_makes_one(db_path: Path) -> None:
    from stage.storage import open_repository

    _store(db_path, [_job("a")])

    async with open_repository(db_path) as repository:
        view = await tracker_view(repository)

    assert view.statuses == {} and view.problem == ""
    assert not tracker_path().exists()


async def test_a_damaged_tracker_gives_the_view_a_problem(db_path: Path) -> None:
    from stage.storage import open_repository

    _store(db_path, [_job("a")])
    tracker_path().write_bytes(b"garbage" * 200)

    async with open_repository(db_path) as repository:
        view = await tracker_view(repository)
        found, problem = await tracked_for(repository, [_id("a")])

    assert view.problem == LIST_PROBLEM
    assert found == {} and "could not use your tracker" in problem


async def test_the_listing_reports_states_hidden_and_stats(db_path: Path) -> None:
    from stage.storage import open_repository

    _store(db_path, [_job("open"), _job("shut")])
    _sql(db_path, "UPDATE jobs SET status = 'closed' WHERE id = ?", _id("shut"))
    _seed(
        _tracked(_id("open"), APPLIED, moment=NOW - timedelta(hours=3)),
        _tracked(_id("shut"), SAVED, moment=NOW - timedelta(hours=2)),
        _tracked("greenhouse:acme:gone", SAVED, moment=NOW - timedelta(hours=1)),
        _tracked("greenhouse:acme:secret", HIDDEN),
    )

    async with open_repository(db_path) as repository:
        everything = await tracker_listing(repository)
        hidden = await tracker_listing(repository, HIDDEN)

    states = [(row.entry.posting_id.rsplit(":", 1)[1], row.state) for row in everything.rows]
    assert states == [
        ("gone", PostingState.GONE),
        ("shut", PostingState.CLOSED),
        ("open", PostingState.OPEN),
    ]
    assert everything.hidden == 1
    assert [row.entry.status for row in hidden.rows] == [HIDDEN]
    assert everything.stats.tracked == 3


def test_exports_guard_formulas_and_escape_markdown(tmp_path: Path) -> None:
    rows = [TrackerRow(_tracked("x", title="=HYPERLINK(1)|pipe"), PostingState.OPEN)]

    assert "'=HYPERLINK(1)|pipe" in tracker_csv(rows)
    assert "=HYPERLINK(1)\\|pipe" in tracker_markdown(rows, generated_at=NOW)
    written = export_tracker(rows, ExportFormat.JSON, destination=tmp_path)
    assert written.name.startswith("stage-tracker-")
    assert json.loads(written.read_text(encoding="utf-8"))[0]["posting"] == "open"


def test_an_export_refuses_pdf_and_will_not_overwrite(tmp_path: Path) -> None:
    from stage.services.export import ExportError

    rows = [TrackerRow(_tracked("x"), PostingState.OPEN)]
    target = tmp_path / "mine.csv"
    target.write_text("keep me", encoding="utf-8")

    with pytest.raises(ExportError, match="csv, json or md"):
        export_tracker(rows, ExportFormat.PDF, destination=tmp_path)
    with pytest.raises(ExportError, match="--force"):
        export_tracker(rows, ExportFormat.CSV, destination=target)
    assert target.read_text(encoding="utf-8") == "keep me"
    export_tracker(rows, ExportFormat.CSV, destination=target, force=True)
    assert "status,on,company" in target.read_text(encoding="utf-8-sig")


def test_save_applied_and_hide_from_a_listing(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)

    saved = _run("save", "1", "--db", db)
    applied = _run("applied", "2", "--note", "referral", "--db", db)
    hidden = _run("hide", "3", "--db", db)
    after = _listed(db)

    assert saved[:2] == (0, "Saved 1 posting. [S] Intern a - Acme")
    assert applied[1] == f"Marked 1 posting applied on {TODAY}. [A] Intern b - Acme"
    assert hidden[1] == "Hid 1 posting. stage untrack 3 brings it back. hidden Intern c - Acme"
    assert "[S] Intern a" in after and "[A] Intern b" in after
    assert "Intern c" not in after
    assert after.endswith("1 hidden: stage tracker --status hidden")


def test_hidden_stays_out_of_json_and_untrack_brings_it_back(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("hide", "1-2", "--db", db)

    shown = CliRunner().invoke(app, ["list", "--json", "--db", db])
    _run("tracker", "--status", "hidden", "--db", db)
    back = _run("untrack", "1-2", "--db", db)

    assert shown.exit_code == 0 and shown.stderr == ""
    titles = [row["title_raw"] for row in json.loads(shown.stdout)]
    assert titles == ["Intern c", "Intern d", "Intern e"]
    assert back[1].startswith("Stopped tracking 2 postings.")
    assert "Intern a" in _listed(db)


def test_untracked_shows_only_postings_not_acted_on(db_path: Path, tmp_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    _run("hide", "2", "--db", db)
    out = tmp_path / "rest.csv"

    listed = _listed(db, "--untracked")
    searched = _run("search", "intern", "--untracked", "--db", db)[1]
    _run("export", "--format", "csv", "--untracked", "--out", str(out), "--db", db)

    for shown in (listed, searched):
        assert "Intern a" not in shown and "Intern b" not in shown and "Intern c" in shown
    with out.open(encoding="utf-8-sig") as handle:
        assert [row["title"] for row in csv.DictReader(handle)] == [
            "Intern c",
            "Intern d",
            "Intern e",
        ]


def test_search_and_export_hide_and_say_so(db_path: Path, tmp_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("applied", "1", "--db", db)
    _run("hide", "2", "--db", db)

    searched = _run("search", "intern", "--db", db)[1]
    exported = _run("export", "--format", "csv", "--out", str(tmp_path / "x.csv"), "--db", db)[1]

    assert "[A] Intern a" in searched and "Intern b" not in searched
    assert searched.endswith("1 hidden: stage tracker --status hidden")
    assert exported.endswith("1 hidden: stage tracker --status hidden")


def test_a_sync_after_the_listing_refuses_row_numbers(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _synced(db)

    code, out, _ = _run("save", "1", "--db", db)

    assert code == 2
    assert "database has changed since that listing" in out
    assert not tracker_path().exists()


@pytest.mark.parametrize(
    ("args", "code", "said"),
    [
        (["applied", "1", "--on", "2999-01-01"], 2, "is in the future"),
        (["applied", "1", "--on", "soon"], 2, "Write dates like"),
        (["track", "1"], 2, "stage track needs --status, --on or --note."),
        (["track", "1", "--status", "intervewing"], 2, "Did you mean 'interviewing'?"),
        (["save", "1", "greenhouse:nope:1"], 1, "No posting with id 'greenhouse:nope:1'."),
        (["save", "3-1"], 2, "3-1 counts backwards; write 1-3."),
        (["save", "9"], 2, "so 9 is out of range."),
        (["save"], 2, "stage save needs a row number or a posting id."),
        (["track", "2", "--note", "hi"], 1, "Row 2 is not tracked yet"),
        (["save", "1", "--note", "x" * 1_001], 2, "at most 1,000 characters"),
    ],
)
def test_bad_input_is_explained_and_nothing_is_saved(
    db_path: Path, args: list[str], code: int, said: str
) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)

    result = _run(*args, "--db", db)

    assert result[0] == code
    assert said in result[1]
    assert not tracker_path().exists() or _entries() == []


def test_track_sets_status_date_and_note(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("applied", "1", "--on", "2026-10-01", "--db", db)

    moved = _run("track", "1", "--status", "interviewing", "--note", "call Tuesday", "--db", db)
    noted = _run("track", "1", "--note", "", "--db", db)

    assert moved[1] == "Updated 1 posting. [I] Intern a - Acme (was applied)"
    assert noted[0] == 0
    [entry] = _entries()
    assert entry.status is ApplicationStatus.INTERVIEWING and entry.note == ""
    assert [change.on for change in entry.history] == [date(2026, 10, 1), TODAY]


def test_quick_commands_say_how_to_move_back(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("track", "1", "--status", "offer", "--db", db)

    result = _run("save", "1", "--db", db)

    assert result[1] == (
        "Nothing to change. Already marked offer: Intern a - Acme "
        "Quick commands never move a status back; stage track ROW --status saved does."
    )


def test_the_tracker_lists_newest_change_first_and_numbers_rows(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    _run("applied", "2", "--db", db)

    shown = _run("tracker", "--db", db)[1]
    moved = _run("track", "1", "--status", "offer", "--db", db)[1]

    assert shown.index("Intern b") < shown.index("Intern a")
    assert "2 tracked." in shown
    assert moved == "Updated 1 posting. [O] Intern b - Acme (was applied)"


def test_the_tracker_filters_counts_and_prints_json(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("applied", "1-2", "--db", db)
    _run("track", "2", "--status", "rejected", "--db", db)
    _run("hide", "3", "--db", db)

    applied = _run("tracker", "--status", "applied", "--db", db)[1]
    stats = _run("tracker", "--stats", "--db", db)[1]
    numbers = json.loads(
        CliRunner().invoke(app, ["tracker", "--stats", "--json", "--db", db]).stdout
    )
    rows = json.loads(CliRunner().invoke(app, ["tracker", "--json", "--db", db]).stdout)

    assert "Intern a" in applied and "Intern b" not in applied
    assert stats == (
        "Tracked 2 (applied 1 | rejected 1) Applied 2 in the last 7 days, 2 in the last 30 "
        "Heard back 1 of 2 applications (50%): 1 rejection Hidden 1"
    )
    assert numbers["applications"] == 2 and numbers["heard_back"] == 1
    assert numbers["replies"] == {"interviewing": 0, "offer": 0, "rejected": 1}
    assert numbers["by_status"]["hidden"] == 1
    assert [row["status"] for row in rows] == ["rejected", "applied"]
    assert rows[0]["history"][0]["status"] == "applied"


def test_the_tracker_exports_and_refuses_conflicting_options(db_path: Path, tmp_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    target = tmp_path / "t.md"

    written = _run("tracker", "--export", "md", "--out", str(target), "--db", db)

    assert written[1].startswith("Wrote 1 tracked posting to")
    assert "| saved |" in target.read_text(encoding="utf-8")
    for args, said in [
        (["--export", "pdf"], "The tracker exports as csv, json or md."),
        (["--export", "xls"], "--export must be one of"),
        (["--stats", "--status", "saved"], "--stats counts every tracked posting"),
        (["--export", "csv", "--json"], "--export writes a file"),
        (["--out", "x.csv"], "--out and --force only apply with --export."),
        (["--status", "nope"], "Unknown status 'nope'"),
    ]:
        code, out, _ = _run("tracker", *args, "--db", db)
        assert code == 2 and said in out


def test_an_empty_tracker_says_what_to_do(db_path: Path) -> None:
    db = _store(db_path, _catalogue())

    assert _run("tracker", "--db", db)[1] == (
        "Nothing tracked yet. Run stage list, then stage save 1 or stage applied 1."
    )
    assert _run("tracker", "--status", "offer", "--db", db)[1] == "Nothing tracked as offer."
    assert not tracker_path().exists()


def test_show_includes_tracking_and_history(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    _run("applied", "1", "--note", "referral", "--db", db)

    shown = _run("show", "1", "--db", db)[1]
    data = json.loads(CliRunner().invoke(app, ["show", "1", "--json", "--db", db]).stdout)
    untracked = json.loads(CliRunner().invoke(app, ["show", "2", "--json", "--db", db]).stdout)

    assert (
        f"tracked applied on {TODAY} note referral history saved {TODAY}, applied {TODAY}" in shown
    )
    assert data["tracked"]["status"] == "applied" and data["job"]["id"] == _id("a")
    assert untracked["tracked"] is None


def test_show_and_open_fall_back_to_the_saved_copy(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("applied", "1", "--db", db)
    _sql(db, "DELETE FROM jobs WHERE id = ?", _id("a"))

    shown = _run("show", _id("a"), "--db", db)
    data = json.loads(CliRunner().invoke(app, ["show", _id("a"), "--json", "--db", db]).stdout)
    opened = _run("open", _id("a"), "--print", "--db", db)
    listed = _run("tracker", "--db", db)[1]

    assert shown[0] == 0
    assert "Stage no longer stores this posting" in shown[1]
    assert data["job"] is None and data["tracked"]["title"] == "Intern a"
    assert opened == (0, "https://example.com/a", "")
    assert listed.split("Intern a")[1].split()[0] == "gone"


def test_open_refuses_an_unsafe_saved_link(db_path: Path) -> None:
    db = _store(db_path, [_job("a", url="javascript:alert(1)")])
    _listed(db)
    _run("save", "1", "--db", db)
    _sql(db, "DELETE FROM jobs WHERE id = ?", _id("a"))

    code, out, _ = _run("open", _id("a"), "--print", "--db", db)

    assert code == 2 and "Refusing to open" in out


def test_a_damaged_tracker_never_breaks_a_listing(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    tracker_path().write_bytes(b"garbage" * 200)
    before = tracker_path().read_bytes()

    listed = _run("list", "--db", db)
    as_json = CliRunner().invoke(app, ["list", "--json", "--db", db])
    saved = _run("save", _id("a"), "--db", db)
    shown = _run("tracker", "--db", db)

    assert listed[0] == 0 and listed[1].startswith(LIST_PROBLEM)
    assert len(json.loads(as_json.stdout)) == 5 and LIST_PROBLEM in as_json.stderr
    assert saved[0] == 1 and "could not use your tracker" in saved[1]
    assert "It was left as it was." in saved[1]
    assert shown[0] == 1
    assert tracker_path().read_bytes() == before


def test_purge_leaves_the_tracker_alone(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("applied", "1", "--db", db)
    before = tracker_path().read_bytes()

    result = CliRunner().invoke(app, ["purge", "--db", db])

    assert result.exit_code == 0
    assert tracker_path().read_bytes() == before


def test_the_read_path_stays_light_with_a_tracker(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _seed(_tracked(_id("a"), HIDDEN))
    folder = str(tracker_path().parent)
    probe = (
        "import sys\n"
        "from types import SimpleNamespace\n"
        "from stage import paths\n"
        f"paths._DIRS = SimpleNamespace(user_data_dir={folder!r}, user_config_dir={folder!r})\n"
        "from stage.cli.app import app\n"
        "try:\n"
        f"    app(['list', '--json', '--db', {db!r}])\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = [n for n in ('yaml', 'stage.lexicon', 'rich.console') if n in sys.modules]\n"
        "print('LOADED:' + ','.join(loaded), file=sys.stderr)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stderr.strip().splitlines()[-1] == "LOADED:"
    assert len(json.loads(result.stdout)) == 4


def test_markup_in_a_saved_title_is_printed_not_parsed(db_path: Path) -> None:
    db = _store(db_path, [_job("a", title="[/bold]evil [link=file:///x]y")])
    _listed(db)

    saved = CliRunner().invoke(app, ["save", "1", "--db", db])
    shown = CliRunner().invoke(app, ["tracker", "--db", db])

    assert saved.exit_code == 0 and shown.exit_code == 0
    assert "[/bold]evil" in saved.stdout and "[/bold]evil" in shown.stdout


async def test_the_tui_leaves_hidden_postings_out(db_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _store(db_path, _catalogue())
    _seed(_tracked(_id("b"), HIDDEN))
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        await type(screen).refresh_results.__wrapped__(screen)  # type: ignore[attr-defined]

        assert [job.id for job in screen._jobs] == [_id(n) for n in "acde"]


async def test_the_tui_warns_once_about_a_damaged_tracker(db_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _store(db_path, _catalogue())
    tracker_path().write_bytes(b"garbage" * 200)
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        refresh = type(screen).refresh_results.__wrapped__  # type: ignore[attr-defined]
        await refresh(screen)
        await refresh(screen)
        await pilot.pause()
        messages = [str(note.message) for note in app_under_test._notifications]

        assert messages.count(LIST_PROBLEM) == 1
        assert len(screen._jobs) == 5


def test_the_help_lists_the_tracker_under_applications() -> None:
    output = ANSI.sub("", CliRunner().invoke(app, ["--help"]).stdout)
    guide = ANSI.sub("", CliRunner().invoke(app, ["help"]).stdout)

    section = output.split("Applications")[1].split("╰")[0]
    for command in ("save", "applied", "hide", "track", "untrack", "tracker"):
        assert re.search(rf"\b{command}\b", section)
    assert output.index("Everyday") < output.index("Applications") < output.index("Keeping")
    assert "stage list --untracked" in guide


def test_untrack_explains_every_way_it_can_fail(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)

    unknown = _run("untrack", "greenhouse:nope:1", "--db", db)
    never = _run("untrack", "2", "--db", db)
    _run("save", "1", "--db", db)
    _synced(db)
    stale = _run("untrack", "1", "--db", db)
    tracker_path().write_bytes(b"garbage" * 200)
    damaged = _run("untrack", _id("a"), "--db", db)

    assert unknown[0] == 1 and unknown[1].endswith("Nothing was changed.")
    assert never == (0, "Row 2 was not tracked.", "")
    assert stale[0] == 2 and "database has changed" in stale[1]
    assert damaged[0] == 1 and "could not use your tracker" in damaged[1]


def test_a_tracker_export_that_would_overwrite_is_refused(db_path: Path, tmp_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    target = tmp_path / "t.csv"
    target.write_text("keep", encoding="utf-8")

    code, out, _ = _run("tracker", "--export", "csv", "--out", str(target), "--db", db)

    assert code == 2 and "pass --force to overwrite it" in out
    assert target.read_text(encoding="utf-8") == "keep"


def test_a_big_batch_lists_twenty_and_counts_the_rest(db_path: Path) -> None:
    jobs = [_job(f"n{index:02d}", hours=index + 1) for index in range(25)]
    db = _store(db_path, jobs)
    _listed(db, "--all")

    saved = _run("save", "1-25", "--db", db)[1]
    removed = _run("untrack", "1-25", "--db", db)[1]

    assert saved.startswith("Saved 25 postings.") and saved.endswith("...and 5 more")
    assert removed.startswith("Stopped tracking 25 postings.")
    assert removed.endswith("...and 5 more")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_a_tracker_stage_cannot_write_is_explained(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    tracker_path().chmod(0o400)
    try:
        code, out, _ = _run("applied", "1", "--db", db)
    finally:
        tracker_path().chmod(0o600)

    assert code == 1
    assert "could not use your tracker" in out and "readonly" in out
    assert _status_of(_id("a")) is SAVED


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_a_folder_stage_cannot_write_is_explained(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    folder = tracker_path().parent
    folder.chmod(0o500)
    try:
        code, out, _ = _run("save", "1", "--db", db)
    finally:
        folder.chmod(0o700)

    assert code == 1
    assert "could not use your tracker" in out and "Permission denied" in out
    assert not tracker_path().exists()


def test_a_tracker_path_that_is_a_folder_is_refused() -> None:
    tracker_path().mkdir()

    with pytest.raises(TrackerUnreadableError):
        _entries()


async def test_a_refresh_after_the_screen_is_gone_does_nothing(db_path: Path) -> None:
    from textual.widgets import Static

    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _store(db_path, _catalogue())
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        await screen.query_one("#chips", Static).remove()
        await type(screen).refresh_results.__wrapped__(screen)  # type: ignore[attr-defined]

        assert len(screen._jobs) == 5


async def test_leaving_the_screen_stops_a_pending_refresh(db_path: Path) -> None:
    from stage.tui.app import StageApp
    from stage.tui.screens.postings import PostingsScreen

    db = _store(db_path, _catalogue())
    app_under_test = StageApp(Path(db), "")

    async with app_under_test.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        screen = app_under_test.screen
        assert isinstance(screen, PostingsScreen)
        stopped: list[bool] = []

        class Pending:
            def stop(self) -> None:
                stopped.append(True)

        screen._pending = Pending()  # type: ignore[assignment]
        screen.on_unmount()
        screen._pending = None

        assert stopped == [True]


def test_the_walkthrough_stats_add_up(db_path: Path) -> None:
    db = _store(db_path, _catalogue())
    _listed(db)
    _run("save", "1", "--db", db)
    _run("applied", "2", "--on", (TODAY - timedelta(days=3)).isoformat(), "--db", db)
    _run("tracker", "--db", db)
    _run("track", "1", "--status", "interviewing", "--db", db)
    _run("tracker", "--db", db)
    _run("track", "2", "--status", "rejected", "--db", db)

    stats = _run("tracker", "--stats", "--db", db)[1]

    assert stats == (
        "Tracked 2 (interviewing 1 | rejected 1) Applied 2 in the last 7 days, 2 in the last 30 "
        "Heard back 2 of 2 applications (100%): 1 interview, 1 rejection"
    )
