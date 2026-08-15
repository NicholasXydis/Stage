import plistlib
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

import stage.cli.schedule as schedule

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _success(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


def test_windows_enable_replaces_only_stage_tasks_and_uses_the_packaged_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def execute(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        values = tuple(command)
        commands.append(values)
        return _success(values)

    monkeypatch.setattr(schedule, "_backend", lambda: "windows")
    monkeypatch.setattr(schedule, "_execute", execute)

    report = schedule.enable()

    assert report.backend == "Windows Task Scheduler"
    assert all(enabled for _, enabled in report.actions)
    created = [command for command in commands if "/Create" in command]
    assert len(created) == 2
    assert all("/XML" in command for command in created)
    assert {command[command.index("/TN") + 1] for command in created} == {
        "Stage Sync",
        "Stage Discover",
    }
    sync_xml = schedule._windows_task_xml(schedule._ACTIONS[0]).decode("utf-16")
    discover_xml = schedule._windows_task_xml(schedule._ACTIONS[1]).decode("utf-16")
    assert f"T{schedule.SYNC_TIME}:00</StartBoundary>" in sync_xml
    assert "<Interval>PT6H</Interval>" in sync_xml
    assert "T10:30:00</StartBoundary>" in discover_xml
    assert "<DaysOfWeek><Monday /></DaysOfWeek>" in discover_xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in sync_xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in sync_xml
    assert "stage.cli.schedule run sync" in sync_xml
    assert "scripts/tasks.py" not in sync_xml


def test_windows_disable_only_deletes_stage_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []

    def execute(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        values = tuple(command)
        commands.append(values)
        if "/Query" in values:
            return subprocess.CompletedProcess(values, 1, "", "missing")
        return _success(values)

    monkeypatch.setattr(schedule, "_backend", lambda: "windows")
    monkeypatch.setattr(schedule, "_execute", execute)

    report = schedule.disable()

    assert not any(enabled for _, enabled in report.actions)
    deleted = [command for command in commands if "/Delete" in command]
    assert deleted == [
        ("schtasks", "/Delete", "/TN", "Stage Sync", "/F"),
        ("schtasks", "/Delete", "/TN", "Stage Discover", "/F"),
    ]


def test_macos_enable_writes_launch_agents_at_the_configured_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []

    def execute(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        values = tuple(command)
        commands.append(values)
        return _success(values)

    monkeypatch.setattr(schedule, "_backend", lambda: "macos")
    monkeypatch.setattr(
        schedule, "_launch_agent_dir", lambda: tmp_path / "Library" / "LaunchAgents"
    )
    monkeypatch.setattr(schedule, "_launch_domain", lambda: "gui/501")
    monkeypatch.setattr(schedule, "_execute", execute)

    report = schedule.enable()

    assert report.backend == "macOS launchd"
    sync_path = tmp_path / "Library" / "LaunchAgents" / "com.stage.sync.plist"
    discover_path = tmp_path / "Library" / "LaunchAgents" / "com.stage.discover.plist"
    with sync_path.open("rb") as stream:
        sync = plistlib.load(stream)
    with discover_path.open("rb") as stream:
        discover = plistlib.load(stream)
    assert sync["StartCalendarInterval"] == [{"Hour": hour, "Minute": 0} for hour in (0, 6, 12, 18)]
    assert discover["StartCalendarInterval"] == {"Hour": 10, "Minute": 30, "Weekday": 1}
    assert sync["ProgramArguments"][1:] == ["-m", "stage.cli.schedule", "run", "sync"]
    assert any(command[:2] == ("launchctl", "bootstrap") for command in commands)


def test_linux_enable_writes_persistent_user_timers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []

    def execute(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        values = tuple(command)
        commands.append(values)
        return _success(values)

    monkeypatch.setattr(schedule, "_backend", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(schedule, "_execute", execute)

    report = schedule.enable()

    assert report.backend == "Linux systemd user timers"
    root = tmp_path / "config" / "systemd" / "user"
    assert "OnCalendar=*-*-* 00,06,12,18:00:00" in (root / "stage-sync.timer").read_text(
        encoding="utf-8"
    )
    discovery = (root / "stage-discover.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Mon *-*-* 10:30:00" in discovery
    assert "Persistent=true" in discovery
    assert any(command[:3] == ("systemctl", "--user", "enable") for command in commands)


def test_scheduled_sync_runs_sync_then_doctor_in_one_rotating_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def invoke(arguments: Sequence[str]) -> int:
        calls.append(tuple(arguments))
        return 0

    monkeypatch.setattr(schedule, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_invoke_cli", invoke)

    assert schedule.run_scheduled("sync") == 0

    assert calls == [("sync",), ("doctor",)]
    assert "exit code: 0" in (tmp_path / "scheduled-sync.log").read_text(encoding="utf-8")


def test_scheduled_discovery_uses_the_bounded_unregistered_review_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def invoke(arguments: Sequence[str]) -> int:
        calls.append(tuple(arguments))
        return 0

    monkeypatch.setattr(schedule, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "_invoke_cli", invoke)

    assert schedule.run_scheduled("discover") == 0

    assert calls == [("discover", "--unregistered", "--limit", "40")]


def test_schedule_help_is_layered_and_classification_explains_required_evidence() -> None:
    from stage.cli.app import app

    runner = CliRunner()
    schedule_help = runner.invoke(app, ["schedule", "--help"])
    classify_help = runner.invoke(app, ["classify", "--help"])
    schedule_output = ANSI_ESCAPE.sub("", schedule_help.stdout)
    classify_output = ANSI_ESCAPE.sub("", classify_help.stdout)

    assert schedule_help.exit_code == 0
    assert {"enable", "status", "disable"} <= set(schedule_output.split())
    assert classify_help.exit_code == 0
    assert "Required unless --clear" in classify_output


def test_the_sync_action_runs_every_six_hours() -> None:
    from stage.cli.schedule import _ACTIONS, SYNC_EVERY_HOURS

    sync = next(action for action in _ACTIONS if action.key == "sync")
    assert sync.repeat_hours == SYNC_EVERY_HOURS
    assert sync.start_hours == (0, 6, 12, 18)
    assert 24 % SYNC_EVERY_HOURS == 0, "an uneven cadence drifts against the calendar day"


def test_the_weekly_action_is_not_repeated() -> None:
    from stage.cli.schedule import _ACTIONS

    discover = next(action for action in _ACTIONS if action.key == "discover")
    assert discover.repeat_hours is None
    assert len(discover.start_hours) == 1


def test_the_windows_trigger_repeats_across_the_day() -> None:
    from stage.cli.schedule import _ACTIONS, _windows_task_xml

    sync = next(action for action in _ACTIONS if action.key == "sync")
    xml = _windows_task_xml(sync).decode("utf-16")
    assert "<Interval>PT6H</Interval>" in xml
    assert "<Duration>P1D</Duration>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml, "a missed run must catch up"
    assert "<WakeToRun>false</WakeToRun>" in xml, "never wake a machine to fetch postings"


def test_the_systemd_timer_lists_every_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli import schedule as module

    monkeypatch.setattr(module, "_systemd_dir", lambda: tmp_path)
    sync = next(action for action in module._ACTIONS if action.key == "sync")
    module._write_systemd_units(sync)
    timer = (tmp_path / f"{module._systemd_name(sync)}.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00,06,12,18:00:00" in timer
    assert "Persistent=true" in timer, "a missed run must fire when the machine returns"


def test_the_launch_agent_lists_every_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plistlib

    from stage.cli import schedule as module

    monkeypatch.setattr(module, "_launch_agent_dir", lambda: tmp_path)
    sync = next(action for action in module._ACTIONS if action.key == "sync")
    payload = plistlib.loads(module._write_launch_agent(sync).read_bytes())
    schedule = payload["StartCalendarInterval"]
    assert isinstance(schedule, list)
    assert [entry["Hour"] for entry in schedule] == [0, 6, 12, 18]


def test_load_spreading_waits_a_bounded_random_time() -> None:
    from stage.cli.schedule import _ACTIONS, MAX_JITTER_S, _spread_load

    slept: list[float] = []
    sync = next(action for action in _ACTIONS if action.key == "sync")
    for _ in range(40):
        _spread_load(sync, slept.append)
    assert all(0.0 <= value <= MAX_JITTER_S for value in slept)
    assert max(slept) > 0.0, "spreading load means an actual offset, not always zero"


def test_a_one_off_action_is_never_delayed() -> None:
    from stage.cli.schedule import _ACTIONS, _spread_load

    slept: list[float] = []
    discover = next(action for action in _ACTIONS if action.key == "discover")
    _spread_load(discover, slept.append)
    assert slept == []
