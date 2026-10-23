import json
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.cli.options import _lock_path
from stage.cli.runlock import single_run
from stage.domain import (
    Company,
    CompanyFinished,
    CompanyStarted,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    SyncStarted,
)
from stage.services import background
from stage.services.background import (
    log_path,
    progress_path,
    start_sync,
    sync_command,
    sync_progress,
)

NOW = datetime.now(UTC)


def _write(database: Path, **payload: object) -> None:
    progress_path(database).write_text(json.dumps(payload), encoding="utf-8")


class _Child:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "stage.db"


@pytest.fixture(autouse=True)
def _no_children() -> Iterator[None]:
    yield
    for child in background._CHILDREN:
        if hasattr(child, "wait"):
            child.wait(timeout=30)
    background._CHILDREN.clear()


def test_no_progress_file_means_no_background_sync(database: Path) -> None:
    assert sync_progress(database) is None
    progress_path(database).write_text("not json", encoding="utf-8")
    assert sync_progress(database) is None
    progress_path(database).write_text("[1, 2]", encoding="utf-8")
    assert sync_progress(database) is None


@pytest.mark.parametrize(
    ("payload", "state"),
    [
        ({"phase": "syncing", "updated_at": NOW.isoformat()}, "running"),
        ({"phase": "waiting", "updated_at": NOW.isoformat()}, "starting"),
        ({"phase": "syncing", "updated_at": (NOW - timedelta(minutes=11)).isoformat()}, "stopped"),
        ({"phase": "syncing"}, "stopped"),
        ({"phase": "syncing", "updated_at": "not a time"}, "stopped"),
        ({"phase": "finished", "outcome": "success"}, "finished"),
        ({"phase": "finished", "outcome": "failure", "error": "bad registry"}, "failed"),
        ({"phase": "blocked", "error": "stage sync is already running"}, "blocked"),
    ],
)
def test_progress_reads_as_a_plain_state(
    database: Path, payload: dict[str, object], state: str
) -> None:
    _write(database, **payload)
    found = sync_progress(database, now=NOW)
    assert found is not None and found["state"] == state


def test_progress_counts_never_show_more_checked_than_total(database: Path) -> None:
    _write(
        database,
        phase="syncing",
        updated_at=NOW.isoformat(),
        progress={"total": 1, "complete": 12, "warnings": True},
    )
    found = sync_progress(database, now=NOW)
    assert found is not None
    assert (found["employers_checked"], found["employers_total"]) == (12, 12)
    assert found["warnings"] == 0


def _fake_launch(monkeypatch: pytest.MonkeyPatch, child: _Child, **written: object) -> list[Path]:
    calls: list[Path] = []

    def launch(database: Path) -> _Child:
        calls.append(database)
        if written:
            _write(database, **written)
        return child

    monkeypatch.setattr(background, "_launch", launch)
    return calls


def _quick(database: Path) -> background.SyncStart:
    return start_sync(database, wait=0.05, sleep=lambda _: None)


