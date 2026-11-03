import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast

import pytest
from rich.console import Console

from stage.cli import schedule
from stage.cli.render.progress import render_discovery, render_sync
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
    assert calls == [
        (("sync",), tmp_path / "sync.json"),
        (("doctor", "--skip-file-check"), None),
    ]
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

    contained: list[int] = []

    class Process:
        pid = 4321

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
    monkeypatch.setattr(schedule, "_contain", contained.append)
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
    assert contained == ([4321] if sys.platform == "win32" else [])


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


@pytest.mark.parametrize("interval", [float("inf"), float("nan"), 10**400, True])
def test_a_garbled_heartbeat_interval_is_ignored(interval: object) -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    payload = {
        "phase": "syncing",
        "heartbeat_at": (now - timedelta(seconds=4)).isoformat(),
        "heartbeat_interval_seconds": interval,
    }

    assert state_for_status(payload, now=now) == payload


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


def test_a_heartbeat_from_a_stale_copy_keeps_what_the_child_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sync.json"
    runner = ScheduleStateWriter.start("sync", tmp_path / "scheduled.log", destination=target)
    runner.started("syncing")
    monkeypatch.setattr(runner, "_reload", lambda: None)
    child = ScheduleStateWriter.open(target, "sync")
    child.sync_event(
        SyncFinished(
            outcome=SyncOutcome.PARTIAL,
            added=1,
            updated=0,
            closed=0,
            failed_sources=("x",),
            elapsed_ms=1.0,
        )
    )
    child.blocked("another Stage sync is already running")

    runner.heartbeat()

    state = read_state_path(target)
    assert state is not None
    assert (state["phase"], state["sync_outcome"]) == ("blocked", "partial")
    assert state["heartbeat_at"] != state["triggered_at"]


STOPPABLE = """
import subprocess, sys
from pathlib import Path
from stage.cli import schedule

folder = Path(sys.argv[1])
real = subprocess.Popen
script = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"


def child(command, **kwargs):
    return real([sys.executable, "-c", script, str(folder / "child.pid")], **kwargs)


schedule.subprocess.Popen = child
schedule.state_path = lambda _: folder / "sync.json"
schedule._log_dir = lambda: folder
raise SystemExit(schedule.run_scheduled("sync", jitter_seconds=float(sys.argv[2])))
"""


def _runner(tmp_path: Path, jitter: float) -> "subprocess.Popen[bytes]":
    runner = subprocess.Popen([sys.executable, "-c", STOPPABLE, str(tmp_path), str(jitter)])
    ready = tmp_path / ("sync.json" if jitter else "child.pid")
    deadline = time.monotonic() + 30
    while not (ready.exists() and ready.stat().st_size) and time.monotonic() < deadline:
        time.sleep(0.05)
    return runner


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("jitter", [0.0, 30.0])
def test_a_stopped_runner_stops_its_sync_and_says_so(tmp_path: Path, jitter: float) -> None:
    runner = _runner(tmp_path, jitter)

    runner.send_signal(signal.SIGTERM)

    assert runner.wait(timeout=30) == 128 + signal.SIGTERM
    state = read_state_path(tmp_path / "sync.json")
    assert state is not None
    assert (state["phase"], state["outcome"]) == ("finished", "failure")
    assert "SIGTERM" in str(state["error"])
    assert "Stopped by SIGTERM" in (tmp_path / "scheduled-sync.log").read_text(encoding="utf-8")
    pid_file = tmp_path / "child.pid"
    if pid_file.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), 0)
    else:
        assert jitter, "the sync starts only after the wait"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_ending_the_runner_task_ends_its_sync(tmp_path: Path) -> None:
    runner = _runner(tmp_path, 0.0)
    child = (tmp_path / "child.pid").read_text(encoding="utf-8")

    runner.kill()
    runner.wait(timeout=30)

    tasklist = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32" / "tasklist.exe"
    deadline = time.monotonic() + 15
    listed = child
    while child in listed and time.monotonic() < deadline:
        time.sleep(0.2)
        listed = subprocess.run(
            [str(tasklist), "/FI", f"PID eq {child}", "/NH"], capture_output=True, text=True
        ).stdout
    assert child not in listed


def test_the_job_limits_match_the_windows_layout() -> None:
    import ctypes

    limits = schedule._job_limits()
    basic = type(limits().BasicLimitInformation)
    flags = basic.LimitFlags
    wide = ctypes.sizeof(ctypes.c_void_p) == 8

    assert ctypes.sizeof(basic) == (64 if wide else 48)
    assert ctypes.sizeof(limits) == (144 if wide else 112)
    assert (flags.offset, flags.size) == (16, 4)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_closing_the_job_ends_the_child_it_holds() -> None:
    if sys.platform == "win32":
        import ctypes

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            schedule._contain(child.pid)
            assert schedule._JOBS, "the child was not put in a kill-on-close job"
            began = time.monotonic()
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(schedule._JOBS.pop()))
            child.wait(timeout=15)
            assert time.monotonic() - began < 15, "the job did not end the child"
        finally:
            child.kill()


def test_a_doctor_warning_marks_a_clean_sync_for_attention_but_not_as_failed() -> None:
    from stage.cli.schedule_state import _outcome

    assert _outcome("success", 0, 1) == "partial"
    assert _outcome("partial", 1, 1) == "partial"
    assert _outcome("success", 0, 0) == "success"
    assert _outcome("success", 1, 0) == "partial"
    assert _outcome("failure", 1, 1) == "failure"
    assert _outcome("partial", 2, 1) == "failure", "a sync that exits 2 failed"
    assert _outcome("success", 0, 2) == "failure", "a doctor error is a failure"
    assert _outcome("success", -9, 0) == "failure", "a killed sync failed"
    assert _outcome(None, 1, 1) == "failure", "a sync that crashed recorded nothing"
    assert _outcome(None, 0, 0) == "success"


def test_schedule_status_prints_board_text_as_text() -> None:
    import io

    from rich.console import Console

    from stage.cli.render.schedule import render_scheduled_run

    buffer = io.StringIO()
    console = Console(file=buffer, width=200, emoji=False)
    render_scheduled_run(
        console,
        {
            "phase": "finished",
            "outcome": "success",
            "finished_at": "[bold",
            "exit_code": 0,
            "progress": {
                "source": "feed",
                "company": "Evil [/bold] Corp",
                "complete": 1,
                "total": 2,
            },
        },
    )
    render_scheduled_run(console, {"phase": "blocked", "error": "[link=file:///x]held[/link]"})
    said = buffer.getvalue()

    assert "Evil [/bold] Corp" in said and "[bold" in said
    assert "[link=file:///x]held[/link]" in said
