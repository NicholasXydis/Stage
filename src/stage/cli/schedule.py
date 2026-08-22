import argparse
import os
import platform
import plistlib
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO, cast

from stage.cli.logfile import rotate
from stage.cli.schedule_state import (
    HEARTBEAT_INTERVAL_SECONDS,
    ScheduleStateWriter,
    read_state,
    state_for_status,
    state_path,
)
from stage.paths import data_dir

SYNC_TIME = "00:00"
DISCOVER_TIME = "10:30"
VERIFY_TIME = "11:30"
SYNC_EVERY_HOURS = 6
MAX_JITTER_S = 1800


class ScheduleError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ScheduledAction:
    key: str
    label: str
    cadence: str
    time: str
    windows_schedule: str
    cli_arguments: tuple[str, ...]
    windows_day: str | None = None
    launchd_weekday: int | None = None
    systemd_day: str | None = None
    repeat_hours: int | None = None

    @property
    def start_hours(self) -> tuple[int, ...]:
        first = int(self.time[:2])
        if self.repeat_hours is None:
            return (first,)
        return tuple(range(first, 24, self.repeat_hours))


@dataclass(frozen=True, slots=True)
class ScheduleStatus:
    backend: str
    actions: tuple[tuple[ScheduledAction, bool, str], ...]
    log_dir: Path
    states: tuple[dict[str, object] | None, ...] = ()
    needs_update: tuple[bool, ...] = ()


_ACTIONS = (
    ScheduledAction(
        key="sync",
        label="Stage Sync",
        cadence=f"every {SYNC_EVERY_HOURS} hours",
        time=SYNC_TIME,
        windows_schedule="DAILY",
        cli_arguments=("sync",),
        repeat_hours=SYNC_EVERY_HOURS,
    ),
    ScheduledAction(
        key="discover",
        label="Stage Discover",
        cadence="weekly on Monday",
        time=DISCOVER_TIME,
        windows_schedule="WEEKLY",
        cli_arguments=("discover", "--unregistered", "--limit", "40"),
        windows_day="MON",
        launchd_weekday=1,
        systemd_day="Mon",
    ),
    ScheduledAction(
        key="verify",
        label="Stage Verify",
        cadence="weekly on Wednesday",
        time=VERIFY_TIME,
        windows_schedule="WEEKLY",
        cli_arguments=("discover", "--verify"),
        windows_day="WED",
        launchd_weekday=3,
        systemd_day="Wed",
    ),
)


def enable() -> ScheduleStatus:
    backend = _backend()
    if backend == "windows":
        _enable_windows()
    elif backend == "macos":
        _enable_macos()
    else:
        _enable_linux()
    return status()


def disable() -> ScheduleStatus:
    backend = _backend()
    if backend == "windows":
        _disable_windows()
    elif backend == "macos":
        _disable_macos()
    else:
        _disable_linux()
    return status()


def status() -> ScheduleStatus:
    backend = _backend()
    exists, label = {
        "windows": (_windows_exists, "Windows Task Scheduler"),
        "macos": (_macos_exists, "macOS launchd"),
    }.get(backend, (_linux_exists, "Linux systemd user timers"))
    actions = tuple(
        (action, exists(action), _installed_cadence(action, backend)) for action in _ACTIONS
    )
    return ScheduleStatus(
        label,
        actions,
        _log_dir(),
        tuple(state_for_status(read_state(action.key)) for action in _ACTIONS),
        tuple(
            enabled and not _definition_matches(action, backend) for action, enabled, _ in actions
        ),
    )


def _installed_cadence(action: ScheduledAction, backend: str) -> str:
    if backend == "windows":
        return _windows_installed(action)
    if backend == "macos":
        return _macos_installed(action)
    return _linux_installed(action)


def _windows_installed(action: ScheduledAction) -> str:
    result = _execute(("schtasks", "/Query", "/TN", action.label, "/XML"))
    if result.returncode != 0:
        return ""
    body = result.stdout
    match = re.search(r"<Interval>PT(\d+)H</Interval>", body)
    if match:
        return f"every {int(match.group(1))} hours"
    return "daily" if "<ScheduleByDay>" in body else "weekly"


def _macos_installed(action: ScheduledAction) -> str:
    path = _launch_path(action)
    if not path.is_file():
        return ""
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return ""
    schedule = payload.get("StartCalendarInterval")
    if isinstance(schedule, list) and len(schedule) > 1:
        return f"every {24 // len(schedule)} hours"
    return "weekly" if action.launchd_weekday is not None else "daily"