def test_a_started_sync_is_reported_as_running(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_launch(monkeypatch, _Child(), phase="syncing", updated_at=NOW.isoformat())
    launched = _quick(database)
    assert calls == [database] and launched.started and launched.detail == "The sync started."


def test_a_sync_already_held_by_another_run_is_not_started(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launch(monkeypatch, _Child(2), phase="blocked", error="stage sync is already running")
    launched = _quick(database)
    assert not launched.started and launched.detail == "stage sync is already running"


def test_a_sync_that_cannot_run_says_why(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_launch(monkeypatch, _Child(2), phase="finished", outcome="failure", error="bad registry")
    launched = _quick(database)
    assert not launched.started and launched.detail == "bad registry"


def test_a_sync_that_finishes_at_once_says_so(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launch(monkeypatch, _Child(0), phase="finished", outcome="success")
    launched = _quick(database)
    assert launched.started and launched.detail == "The sync already finished."


def test_a_sync_that_dies_before_writing_progress_shows_its_output(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launch(monkeypatch, _Child(1))
    log_path(database).write_text("Traceback\n  boom: no module named x\n", encoding="utf-8")
    launched = _quick(database)
    assert not launched.started and "boom: no module named x" in launched.detail


def test_a_slow_starting_sync_is_reported_as_starting(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launch(monkeypatch, _Child())
    launched = _quick(database)
    assert launched.started and launched.detail == "The sync is starting."


def test_a_running_sync_is_never_started_twice(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(database, phase="syncing", updated_at=datetime.now(UTC).isoformat())
    calls = _fake_launch(monkeypatch, _Child())
    launched = _quick(database)
    assert calls == [] and not launched.started
    assert launched.detail == "A sync Stage started is already running."


def test_a_stopped_sync_does_not_block_a_new_one(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(database, phase="syncing", updated_at=(NOW - timedelta(hours=2)).isoformat())
    calls = _fake_launch(monkeypatch, _Child(), phase="syncing", updated_at=NOW.isoformat())
    assert _quick(database).started and calls == [database]


def test_a_launch_the_system_refuses_is_reported(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_: Path) -> _Child:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(background, "_launch", refuse)
    launched = _quick(database)
    assert not launched.started and "Permission denied" in launched.detail


def test_the_sync_runs_this_python_on_this_database() -> None:
    database = Path("/somewhere/stage.db")
    command = sync_command(database)
    assert command[:4] == [sys.executable, "-m", "stage", "sync"]
    assert command[4:] == ["--db", str(database), "--progress-file", str(progress_path(database))]
    assert progress_path(database).name == ".stage.db.sync.json"
    assert log_path(database).name == ".stage.db.sync.log"


def test_a_real_child_process_is_launched_detached_and_reaped(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = (
        "import json, sys, datetime\n"
        "path = sys.argv[1]\n"
        "now = datetime.datetime.now(datetime.UTC).isoformat()\n"
        "json.dump({'phase': 'syncing', 'updated_at': now}, open(path, 'w'))\n"
        "print('child ran')\n"
    )
    monkeypatch.setattr(
        background,
        "sync_command",
        lambda target: [sys.executable, "-c", script, str(progress_path(target))],
    )
    launched = start_sync(database)
    assert launched.started, launched.detail
    for child in list(background._CHILDREN):
        child.wait(timeout=30)
    assert "child ran" in log_path(database).read_text(encoding="utf-8")
    sync_progress(database)
    assert background._CHILDREN == []


def _fake_sync(events: Sequence[SyncEvent]) -> Any:
    async def sync(
        repository: Any, companies: Sequence[Company], **kwargs: Any
    ) -> AsyncIterator[SyncEvent]:
        for event in events:
            yield event

    return sync


def _sync(database: Path, *extra: str) -> int:
    progress = progress_path(database)
    result = CliRunner().invoke(
        app, ["sync", "--db", str(database), "--progress-file", str(progress), *extra]
    )
    return result.exit_code


def _state(database: Path) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(progress_path(database).read_text(encoding="utf-8"))
    return found


def test_stage_sync_records_its_progress_and_finish(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = (
        SyncStarted(sources=("greenhouse",), companies=2, started_at=NOW),
        CompanyStarted(source="greenhouse", company="Acme"),
        CompanyFinished(source="greenhouse", company="Acme", fetched=3, elapsed_ms=1.0),
        CompanyFinished(source="greenhouse", company="Beta", fetched=4, elapsed_ms=1.0),
        SyncFinished(
            outcome=SyncOutcome.SUCCESS,
            added=7,
            updated=0,
            closed=0,
            failed_sources=(),
            elapsed_ms=2.0,
        ),
    )
    monkeypatch.setattr("stage.services.sync.sync", _fake_sync(events))
    assert _sync(database) == 0
    state = _state(database)
    assert state["phase"] == "finished" and state["outcome"] == "success"
    assert state["progress"]["complete"] == 2 and state["progress"]["total"] == 2
    found = sync_progress(database)
    assert found is not None and found["state"] == "finished" and found["employers_checked"] == 2


def test_stage_sync_records_a_partial_sync_as_finished_with_its_outcome(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = (
        SyncFinished(
            outcome=SyncOutcome.PARTIAL,
            added=0,
            updated=0,
            closed=0,
            failed_sources=("lever",),
            elapsed_ms=2.0,
        ),
    )
    monkeypatch.setattr("stage.services.sync.sync", _fake_sync(events))
    assert _sync(database) == 1
    found = sync_progress(database)
    assert found is not None and found["state"] == "finished" and found["outcome"] == "partial"


def test_stage_sync_records_that_another_run_holds_the_lock(database: Path) -> None:
    with single_run("sync", _lock_path(database)):
        assert _sync(database) == 2
    found = sync_progress(database)
    assert found is not None and found["state"] == "blocked"
    assert "already running" in str(found["error"])


def test_stage_sync_records_a_registry_it_cannot_read(database: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("not: [valid\n", encoding="utf-8")
    assert _sync(database, "--registry", str(broken)) == 2
    found = sync_progress(database)
    assert found is not None and found["state"] == "failed" and "YAML" in str(found["error"])


def test_stage_sync_records_flags_it_cannot_combine(database: Path) -> None:
    assert _sync(database, "--source", "lever", "--exclude", "ashby") == 2
    found = sync_progress(database)
    assert found is not None and found["state"] == "failed"


def test_stage_sync_records_a_source_that_does_not_exist(database: Path) -> None:
    assert _sync(database, "--source", "nowhere") == 2
    found = sync_progress(database)
    assert found is not None and found["state"] == "failed" and "nowhere" in str(found["error"])


def test_stage_sync_records_a_crash_as_a_failure(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*_: Any, **__: Any) -> AsyncIterator[SyncEvent]:
        before: list[SyncEvent] = []
        for event in before:
            yield event
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("stage.services.sync.sync", crash)
    assert _sync(database) == 1
    found = sync_progress(database)
    assert (
        found is not None and found["state"] == "failed" and "disk on fire" in str(found["error"])
    )


def test_a_dry_run_writes_no_progress(database: Path) -> None:
    _sync(database, "--dry-run")
    assert not progress_path(database).exists()


def test_a_sync_that_dies_without_a_log_says_so(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launch(monkeypatch, _Child(1))
    launched = _quick(database)
    assert not launched.started and launched.detail == "The sync stopped at once: no output"
