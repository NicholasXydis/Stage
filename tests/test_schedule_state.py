import asyncio
import io
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast

import pytest
from rich.console import Console

from stage.cli import schedule
from stage.cli.render import render_discovery, render_sync
from stage.cli.schedule_state import ScheduleStateWriter, read_state_path, state_for_status
from stage.domain import (
    CompanyFinished,
    DiscoveryEvent,
    DiscoveryFinished,
    DiscoveryStarted,
    Platform,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    SyncStarted,
)


def test_scheduled_run_records_wait_start_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    waited: list[float] = []

    def invoke(
        arguments: Sequence[str],
        stream: TextIO,
        *,
        progress_path: Path | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> int:
        calls.append((tuple(arguments), progress_path))
        if arguments == ("sync",):
            assert progress_path is not None
            writer = ScheduleStateWriter.open(progress_path, "sync")
            writer.sync_event(SyncStarted(("ashby",), 2, datetime(2026, 8, 21, tzinfo=UTC)))
            writer.sync_event(CompanyFinished("ashby", "Example", 4, 15.0))
            writer.sync_event(SyncFinished(SyncOutcome.SUCCESS, 1, 0, 0, (), 30.0))
        return 0

    monkeypatch.setattr(schedule, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "state_path", lambda _: tmp_path / "sync.json")
    monkeypatch.setattr(schedule, "_invoke_cli", invoke)

    assert schedule.run_scheduled("sync", jitter_seconds=2.4, sleeper=waited.append) == 0

    state = read_state_path(tmp_path / "sync.json")
    assert state is not None
    assert state["phase"] == "finished"
    assert state["outcome"] == SyncOutcome.SUCCESS.value
    assert state["jitter_seconds"] == 2
    assert state["sync_exit_code"] == 0
    assert state["doctor_exit_code"] == 0
    assert waited == pytest.approx([1.0, 1.0, 0.4])
    assert calls == [(("sync",), tmp_path / "sync.json"), (("doctor",), None)]
    log = (tmp_path / "scheduled-sync.log").read_text(encoding="utf-8")
    assert "Triggered:" in log
    assert "Waiting 2s before work starts." in log
    assert "Work started:" in log
    assert "Running health check." in log
    assert "Finished:" in log


def test_scheduled_child_uses_explicit_log_handles_and_heartbeats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []
    waits = 0
    heartbeats = 0

    class Process:
        def wait(self, *, timeout: float) -> int:
            nonlocal waits
            waits += 1
            if waits == 1:
                raise subprocess.TimeoutExpired([], timeout)
            return 0

    def popen(*args: object, **kwargs: object) -> Process:
        calls.append({"args": args, "kwargs": kwargs})
        return Process()

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(subprocess, "Popen", popen)
    with (tmp_path / "scheduled.log").open("w", encoding="utf-8") as stream:
        assert (
            schedule._invoke_cli(
                ("sync",), stream, progress_path=tmp_path / "sync.json", heartbeat=heartbeat
            )
            == 0
        )

    arguments = cast(tuple[object, ...], calls[0]["args"])
    command = cast(list[str], arguments[0])
    assert command[:4] == [sys.executable, "-u", "-m", "stage"]
    assert command[-2:] == ["--scheduled-progress", str(tmp_path / "sync.json")]
    kwargs = cast(dict[str, object], calls[0]["kwargs"])
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdout"] is not None
    assert heartbeats == 1


def test_windows_command_uses_pythonw_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_launcher: Callable[[int], bytes]
) -> None:
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(windows_launcher(2))
    monkeypatch.setattr(schedule, "_backend", lambda: "windows")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))

    action = next(action for action in schedule._ACTIONS if action.key == "sync")

    assert schedule._command(action)[0] == str(pythonw)


def test_windows_command_falls_back_to_python_when_pythonw_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python.exe"
    monkeypatch.setattr(schedule, "_backend", lambda: "windows")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))

    action = next(action for action in schedule._ACTIONS if action.key == "sync")

    assert schedule._command(action)[0] == str(python)


def test_sync_renderer_invokes_optional_progress_sink() -> None:
    started = datetime(2026, 8, 21, tzinfo=UTC)
    seen: list[object] = []

    async def events() -> AsyncIterator[SyncEvent]:
        yield SyncStarted(("ashby",), 1, started)
        yield CompanyFinished("ashby", "Example", 2, 15.0)
        yield SyncFinished(SyncOutcome.SUCCESS, 1, 0, 0, (), 30.0)

    outcome = asyncio.run(render_sync(Console(file=io.StringIO()), events(), progress=seen.append))

    assert outcome is SyncOutcome.SUCCESS
    assert len(seen) == 3