def _linux_installed(action: ScheduledAction) -> str:
    path = _systemd_dir() / f"{_systemd_name(action)}.timer"
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"OnCalendar=.*?\s(\d{2}(?:,\d{2})*):", body)
    if match:
        slots = len(match.group(1).split(","))
        return f"every {24 // slots} hours" if slots > 1 else "daily"
    return "weekly" if action.systemd_day is not None else "daily"


def matches_definition(action: ScheduledAction, installed: str) -> bool:
    return not installed or action.cadence.startswith(installed)


def _definition_matches(action: ScheduledAction, backend: str) -> bool:
    if backend == "windows":
        result = _execute(("schtasks", "/Query", "/TN", action.label, "/XML"))
        if result.returncode != 0:
            return False
        command = re.search(r"<Command>([^<]*)</Command>", result.stdout)
        arguments = re.search(r"<Arguments>([^<]*)</Arguments>", result.stdout)
        expected = _command(action)
        return bool(
            command
            and arguments
            and unescape(command.group(1)) == expected[0]
            and unescape(arguments.group(1)) == subprocess.list2cmdline(expected[1:])
        )
    if backend == "macos":
        try:
            payload = plistlib.loads(_launch_path(action).read_bytes())
        except (OSError, plistlib.InvalidFileException):
            return False
        if not isinstance(payload, dict):
            return False
        log = str(_log_dir() / f"scheduled-{action.key}.log")
        return (
            payload.get("ProgramArguments") == list(_command(action))
            and payload.get("StandardOutPath") == log
            and payload.get("StandardErrorPath") == log
            and payload.get("ProcessType") == "Background"
        )
    try:
        service = (_systemd_dir() / f"{_systemd_name(action)}.service").read_text(encoding="utf-8")
        timer = (_systemd_dir() / f"{_systemd_name(action)}.timer").read_text(encoding="utf-8")
    except OSError:
        return False
    return (service, timer) == _systemd_units(action)


