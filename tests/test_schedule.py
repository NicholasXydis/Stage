import json
import plistlib
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TextIO

import pytest
from typer.testing import CliRunner

import stage.cli.schedule as schedule
from stage.paths import data_dir

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
    assert all(entry[1] for entry in report.actions)
    created = [command for command in commands if "/Create" in command]
    assert len(created) == len(schedule._ACTIONS)
    assert all("/XML" in command for command in created)
    assert {command[command.index("/TN") + 1] for command in created} == {
        action.label for action in schedule._ACTIONS
    }
    sync_xml = schedule._windows_task_xml(schedule._ACTIONS[0]).decode("utf-16")
    discover_xml = schedule._windows_task_xml(schedule._ACTIONS[1]).decode("utf-16")
    assert f"T{schedule.SYNC_TIME}:00</StartBoundary>" in sync_xml
    assert "<Interval>PT30M</Interval>" in sync_xml
    assert "T10:30:00</StartBoundary>" in discover_xml
    assert "<DaysOfWeek><Monday /></DaysOfWeek>" in discover_xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in sync_xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in sync_xml
    assert "stage.cli.schedule run sync</Arguments>" in sync_xml
    hourly = schedule._windows_task_xml(schedule._ACTIONS[0].every(60)).decode("utf-16")
    assert "stage.cli.schedule run sync --every 60</Arguments>" in hourly
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

    assert not any(entry[1] for entry in report.actions)
    assert not any(report.needs_update), "a removed task cannot also need an update"
    deleted = [command for command in commands if "/Delete" in command]
    assert deleted == [
        ("schtasks", "/Delete", "/TN", action.label, "/F") for action in schedule._ACTIONS
    ], "disable must delete every Stage-owned task and nothing else"


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
    monkeypatch.setattr(schedule, "start_minute", lambda **_: 7)

    report = schedule.enable()

    assert report.backend == "macOS launchd"
    sync_path = tmp_path / "Library" / "LaunchAgents" / "com.stage.sync.plist"
    discover_path = tmp_path / "Library" / "LaunchAgents" / "com.stage.discover.plist"
    with sync_path.open("rb") as stream:
        sync = plistlib.load(stream)
    with discover_path.open("rb") as stream:
        discover = plistlib.load(stream)
    assert sync["StartCalendarInterval"] == [{"Minute": 7}, {"Minute": 37}]
    assert discover["StartCalendarInterval"] == {"Hour": 10, "Minute": 7, "Weekday": 1}
    assert sync["ProgramArguments"][1:] == ["-m", "stage.cli.schedule", "run", "sync"], (
        "the default cadence carries no --every, so a downgrade to 1.1.3 still runs"
    )
    assert discover["ProgramArguments"][-1] == "discover"
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
    monkeypatch.setattr(schedule, "start_minute", lambda **_: 52)

    report = schedule.enable()

    assert report.backend == "Linux systemd user timers"
    root = tmp_path / "config" / "systemd" / "user"
    assert "OnCalendar=*-*-* *:22,52:00" in (root / "stage-sync.timer").read_text(encoding="utf-8")
    discovery = (root / "stage-discover.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Mon *-*-* 10:52:00" in discovery
    assert "Persistent=true" in discovery
    assert any(command[:3] == ("systemctl", "--user", "enable") for command in commands)


def test_scheduled_sync_runs_sync_then_doctor_in_one_rotating_log(
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
        return 0

    monkeypatch.setattr(schedule, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "state_path", lambda action: tmp_path / f"{action}.json")
    monkeypatch.setattr(schedule, "_invoke_cli", invoke)

    assert schedule.run_scheduled("sync", jitter_seconds=0) == 0

    assert calls == [("sync",), ("doctor", "--skip-file-check")]
    assert "exit code: 0" in (tmp_path / "scheduled-sync.log").read_text(encoding="utf-8")


def test_scheduled_discovery_uses_the_bounded_unregistered_review_workflow(
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
        return 0

    monkeypatch.setattr(schedule, "_log_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "state_path", lambda action: tmp_path / f"{action}.json")
    monkeypatch.setattr(schedule, "_invoke_cli", invoke)

    assert schedule.run_scheduled("discover", jitter_seconds=0) == 0

    assert calls == [
        (
            "discover",
            "--unregistered",
            "--direct-only",
            "--adopt-unnamed",
            "--limit",
            "40",
            "--apply",
        )
    ]


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


def test_schedule_status_json_includes_definition_and_run_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli.app import app

    report = schedule.ScheduleStatus(
        "Windows Task Scheduler",
        tuple((action, True, action.cadence) for action in schedule._ACTIONS),
        tmp_path,
        (
            {"phase": "finished", "outcome": "success", "exit_code": 0},
            {"phase": "blocked", "error": "another run is active"},
        ),
        (False, True),
    )
    monkeypatch.setattr(schedule, "status", lambda: report)

    result = CliRunner().invoke(app, ["schedule", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["actions"][0]["run"]["outcome"] == "success"
    assert payload["actions"][1]["needs_update"] is True


def test_schedule_status_watch_stops_for_a_blocked_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli.app import app

    report = schedule.ScheduleStatus(
        "Windows Task Scheduler",
        ((schedule._ACTIONS[0], True, schedule._ACTIONS[0].cadence),),
        tmp_path,
        ({"phase": "blocked", "error": "another run holds the lock"},),
        (False,),
    )
    monkeypatch.setattr(schedule, "status", lambda: report)

    result = CliRunner().invoke(app, ["schedule", "status", "--watch"])

    assert result.exit_code == 0
    assert "another run holds the lock" in ANSI_ESCAPE.sub("", result.stdout)


def test_schedule_status_says_when_a_run_was_skipped_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli.app import app

    report = schedule.ScheduleStatus(
        "Windows Task Scheduler",
        ((schedule._ACTIONS[0], True, schedule._ACTIONS[0].cadence),),
        tmp_path,
        ({"phase": "offline", "finished_at": "2026-11-01T12:00:00+00:00"},),
        (False,),
    )
    monkeypatch.setattr(schedule, "status", lambda: report)

    result = CliRunner().invoke(app, ["schedule", "status"])

    assert result.exit_code == 0
    assert "skipped, no internet connection" in ANSI_ESCAPE.sub("", result.stdout)


def test_the_sync_action_runs_every_half_hour() -> None:
    from stage.cli.schedule import _ACTIONS, EVERY_CHOICES, SYNC_EVERY_MINUTES

    sync = next(action for action in _ACTIONS if action.key == "sync")
    assert sync.repeat_minutes == SYNC_EVERY_MINUTES == 30
    assert sync.start_minutes == (0, 30)
    assert sync.cadence == "every 30 minutes"
    assert all(1440 % choice == 0 for choice in EVERY_CHOICES), "an uneven cadence drifts"


def test_every_six_hours_keeps_the_old_calendar_on_each_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plistlib

    from stage.cli import schedule as module

    monkeypatch.setattr(module, "_launch_agent_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "_systemd_dir", lambda: tmp_path)
    sync = next(action for action in module._ACTIONS if action.key == "sync").every(360)

    assert sync.cadence == "every 6 hours"
    assert "<Interval>PT6H</Interval>" in module._windows_task_xml(sync).decode("utf-16")
    payload = plistlib.loads(module._write_launch_agent(sync).read_bytes())
    assert payload["StartCalendarInterval"] == [
        {"Hour": hour, "Minute": 0} for hour in (0, 6, 12, 18)
    ]
    module._write_systemd_units(sync)
    timer = (tmp_path / f"{module._systemd_name(sync)}.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00,06,12,18:00:00" in timer
    assert module._macos_installed(sync) == 360
    assert module._linux_installed(sync) == 360


@pytest.mark.parametrize(
    ("text", "minutes"), [("30m", 30), ("1h", 60), ("6h", 360), (" 12 hours ", 720)]
)
def test_every_reads_minutes_and_hours(text: str, minutes: int) -> None:
    assert schedule.parse_every(text) == minutes


@pytest.mark.parametrize("text", ["5m", "7h", "soon", "", "0h"])
def test_every_refuses_a_cadence_that_drifts(text: str) -> None:
    with pytest.raises(schedule.ScheduleError, match="for --every. Choose from: 30m, 1h, 2h"):
        schedule.parse_every(text)


def test_an_install_from_before_the_every_flag_needs_an_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plistlib

    monkeypatch.setattr(schedule, "_backend", lambda: "macos")
    monkeypatch.setattr(schedule, "_launch_agent_dir", lambda: tmp_path)
    sync = next(action for action in schedule._ACTIONS if action.key == "sync").every(360)
    path = schedule._write_launch_agent(sync)
    payload = plistlib.loads(path.read_bytes())
    payload["ProgramArguments"] = payload["ProgramArguments"][:-2]
    path.write_bytes(plistlib.dumps(payload))

    assert schedule._installed_cadence(sync, "macos") == "every 6 hours"
    assert not schedule._definition_matches(sync, "macos")
    schedule._write_launch_agent(sync)
    assert schedule._definition_matches(sync, "macos"), "a chosen --every is not out of date"


def test_the_weekly_action_is_not_repeated() -> None:
    from stage.cli.schedule import _ACTIONS

    discover = next(action for action in _ACTIONS if action.key == "discover")
    assert discover.repeat_minutes is None
    assert discover.every(30) is discover
    assert len(discover.start_hours) == 1


def test_the_windows_trigger_repeats_across_the_day() -> None:
    from stage.cli.schedule import _ACTIONS, _windows_task_xml

    sync = next(action for action in _ACTIONS if action.key == "sync")
    xml = _windows_task_xml(sync).decode("utf-16")
    assert "<Interval>PT30M</Interval>" in xml
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
    assert "OnCalendar=*-*-* *:00,30:00" in timer
    assert "Persistent=true" in timer, "a missed run must fire when the machine returns"


def test_the_launch_agent_lists_every_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plistlib

    from stage.cli import schedule as module

    monkeypatch.setattr(module, "_launch_agent_dir", lambda: tmp_path)
    sync = next(action for action in module._ACTIONS if action.key == "sync")
    payload = plistlib.loads(module._write_launch_agent(sync).read_bytes())
    schedule = payload["StartCalendarInterval"]
    assert schedule == [{"Minute": 0}, {"Minute": 30}]
    assert module._macos_installed(sync) == 30


def test_load_spreading_waits_a_bounded_random_time() -> None:
    from stage.cli.schedule import _ACTIONS, MAX_JITTER_S, _jitter_seconds

    sync = next(action for action in _ACTIONS if action.key == "sync")
    slept = [_jitter_seconds(sync) for _ in range(40)]
    assert all(0.0 <= value <= min(MAX_JITTER_S, 30 * 60 / 5) for value in slept)
    assert max(slept) > 0.0, "spreading load means an actual offset, not always zero"


def test_a_one_off_action_is_never_delayed() -> None:
    from stage.cli.schedule import _ACTIONS, _jitter_seconds

    discover = next(action for action in _ACTIONS if action.key == "discover")
    assert _jitter_seconds(discover) == 0.0


def test_registry_verification_is_scheduled_and_prunes_only_the_users_own_boards() -> None:
    verify = next(action for action in schedule._ACTIONS if action.key == "verify")
    assert verify.cli_arguments == ("discover", "--verify", "--apply")
    days = {action.systemd_day for action in schedule._ACTIONS if action.systemd_day}
    assert len(days) == len({action.key for action in schedule._ACTIONS if action.systemd_day}), (
        "two weekly actions on one day would stack their request budgets"
    )


def test_a_console_launcher_named_pythonw_is_not_treated_as_windowless(
    tmp_path: Path, windows_launcher: Callable[[int], bytes]
) -> None:
    console = tmp_path / "pythonw.exe"
    console.write_bytes(windows_launcher(3))
    assert not schedule._is_windowless(console), (
        "uv ships its console trampoline under both names, so the filename proves nothing"
    )


def test_a_real_gui_launcher_is_treated_as_windowless(
    tmp_path: Path, windows_launcher: Callable[[int], bytes]
) -> None:
    gui = tmp_path / "pythonw.exe"
    gui.write_bytes(windows_launcher(2))
    assert schedule._is_windowless(gui)


@pytest.mark.parametrize(
    "payload",
    [b"", b"MZ", b"not an executable at all", b"MZ" + b"\x00" * 200],
)
def test_a_file_that_is_not_a_windows_binary_is_never_windowless(
    tmp_path: Path, payload: bytes
) -> None:
    candidate = tmp_path / "pythonw.exe"
    candidate.write_bytes(payload)
    assert not schedule._is_windowless(candidate)


def test_a_missing_launcher_is_never_windowless(tmp_path: Path) -> None:
    assert not schedule._is_windowless(tmp_path / "absent.exe")


def test_the_windows_task_runs_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "windows")
    xml = schedule._windows_task_xml(schedule._ACTIONS[0]).decode("utf-16")
    assert "<Hidden>true</Hidden>" in xml, "a scheduled sync must never flash a console"


def test_a_missing_interpreter_is_reported_so_silent_failures_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = schedule._ACTIONS[0]
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    plist = agents / f"{schedule._launch_label(action)}.plist"
    plist.write_bytes(
        plistlib.dumps({"ProgramArguments": [str(tmp_path / "gone" / "python3"), "-m", "stage"]})
    )
    monkeypatch.setattr(schedule, "_launch_path", lambda _: plist)

    stale = schedule._stale_interpreter(action, "macos")

    assert stale.replace("\\", "/").endswith("gone/python3")


def test_a_present_interpreter_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = schedule._ACTIONS[0]
    existing = tmp_path / "python3"
    existing.write_text("", encoding="utf-8")
    plist = tmp_path / "agent.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(existing), "-m", "stage"]}))
    monkeypatch.setattr(schedule, "_launch_path", lambda _: plist)

    assert schedule._stale_interpreter(action, "macos") == ""


def test_selecting_one_action_leaves_the_others_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "macos")
    monkeypatch.setattr(schedule, "status", lambda: None)
    written: list[str] = []
    monkeypatch.setattr(
        schedule, "_enable_macos", lambda actions: written.extend(a.key for a in actions)
    )

    schedule.enable(["verify"])

    assert written == ["verify"]


def test_an_unknown_action_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "macos")
    monkeypatch.setattr(
        schedule,
        "_enable_macos",
        lambda _actions: pytest.fail("nothing may be written for an unknown action"),
    )

    with pytest.raises(schedule.ScheduleError, match="nosuchaction"):
        schedule.enable(["nosuchaction"])


def test_a_child_writing_progress_never_regresses_the_parent_heartbeat(tmp_path: Path) -> None:
    from stage.cli.schedule_state import ScheduleStateWriter, read_state_path, state_for_status

    path = tmp_path / "verify.json"
    parent = ScheduleStateWriter.start("verify", tmp_path / "verify.log", destination=path)
    parent.started("discovering")

    child = ScheduleStateWriter.open(path, "discover")
    parent.heartbeat()
    fresh = read_state_path(path)
    assert fresh is not None

    child._set({"progress": {"complete": 1, "total": 2}}, force=True)

    merged = read_state_path(path)
    assert merged is not None
    assert merged["heartbeat_at"] == fresh["heartbeat_at"]
    assert merged["action"] == "verify"
    reported = state_for_status(merged)
    assert reported is not None
    assert reported["phase"] != "unresponsive"


def test_status_survives_a_missing_scheduler_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise schedule.ScheduleError(f"required scheduler command is unavailable: {command[0]}")

    monkeypatch.setattr(schedule, "_backend", lambda: "windows")
    monkeypatch.setattr(schedule, "_execute", missing)

    report = schedule.status()

    assert report.backend == "Windows Task Scheduler"
    assert not any(enabled for _, enabled, _ in report.actions)
    assert not any(report.stale_interpreter)


def test_a_wednesday_task_is_not_scheduled_for_monday() -> None:
    from stage.cli.schedule import _ACTIONS, _windows_task_xml

    for action in _ACTIONS:
        if action.windows_day is None:
            continue
        xml = _windows_task_xml(action).decode("utf-16")
        expected = {"MON": "Monday", "WED": "Wednesday"}[action.windows_day]
        assert f"<{expected}" in xml, action.label


def test_the_start_minute_is_picked_once_per_computer_and_kept() -> None:
    first = schedule.start_minute(create=True)
    assert 0 <= first < 60
    assert all(schedule.start_minute(create=True) == first for _ in range(5))
    assert schedule.start_minute() == first


def test_without_a_stored_minute_the_old_calendar_is_assumed() -> None:
    assert schedule.start_minute() == 0
    path = data_dir() / "schedule" / schedule.START_MINUTE_NAME
    assert not path.exists(), "reading the status must not pick a minute"


@pytest.mark.parametrize("stored", ["", "sixty", "75", "-3"])
def test_a_damaged_start_minute_is_replaced(stored: str) -> None:
    path = data_dir() / "schedule" / schedule.START_MINUTE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stored, encoding="utf-8")
    assert schedule.start_minute() == 0
    assert 0 <= schedule.start_minute(create=True) < 60
    assert path.read_text(encoding="utf-8").strip().isdigit()


def test_two_computers_spread_over_the_whole_half_hour() -> None:
    sync = next(action for action in schedule._ACTIONS if action.key == "sync")
    discover = next(action for action in schedule._ACTIONS if action.key == "discover")
    assert sync.placed(7).start_minutes == (7, 37)
    assert sync.placed(51).start_minutes == (21, 51)
    assert sync.placed(51).when == "at :21 and :51"
    assert sync.placed(51).every(360).when == "from 00:21"
    assert discover.placed(51).time == "10:51"
    assert discover.placed(51).when == "at 10:51"


def test_status_reads_the_minute_enable_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(schedule, "_execute", _success)

    report = schedule.enable()

    assert report.actions and not any(report.needs_update)
    minute = schedule.start_minute()
    sync = next(action for action, _, _ in report.actions if action.key == "sync")
    assert sync.start_minutes == (minute % 30, minute % 30 + 30)


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [(0, "yes\n", True), (0, "no\n", False), (1, "", None), (0, "maybe", None)],
)
def test_linger_is_read_from_logind(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, expected: bool | None
) -> None:
    seen: list[tuple[str, ...]] = []

    def query(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen.append(tuple(command))
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    monkeypatch.setattr(schedule, "_query", query)
    assert schedule.lingers() is expected
    assert seen[0][:2] == ("loginctl", "show-user")


def test_status_warns_when_linux_will_stop_the_timers_at_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stage.cli.app import app

    sync = next(action for action in schedule._ACTIONS if action.key == "sync")
    report = schedule.ScheduleStatus(
        "Linux systemd user timers",
        ((sync, True, "every 30 minutes"),),
        Path("logs"),
        lingering=False,
    )
    monkeypatch.setattr(schedule, "status", lambda: report)
    raw = ANSI_ESCAPE.sub("", CliRunner().invoke(app, ["schedule", "status"]).stdout)
    output = " ".join(raw.split())
    assert "sudo loginctl enable-linger" in output
    assert "at :00 and :30" in output

    monkeypatch.setattr(schedule, "status", lambda: replace(report, lingering=True))
    output = CliRunner().invoke(app, ["schedule", "status"]).stdout
    assert "enable-linger" not in output


def test_doctor_advice_names_an_outdated_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "macos")
    monkeypatch.setattr(schedule, "_launch_agent_dir", lambda: tmp_path)
    assert schedule.advice() == (), "no schedule means nothing to update"

    sync = next(action for action in schedule._ACTIONS if action.key == "sync").every(360)
    path = schedule._write_launch_agent(sync)
    payload = plistlib.loads(path.read_bytes())
    payload["ProgramArguments"] = payload["ProgramArguments"][:-2]
    path.write_bytes(plistlib.dumps(payload))
    assert any("stage schedule enable" in hint for hint in schedule.advice())

    schedule._write_launch_agent(sync)
    assert schedule.advice() == (), "a deliberate --every 6h is up to date"


def test_doctor_advice_warns_about_linger_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "linux")
    monkeypatch.setattr(schedule, "_systemd_dir", lambda: tmp_path)
    monkeypatch.setattr(schedule, "lingers", lambda: False)
    sync = next(action for action in schedule._ACTIONS if action.key == "sync")
    schedule._write_systemd_units(sync)
    hints = schedule.advice()
    assert len(hints) == 1 and "enable-linger" in hints[0]


def _macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule, "_backend", lambda: "macos")
    monkeypatch.setattr(schedule, "_launch_agent_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr(schedule, "_launch_domain", lambda: "gui/501")
    monkeypatch.setattr(schedule, "_execute", _success)


def _save_minute(minute: int) -> None:
    path = data_dir() / "schedule" / schedule.START_MINUTE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{minute}\n", encoding="utf-8")


def test_status_shows_the_installed_minutes_and_flags_a_moved_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _macos(tmp_path, monkeypatch)
    for action in schedule._ACTIONS:
        schedule._write_launch_agent(action.placed(7))

    _save_minute(12)
    moved = schedule.status()
    _save_minute(7)
    kept = schedule.status()

    sync, discover = moved.actions[0][0], moved.actions[1][0]
    assert sync.when == "at :07 and :37" and discover.when == "at 10:07"
    assert all(moved.needs_update)
    assert not any(kept.needs_update)


def test_the_same_interpreter_under_another_name_is_not_out_of_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _macos(tmp_path, monkeypatch)
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("", encoding="utf-8")
    (bin_dir / "python3").symlink_to(bin_dir / "python")
    other = tmp_path / "base" / "python"
    other.parent.mkdir()
    other.symlink_to(bin_dir / "python")
    sync = schedule._ACTIONS[0]

    monkeypatch.setattr("sys.executable", str(bin_dir / "python3"))
    schedule._write_launch_agent(sync)
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
    assert schedule._definition_matches(sync, "macos")

    monkeypatch.setattr("sys.executable", str(other))
    assert not schedule._definition_matches(sync, "macos"), "another folder is another venv"


def test_linux_and_windows_report_their_installed_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_systemd_dir", lambda: tmp_path)
    for action in schedule._ACTIONS:
        schedule._write_systemd_units(action.placed(44))
    xml = schedule._windows_task_xml(schedule._ACTIONS[1].placed(44)).decode("utf-16")
    monkeypatch.setattr(
        schedule, "_query", lambda command: subprocess.CompletedProcess(command, 0, xml, "")
    )

    assert [schedule._installed_minute(action, "linux") for action in schedule._ACTIONS] == [
        14,
        44,
        44,
    ]
    assert schedule._installed_minute(schedule._ACTIONS[1], "windows") == 44