def test_discovery_renderer_invokes_optional_progress_sink() -> None:
    seen: list[object] = []

    async def events() -> AsyncIterator[DiscoveryEvent]:
        yield DiscoveryStarted(("Example",), (Platform.ASHBY,), 1)
        yield DiscoveryFinished((), (), (), 0, 0, 0, 0.0)

    asyncio.run(render_discovery(Console(file=io.StringIO()), events(), progress=seen.append))

    assert len(seen) == 2


def test_state_writer_persists_progress_as_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "sync.json"
    writer = ScheduleStateWriter.start("sync", tmp_path / "scheduled.log", destination=target)
    writer.waiting(3)
    writer.started("syncing")
    writer.sync_event(SyncStarted(("ashby",), 2, datetime(2026, 8, 21, tzinfo=UTC)))
    writer.sync_event(CompanyFinished("ashby", "Example", 4, 15.0))
    writer.sync_event(SyncFinished(SyncOutcome.SUCCESS, 1, 0, 0, (), 30.0))
    writer.complete(sync_exit_code=0, doctor_exit_code=0, exit_code=0)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["phase"] == "finished"
    assert payload["outcome"] == SyncOutcome.SUCCESS.value
    assert payload["progress"]["total"] == 2
    assert payload["progress"]["complete"] == 1
    assert payload["progress"]["fetched"] == 4


def test_stale_heartbeat_is_unresponsive_without_claiming_interruption() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    payload = {
        "phase": "syncing",
        "heartbeat_at": (now - timedelta(seconds=4)).isoformat(),
        "heartbeat_interval_seconds": 1,
    }

    state = state_for_status(payload, now=now)

    assert state is not None
    assert state["phase"] == "unresponsive"
    assert "interrupted" not in state
    assert state_for_status({"phase": "syncing"}, now=now) == {"phase": "syncing"}


def test_state_reader_retries_when_another_process_holds_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sync.json"
    target.write_text('{"phase": "syncing"}', encoding="utf-8")
    original = Path.open
    attempts = 0

    def open_path(path: Path, *args: Any, **kwargs: Any) -> TextIO:
        nonlocal attempts
        if path == target and attempts == 0:
            attempts += 1
            raise PermissionError("sharing violation")
        return cast(TextIO, original(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", open_path)

    assert read_state_path(target) == {"phase": "syncing"}
    assert attempts == 1


def test_state_writer_retries_an_atomic_replace_after_a_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sync.json"
    original = Path.replace
    attempts = 0

    def replace(path: Path, destination: Path) -> Path:
        nonlocal attempts
        if path.parent == tmp_path and attempts == 0:
            attempts += 1
            raise PermissionError("sharing violation")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    writer = ScheduleStateWriter.start("sync", tmp_path / "scheduled.log", destination=target)

    writer.waiting(1)

    assert attempts == 1
    assert read_state_path(target) is not None


def test_scheduled_lock_contention_skips_doctor_and_remains_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def invoke(
        arguments: Sequence[str],
        stream: TextIO,
        *,
        progress_path: Path | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> int:
        calls.append(tuple(arguments))
        assert progress_path is not None
        writer = ScheduleStateWriter.open(progress_path, "sync")
        writer.blocked("another Stage sync is already running")
        return 2

    monkeypatch.setattr(schedule, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "state_path", lambda _: tmp_path / "sync.json")
    monkeypatch.setattr(schedule, "_invoke_cli", invoke)

    assert schedule.run_scheduled("sync", jitter_seconds=0) == 2

    state = read_state_path(tmp_path / "sync.json")
    assert state is not None
    assert state["phase"] == "blocked"
    assert "already running" in str(state["error"])
    assert calls == [("sync",)]


def test_state_writer_does_not_interrupt_work_when_state_storage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sync.json"

    def replace(_: Path, __: Path) -> Path:
        raise OSError("storage unavailable")

    monkeypatch.setattr(Path, "replace", replace)
    writer = ScheduleStateWriter.start("sync", tmp_path / "scheduled.log", destination=target)

    writer.waiting(1)

    assert not target.exists()