def run_scheduled(
    key: str,
    *,
    jitter_seconds: float | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    action = next((candidate for candidate in _ACTIONS if candidate.key == key), None)
    if action is None:
        raise ScheduleError(f"unknown scheduled action: {key}")
    path = _log_dir() / f"scheduled-{action.key}.log"
    rotate(path)
    state = ScheduleStateWriter.start(action.key, path, destination=state_path(action.key))
    delay = _jitter_seconds(action) if jitter_seconds is None else jitter_seconds
    with path.open("a", encoding="utf-8", buffering=1) as stream:
        stream.write(f"\n{action.label}\nTriggered: {_timestamp()}\n")
        state.waiting(delay)
        if delay:
            stream.write(f"Waiting {round(delay)}s before work starts.\n")
            _wait_for_start(delay, sleeper, state.heartbeat)
        phase = "syncing" if action.key == "sync" else "discovering"
        state.started(phase)
        stream.write(f"Work started: {_timestamp()}\n")
        sync_exit = _invoke_cli(
            action.cli_arguments,
            stream,
            progress_path=state.path,
            heartbeat=state.heartbeat,
        )
        doctor_exit: int | None = None
        exit_code = sync_exit
        if action.key == "sync" and not state.path_is_blocked:
            state.checking()
            stream.write("Running health check.\n")
            doctor_exit = _invoke_cli(("doctor",), stream, heartbeat=state.heartbeat)
            if doctor_exit:
                exit_code = doctor_exit
        state.complete(
            sync_exit_code=sync_exit,
            doctor_exit_code=doctor_exit,
            exit_code=exit_code,
        )
        stream.write(f"Finished: {_timestamp()}\n")
        stream.write(f"exit code: {exit_code}\n")
    return exit_code


def _spread_load(action: ScheduledAction, sleeper: Callable[[float], None] = time.sleep) -> None:
    if action.repeat_hours is not None:
        sleeper(_jitter_seconds(action))


def _jitter_seconds(action: ScheduledAction) -> float:
    if action.repeat_hours is None:
        return 0.0
    return random.SystemRandom().uniform(0.0, float(MAX_JITTER_S))


def _wait_for_start(
    delay_seconds: float,
    sleeper: Callable[[float], None],
    heartbeat: Callable[[], None],
) -> None:
    remaining = delay_seconds
    while remaining > 0:
        interval = min(HEARTBEAT_INTERVAL_SECONDS, remaining)
        sleeper(interval)
        remaining -= interval
        heartbeat()


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Stage scheduled action.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one configured action")
    run.add_argument("action", choices=[action.key for action in _ACTIONS])
    arguments = parser.parse_args()
    raise SystemExit(run_scheduled(arguments.action))


def _backend() -> str:
    name = platform.system()
    if name == "Windows":
        return "windows"
    if name == "Darwin":
        return "macos"
    if name == "Linux":
        return "linux"
    raise ScheduleError(f"automatic scheduling is not supported on {name}")


def _command(action: ScheduledAction) -> tuple[str, ...]:
    executable = sys.executable
    if _backend() == "windows":
        candidate = Path(executable).with_name("pythonw.exe")
        if candidate.is_file():
            executable = str(candidate)
    return (executable, "-m", "stage.cli.schedule", "run", action.key)


def _log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _invoke_cli(
    arguments: Sequence[str],
    stream: TextIO,
    *,
    progress_path: Path | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> int:
    command = [sys.executable, "-u", "-m", "stage", *arguments]
    if progress_path is not None:
        command.extend(("--scheduled-progress", str(progress_path)))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    except OSError as error:
        raise ScheduleError(f"unable to start scheduled command: {command[0]}") from error
    while True:
        try:
            return int(process.wait(timeout=HEARTBEAT_INTERVAL_SECONDS))
        except subprocess.TimeoutExpired:
            if heartbeat is not None:
                heartbeat()


def _execute(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise ScheduleError(f"required scheduler command is unavailable: {command[0]}") from error


def _run(command: Sequence[str]) -> None:
    completed = _execute(command)
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout).strip()
    raise ScheduleError(
        f"scheduler command failed: {' '.join(command)}{': ' + detail if detail else ''}"
    )


def _windows_exists(action: ScheduledAction) -> bool:
    return _execute(("schtasks", "/Query", "/TN", action.label)).returncode == 0


def _enable_windows() -> None:
    for action in _ACTIONS:
        _write_windows_task(action)


def _write_windows_task(action: ScheduledAction) -> None:
    with TemporaryDirectory(prefix="stage-schedule-") as directory:
        path = Path(directory) / f"{action.key}.xml"
        path.write_bytes(_windows_task_xml(action))
        _run(("schtasks", "/Create", "/F", "/TN", action.label, "/XML", str(path)))


def _windows_task_xml(action: ScheduledAction) -> bytes:
    root = ElementTree.Element(
        "Task", {"version": "1.4", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    )
    registration = ElementTree.SubElement(root, "RegistrationInfo")
    ElementTree.SubElement(registration, "Description").text = f"{action.label}"
    triggers = ElementTree.SubElement(root, "Triggers")
    trigger = ElementTree.SubElement(triggers, "CalendarTrigger")
    ElementTree.SubElement(trigger, "StartBoundary").text = _windows_start_boundary(action)
    ElementTree.SubElement(trigger, "Enabled").text = "true"
    if action.windows_day is None:
        schedule = ElementTree.SubElement(trigger, "ScheduleByDay")
        ElementTree.SubElement(schedule, "DaysInterval").text = "1"
        if action.repeat_hours is not None:
            repetition = ElementTree.SubElement(trigger, "Repetition")
            ElementTree.SubElement(repetition, "Interval").text = f"PT{action.repeat_hours}H"
            ElementTree.SubElement(repetition, "Duration").text = "P1D"
            ElementTree.SubElement(repetition, "StopAtDurationEnd").text = "false"
    else:
        schedule = ElementTree.SubElement(trigger, "ScheduleByWeek")
        ElementTree.SubElement(schedule, "WeeksInterval").text = "1"
        days = ElementTree.SubElement(schedule, "DaysOfWeek")
        ElementTree.SubElement(days, "Monday")
    principals = ElementTree.SubElement(root, "Principals")
    principal = ElementTree.SubElement(principals, "Principal", {"id": "Stage"})
    ElementTree.SubElement(principal, "LogonType").text = "InteractiveToken"
    ElementTree.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ElementTree.SubElement(root, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT2H"),
        ("Priority", "7"),
    ):
        ElementTree.SubElement(settings, name).text = value
    actions = ElementTree.SubElement(root, "Actions", {"Context": "Stage"})
    execute = ElementTree.SubElement(actions, "Exec")
    command = _command(action)
    ElementTree.SubElement(execute, "Command").text = command[0]
    ElementTree.SubElement(execute, "Arguments").text = subprocess.list2cmdline(command[1:])
    return cast(bytes, ElementTree.tostring(root, encoding="utf-16", xml_declaration=True))


def _windows_start_boundary(action: ScheduledAction) -> str:
    local = datetime.now().astimezone()
    return f"{local.date().isoformat()}T{action.time}:00"


def _disable_windows() -> None:
    for action in _ACTIONS:
        _execute(("schtasks", "/Delete", "/TN", action.label, "/F"))


def _launch_agent_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launch_label(action: ScheduledAction) -> str:
    return f"com.stage.{action.key}"


def _launch_path(action: ScheduledAction) -> Path:
    return _launch_agent_dir() / f"{_launch_label(action)}.plist"


def _write_launch_agent(action: ScheduledAction) -> Path:
    minute = int(action.time[3:])
    schedule: list[dict[str, int]] = [
        {"Hour": hour, "Minute": minute} for hour in action.start_hours
    ]
    if action.launchd_weekday is not None:
        for entry in schedule:
            entry["Weekday"] = action.launchd_weekday
    payload: dict[str, object] = {
        "Label": _launch_label(action),
        "ProgramArguments": list(_command(action)),
        "StartCalendarInterval": schedule if len(schedule) > 1 else schedule[0],
        "StandardOutPath": str(_log_dir() / f"scheduled-{action.key}.log"),
        "StandardErrorPath": str(_log_dir() / f"scheduled-{action.key}.log"),
        "ProcessType": "Background",
    }
    path = _launch_path(action)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(payload, sort_keys=False))
    path.chmod(0o600)
    return path


def _launch_domain() -> str:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise ScheduleError("launchd scheduling is unavailable without a macOS user ID")
    return f"gui/{getuid()}"


def _macos_exists(action: ScheduledAction) -> bool:
    if not _launch_path(action).is_file():
        return False
    return (
        _execute(("launchctl", "print", f"{_launch_domain()}/{_launch_label(action)}")).returncode
        == 0
    )


def _enable_macos() -> None:
    paths = {action: _write_launch_agent(action) for action in _ACTIONS}
    active: list[ScheduledAction] = []
    try:
        for action in _ACTIONS:
            _execute(("launchctl", "bootout", f"{_launch_domain()}/{_launch_label(action)}"))
            _run(("launchctl", "bootstrap", _launch_domain(), str(paths[action])))
            active.append(action)
    except ScheduleError:
        for action in active:
            _execute(("launchctl", "bootout", f"{_launch_domain()}/{_launch_label(action)}"))
        raise


def _disable_macos() -> None:
    for action in _ACTIONS:
        _execute(("launchctl", "bootout", f"{_launch_domain()}/{_launch_label(action)}"))
        _launch_path(action).unlink(missing_ok=True)


def _systemd_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "systemd" / "user"


def _systemd_name(action: ScheduledAction) -> str:
    return f"stage-{action.key}"


def _systemd_argument(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_units(action: ScheduledAction) -> tuple[str, str]:
    name = _systemd_name(action)
    command = " ".join(_systemd_argument(value) for value in _command(action))
    service = (
        f"[Unit]\nDescription={action.label}\n\n[Service]\nType=oneshot\nExecStart={command}\n"
    )
    hours = ",".join(f"{hour:02d}" for hour in action.start_hours)
    calendar = f"*-*-* {hours}:{action.time[3:]}:00"
    if action.systemd_day is not None:
        calendar = f"{action.systemd_day} {calendar}"
    timer = (
        "[Unit]\n"
        f"Description={action.label} timer\n\n"
        "[Timer]\n"
        f"OnCalendar={calendar}\n"
        "Persistent=true\n"
        f"Unit={name}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _write_systemd_units(action: ScheduledAction) -> None:
    root = _systemd_dir()
    root.mkdir(parents=True, exist_ok=True)
    name = _systemd_name(action)
    service, timer = _systemd_units(action)
    for path, body in ((root / f"{name}.service", service), (root / f"{name}.timer", timer)):
        path.write_text(body, encoding="utf-8")
        path.chmod(0o600)


def _linux_exists(action: ScheduledAction) -> bool:
    name = f"{_systemd_name(action)}.timer"
    return _execute(("systemctl", "--user", "is-enabled", name)).returncode == 0


def _enable_linux() -> None:
    for action in _ACTIONS:
        _write_systemd_units(action)
    _run(("systemctl", "--user", "daemon-reload"))
    active: list[ScheduledAction] = []
    try:
        for action in _ACTIONS:
            _run(("systemctl", "--user", "enable", "--now", f"{_systemd_name(action)}.timer"))
            active.append(action)
    except ScheduleError:
        for action in active:
            _execute(("systemctl", "--user", "disable", "--now", f"{_systemd_name(action)}.timer"))
        raise


def _disable_linux() -> None:
    for action in _ACTIONS:
        name = _systemd_name(action)
        _execute(("systemctl", "--user", "disable", "--now", f"{name}.timer"))
        for suffix in ("service", "timer"):
            (_systemd_dir() / f"{name}.{suffix}").unlink(missing_ok=True)
    _run(("systemctl", "--user", "daemon-reload"))


if __name__ == "__main__":
    main()
