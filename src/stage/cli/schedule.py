import argparse
import ctypes
import getpass
import os
import platform
import plistlib
import random
import re
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from types import FrameType
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
SYNC_EVERY_MINUTES = 30
EVERY_CHOICES = (30, 60, 120, 180, 240, 360, 480, 720)
MAX_JITTER_S = 1800
JITTER_SHARE = 0.2
START_MINUTE_NAME = "start-minute"
_SUBSYSTEM_GUI = 2
_CREATE_NO_WINDOW = 0x08000000
STOP_WAIT_SECONDS = 10.0
_STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGBREAK")
_JOBS: list[int] = []
_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMITS = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


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
    repeat_minutes: int | None = None

    @property
    def start_hours(self) -> tuple[int, ...]:
        first = int(self.time[:2])
        if self.repeat_minutes is None:
            return (first,)
        return tuple(range(first, 24, max(1, self.repeat_minutes // 60)))

    @property
    def start_minutes(self) -> tuple[int, ...]:
        first = int(self.time[3:])
        if self.repeat_minutes is None or self.repeat_minutes >= 60:
            return (first,)
        return tuple(range(first, 60, self.repeat_minutes))

    @property
    def when(self) -> str:
        if self.repeat_minutes is not None and self.repeat_minutes < 60:
            return "at " + " and ".join(f":{minute:02d}" for minute in self.start_minutes)
        if self.repeat_minutes is not None:
            return f"from {self.time}"
        return f"at {self.time}"

    def every(self, minutes: int | None) -> "ScheduledAction":
        if self.repeat_minutes is None or minutes is None:
            return self
        return replace(self, repeat_minutes=minutes, cadence=every_text(minutes))

    def placed(self, minute: int) -> "ScheduledAction":
        offset = minute % SYNC_EVERY_MINUTES if self.repeat_minutes is not None else minute % 60
        return replace(self, time=f"{self.time[:3]}{offset:02d}")


def _short(minutes: int) -> str:
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"


def every_text(minutes: int) -> str:
    if minutes < 60:
        return f"every {minutes} minutes"
    hours = minutes // 60
    return "every hour" if hours == 1 else f"every {hours} hours"


def parse_every(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)\s*", text.lower())
    minutes = 0
    if match:
        minutes = int(match.group(1)) * (1 if match.group(2).startswith("m") else 60)
    if minutes not in EVERY_CHOICES:
        choices = ", ".join(_short(choice) for choice in EVERY_CHOICES)
        asked = f'Unknown interval "{text.strip()[:20]}"' if text.strip() else "No interval given"
        raise ScheduleError(f"{asked} for --every. Choose from: {choices}.")
    return minutes


@dataclass(frozen=True, slots=True)
class ScheduleStatus:
    backend: str
    actions: tuple[tuple[ScheduledAction, bool, str], ...]
    log_dir: Path
    states: tuple[dict[str, object] | None, ...] = ()
    needs_update: tuple[bool, ...] = ()
    stale_interpreter: tuple[str, ...] = ()
    lingering: bool | None = None


_ACTIONS = (
    ScheduledAction(
        key="sync",
        label="Stage Sync",
        cadence=every_text(SYNC_EVERY_MINUTES),
        time=SYNC_TIME,
        windows_schedule="DAILY",
        cli_arguments=("sync",),
        repeat_minutes=SYNC_EVERY_MINUTES,
    ),
    ScheduledAction(
        key="discover",
        label="Stage Discover",
        cadence="weekly on Monday",
        time=DISCOVER_TIME,
        windows_schedule="WEEKLY",
        cli_arguments=(
            "discover",
            "--unregistered",
            "--direct-only",
            "--adopt-unnamed",
            "--limit",
            "40",
            "--apply",
        ),
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
        cli_arguments=("discover", "--verify", "--apply"),
        windows_day="WED",
        launchd_weekday=3,
        systemd_day="Wed",
    ),
)


def action_keys() -> tuple[str, ...]:
    return tuple(action.key for action in _ACTIONS)


def _chosen(keys: Sequence[str] | None, every: int | None = None) -> tuple[ScheduledAction, ...]:
    if every is not None:
        return tuple(action.every(every) for action in _chosen(keys))
    if not keys:
        return _ACTIONS
    unknown = sorted(set(keys) - set(action_keys()))
    if unknown:
        from stage.cli.options import _unknown

        raise ScheduleError(_unknown("action", unknown[0], action_keys()))
    wanted = set(keys)
    return tuple(action for action in _ACTIONS if action.key in wanted)


def _saved_minute() -> int | None:
    path = data_dir() / "schedule" / START_MINUTE_NAME
    try:
        minute = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return minute if 0 <= minute < 60 else None


def start_minute(create: bool = False) -> int:
    path = data_dir() / "schedule" / START_MINUTE_NAME
    minute = _saved_minute()
    if minute is not None:
        return minute
    if not create:
        return 0
    minute = random.SystemRandom().randrange(60)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_text(f"{minute}\n", encoding="utf-8")
    staging.replace(path)
    return minute


def lingers() -> bool | None:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        return None
    result = _query(("loginctl", "show-user", user, "--property=Linger", "--value"))
    answer = result.stdout.strip().lower() if result.returncode == 0 else ""
    return {"yes": True, "no": False}.get(answer)


def enable(actions: Sequence[str] | None = None, every: int | None = None) -> ScheduleStatus:
    backend = _backend()
    minute = start_minute(create=True)
    chosen = tuple(action.placed(minute) for action in _chosen(actions, every))
    if backend == "windows":
        _enable_windows(chosen)
    elif backend == "macos":
        _enable_macos(chosen)
    else:
        _enable_linux(chosen)
    return status()


def disable(actions: Sequence[str] | None = None) -> ScheduleStatus:
    backend = _backend()
    chosen = _chosen(actions)
    if backend == "windows":
        _disable_windows(chosen)
    elif backend == "macos":
        _disable_macos(chosen)
    else:
        _disable_linux(chosen)
    return status()


def status() -> ScheduleStatus:
    backend = _backend()
    exists, label = {
        "windows": (_windows_exists, "Windows Task Scheduler"),
        "macos": (_macos_exists, "macOS launchd"),
    }.get(backend, (_linux_exists, "Linux systemd user timers"))
    minute = start_minute()
    saved = _saved_minute() is not None
    expected = tuple(
        action.placed(minute).every(_installed_minutes(action, backend)) for action in _ACTIONS
    )
    found = tuple(_installed_minute(action, backend) for action in _ACTIONS)
    shown = tuple(
        action if at is None else action.placed(at)
        for action, at in zip(expected, found, strict=True)
    )
    actions = tuple(
        (action, exists(action), _installed_cadence(action, backend)) for action in shown
    )
    return ScheduleStatus(
        label,
        actions,
        _log_dir(),
        tuple(state_for_status(read_state(action.key)) for action in _ACTIONS),
        tuple(
            enabled
            and (not _definition_matches(wanted, backend) or (saved and action.time != wanted.time))
            for (action, enabled, _), wanted in zip(actions, expected, strict=True)
        ),
        tuple(
            _stale_interpreter(action, backend) if enabled else "" for action, enabled, _ in actions
        ),
        lingers() if backend == "linux" else None,
    )


def advice() -> tuple[str, ...]:
    try:
        backend = _backend()
    except ScheduleError:
        return ()
    installed = [action for action in _ACTIONS if _installed_file(action, backend)]
    if not installed:
        return ()
    minute = start_minute()
    hints: list[str] = []
    if any(
        not _definition_matches(
            action.placed(minute).every(_installed_minutes(action, backend)), backend
        )
        for action in installed
    ):
        hints.append(
            "The schedule was set up by an older Stage. Run stage schedule enable to update it."
        )
    if backend == "linux" and lingers() is False:
        hints.append(
            "Linux stops the schedule when you log out. To keep it running, run: "
            f"sudo loginctl enable-linger {getpass.getuser()}"
        )
    return tuple(hints)


def _task_xml(action: ScheduledAction) -> str | None:
    result = _query(("schtasks", "/Query", "/TN", action.label, "/XML"))
    return result.stdout if result.returncode == 0 else None


def _plist(action: ScheduledAction) -> dict[str, object] | None:
    try:
        payload = plistlib.loads(_launch_path(action).read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    return payload if isinstance(payload, dict) else {}


def _unit(action: ScheduledAction, suffix: str) -> str | None:
    try:
        return (_systemd_dir() / f"{_systemd_name(action)}.{suffix}").read_text(encoding="utf-8")
    except OSError:
        return None


def _calendar(payload: dict[str, object]) -> list[dict[str, object]]:
    schedule = payload.get("StartCalendarInterval")
    listed = schedule if isinstance(schedule, list) else [schedule]
    return [entry for entry in listed if isinstance(entry, dict)]


def _installed_file(action: ScheduledAction, backend: str) -> bool:
    if backend == "windows":
        return _windows_exists(action)
    if backend == "macos":
        return _launch_path(action).is_file()
    return (_systemd_dir() / f"{_systemd_name(action)}.timer").is_file()


def _installed_interpreter(action: ScheduledAction, backend: str) -> str:
    if backend == "windows":
        command = re.search(r"<Command>([^<]*)</Command>", _task_xml(action) or "")
        return unescape(command.group(1)) if command else ""
    if backend == "macos":
        arguments = (_plist(action) or {}).get("ProgramArguments")
        return str(arguments[0]) if isinstance(arguments, list) and arguments else ""
    match = re.search(
        r"^ExecStart=\"([^\"]*)\"", _unit(action, "service") or "", flags=re.MULTILINE
    )
    return match.group(1).replace('\\"', '"').replace("\\\\", "\\") if match else ""


def _stale_interpreter(action: ScheduledAction, backend: str) -> str:
    executable = _installed_interpreter(action, backend)
    if not executable or Path(executable).exists():
        return ""
    return executable


def _installed_repeat(action: ScheduledAction, backend: str) -> int | None:
    return {"windows": _windows_installed, "macos": _macos_installed}.get(
        backend, _linux_installed
    )(action)


def _installed_cadence(action: ScheduledAction, backend: str) -> str:
    found = _installed_repeat(action, backend)
    if found is None:
        return ""
    if found:
        return every_text(found)
    weekly = action.windows_day or action.launchd_weekday is not None or action.systemd_day
    return "weekly" if weekly else "daily"


def _installed_minutes(action: ScheduledAction, backend: str) -> int | None:
    return _installed_repeat(action, backend) or None


def _installed_minute(action: ScheduledAction, backend: str) -> int | None:
    minutes: list[int] = []
    if backend == "windows":
        found = re.search(r"<StartBoundary>[^<T]*T\d{2}:(\d{2})", _task_xml(action) or "")
        minutes = [int(found.group(1))] if found else []
    elif backend == "macos":
        payload = _plist(action)
        if payload is None:
            return None
        minutes = [
            minute for entry in _calendar(payload) if isinstance(minute := entry.get("Minute"), int)
        ]
    else:
        body = _unit(action, "timer")
        if body is None:
            return None
        calendar = re.search(r"OnCalendar=(?:\w+ )?\*-\*-\* [\d,*]+:(\d{2}(?:,\d{2})*):00", body)
        minutes = [int(value) for value in calendar.group(1).split(",")] if calendar else []
    return min(minutes) if minutes and all(0 <= minute < 60 for minute in minutes) else None


def _windows_installed(action: ScheduledAction) -> int | None:
    xml = _task_xml(action)
    if xml is None:
        return None
    match = re.search(r"<Interval>PT(\d+)([HM])</Interval>", xml)
    if not match:
        return 0
    return int(match.group(1)) * (60 if match.group(2) == "H" else 1)


def _macos_installed(action: ScheduledAction) -> int | None:
    payload = _plist(action)
    if payload is None:
        return None
    entries = _calendar(payload)
    if not entries:
        return 0
    if all("Hour" not in entry and "Weekday" not in entry for entry in entries):
        return 60 // len(entries)
    return 1440 // len(entries) if len(entries) > 1 else 0


def _linux_installed(action: ScheduledAction) -> int | None:
    body = _unit(action, "timer")
    if body is None:
        return None
    match = re.search(r"OnCalendar=\*-\*-\* (\*|\d{2}(?:,\d{2})*):(\d{2}(?:,\d{2})*):00", body)
    if not match:
        return 0
    hours, minutes = match.group(1), match.group(2).split(",")
    if hours == "*":
        return 60 // len(minutes)
    slots = len(hours.split(","))
    return 1440 // slots if slots > 1 else 0


def _same_interpreter(installed: str, current: str) -> bool:
    if installed == current:
        return True
    there, here = Path(installed), Path(current)
    try:
        return there.samefile(here) and there.parent.samefile(here.parent)
    except OSError:
        return False


def _definition_matches(action: ScheduledAction, backend: str) -> bool:
    installed = _installed_interpreter(action, backend)
    current = _command(action)[0]
    executable = installed if installed and _same_interpreter(installed, current) else current
    expected = _command(action, executable)
    if backend == "windows":
        xml = _task_xml(action) or ""
        command = re.search(r"<Command>([^<]*)</Command>", xml)
        arguments = re.search(r"<Arguments>([^<]*)</Arguments>", xml)
        return bool(
            command
            and arguments
            and unescape(command.group(1)) == expected[0]
            and unescape(arguments.group(1)) == subprocess.list2cmdline(expected[1:])
        )
    if backend == "macos":
        payload = _plist(action) or {}
        log = str(_log_dir() / f"scheduled-{action.key}.log")
        return (
            payload.get("ProgramArguments") == list(expected)
            and payload.get("StandardOutPath") == log
            and payload.get("StandardErrorPath") == log
            and payload.get("ProcessType") == "Background"
        )
    return (_unit(action, "service"), _unit(action, "timer")) == _systemd_units(action, executable)


def run_scheduled(
    key: str,
    *,
    every: int | None = None,
    jitter_seconds: float | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    action = next((candidate for candidate in _ACTIONS if candidate.key == key), None)
    if action is None:
        raise ScheduleError(f'Unknown scheduled action "{key}".')
    action = action.every(every)
    path = _log_dir() / f"scheduled-{action.key}.log"
    rotate(path)
    state = ScheduleStateWriter.start(action.key, path, destination=state_path(action.key))
    delay = _jitter_seconds(action) if jitter_seconds is None else jitter_seconds
    with path.open("a", encoding="utf-8", buffering=1) as stream, _stoppable():
        try:
            exit_code = _run_action(action, state, stream, delay, sleeper)
        except (_StoppedError, KeyboardInterrupt) as stop:
            number = stop.signum if isinstance(stop, _StoppedError) else signal.SIGINT
            name = signal.Signals(number).name
            exit_code = 128 + number
            if state.is_waiting:
                state.stopped(
                    f"The computer stopped the scheduled {action.key} ({name}) before it "
                    "started. The next run catches up.",
                    exit_code=exit_code,
                )
            else:
                state.failed(
                    f"The scheduled {action.key} was stopped by {name} before it finished.",
                    exit_code=exit_code,
                )
            stream.write(f"Stopped by {name}: {_timestamp()}\n")
            stream.write(f"exit code: {exit_code}\n")
    return exit_code


def _run_action(
    action: ScheduledAction,
    state: ScheduleStateWriter,
    stream: TextIO,
    delay: float,
    sleeper: Callable[[float], None],
) -> int:
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
        doctor_exit = _invoke_cli(
            ("doctor", "--skip-file-check"), stream, heartbeat=state.heartbeat
        )
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


class _StoppedError(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _stop_signals() -> list[int]:
    return [number for name in _STOP_SIGNALS if (number := getattr(signal, name, None))]


def _raise_stop(signum: int, _frame: FrameType | None) -> None:
    for number in _stop_signals():
        signal.signal(number, signal.SIG_IGN)
    raise _StoppedError(signum)


@contextmanager
def _stoppable() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {number: signal.signal(number, _raise_stop) for number in _stop_signals()}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _jitter_seconds(action: ScheduledAction) -> float:
    if action.repeat_minutes is None:
        return 0.0
    longest = min(float(MAX_JITTER_S), action.repeat_minutes * 60 * JITTER_SHARE)
    return random.SystemRandom().uniform(0.0, longest)


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
    run.add_argument("--every", type=int, choices=EVERY_CHOICES, default=None)
    arguments = parser.parse_args()
    raise SystemExit(run_scheduled(arguments.action, every=arguments.every))


def _backend() -> str:
    name = platform.system()
    if name == "Windows":
        return "windows"
    if name == "Darwin":
        return "macos"
    if name == "Linux":
        return "linux"
    raise ScheduleError(
        f"Stage cannot schedule syncs on {name}. Run stage sync yourself, or from a scheduler "
        "of your own."
    )


def _is_windowless(candidate: Path) -> bool:
    try:
        header = candidate.read_bytes()[:1024]
    except OSError:
        return False
    if len(header) < 0x40 or header[:2] != b"MZ":
        return False
    offset = int.from_bytes(header[0x3C:0x40], "little")
    if offset + 0x5E > len(header) or header[offset : offset + 4] != b"PE\0\0":
        return False
    return int.from_bytes(header[offset + 0x5C : offset + 0x5E], "little") == _SUBSYSTEM_GUI


def _windowless_python() -> str | None:
    seen: set[Path] = set()
    for parent in (Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.prefix)):
        for candidate in (parent / "pythonw.exe", parent / "Scripts" / "pythonw.exe"):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file() and _is_windowless(candidate):
                return str(candidate)
    return None


def _command(action: ScheduledAction, interpreter: str | None = None) -> tuple[str, ...]:
    executable = interpreter or sys.executable
    if interpreter is None and _backend() == "windows":
        executable = _windowless_python() or executable
    custom = action.repeat_minutes not in (None, SYNC_EVERY_MINUTES)
    every = ("--every", str(action.repeat_minutes)) if custom else ()
    return (executable, "-m", "stage.cli.schedule", "run", action.key, *every)


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
            creationflags=_CREATE_NO_WINDOW if _backend() == "windows" else 0,
        )
    except OSError as error:
        raise ScheduleError(f"Stage could not start {command[0]} for the scheduled run.") from error
    if sys.platform == "win32":
        _contain(process.pid)
    try:
        while True:
            try:
                return int(process.wait(timeout=HEARTBEAT_INTERVAL_SECONDS))
            except subprocess.TimeoutExpired:
                if heartbeat is not None:
                    heartbeat()
    except BaseException:
        _stop(process)
        raise


def _stop(process: "subprocess.Popen[bytes]") -> None:
    with suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=STOP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            process.kill()
        process.wait()


@cache
def _job_limits() -> type[ctypes.Structure]:
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return ExtendedLimits


def _contain(pid: int) -> None:
    if sys.platform == "win32":
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = _job_limits()()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        child = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        held = bool(
            child
            and kernel32.SetInformationJobObject(
                job, _EXTENDED_LIMITS, ctypes.addressof(limits), ctypes.sizeof(limits)
            )
            and kernel32.AssignProcessToJobObject(job, child)
        )
        if child:
            kernel32.CloseHandle(child)
        if held:
            _JOBS.append(job)
        else:
            kernel32.CloseHandle(job)


def _execute(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, errors="replace"
        )
    except FileNotFoundError as error:
        raise ScheduleError(
            f"Stage needs {command[0]} to set up the schedule, and it is not installed here."
        ) from error


def _query(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return _execute(command)
    except ScheduleError:
        return subprocess.CompletedProcess(command, 1, "", "")


def _run(command: Sequence[str]) -> None:
    completed = _execute(command)
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout).strip()
    said = f": {detail}" if detail else ""
    raise ScheduleError(
        f"{command[0]} refused the change{said}. Run stage schedule status to see what is set up."
    )


def _windows_exists(action: ScheduledAction) -> bool:
    return _query(("schtasks", "/Query", "/TN", action.label)).returncode == 0


def _enable_windows(actions: Sequence[ScheduledAction] = _ACTIONS) -> None:
    for action in actions:
        _write_windows_task(action)


def _write_windows_task(action: ScheduledAction) -> None:
    with TemporaryDirectory(prefix="stage-schedule-") as directory:
        path = Path(directory) / f"{action.key}.xml"
        path.write_bytes(_windows_task_xml(action))
        _run(("schtasks", "/Create", "/F", "/TN", action.label, "/XML", str(path)))


_WINDOWS_DAYS = {
    "MON": "Monday",
    "TUE": "Tuesday",
    "WED": "Wednesday",
    "THU": "Thursday",
    "FRI": "Friday",
    "SAT": "Saturday",
    "SUN": "Sunday",
}


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
        if action.repeat_minutes is not None:
            repetition = ElementTree.SubElement(trigger, "Repetition")
            ElementTree.SubElement(repetition, "Interval").text = _windows_interval(action)
            ElementTree.SubElement(repetition, "Duration").text = "P1D"
            ElementTree.SubElement(repetition, "StopAtDurationEnd").text = "false"
    else:
        schedule = ElementTree.SubElement(trigger, "ScheduleByWeek")
        ElementTree.SubElement(schedule, "WeeksInterval").text = "1"
        days = ElementTree.SubElement(schedule, "DaysOfWeek")
        ElementTree.SubElement(days, _WINDOWS_DAYS.get(action.windows_day or "MON", "Monday"))
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
        ("Hidden", "true"),
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


def _windows_interval(action: ScheduledAction) -> str:
    minutes = action.repeat_minutes or 1440
    return f"PT{minutes // 60}H" if minutes % 60 == 0 else f"PT{minutes}M"


def _windows_start_boundary(action: ScheduledAction) -> str:
    local = datetime.now().astimezone()
    return f"{local.date().isoformat()}T{action.time}:00"


def _disable_windows(actions: Sequence[ScheduledAction] = _ACTIONS) -> None:
    for action in actions:
        _execute(("schtasks", "/Delete", "/TN", action.label, "/F"))


def _launch_agent_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launch_label(action: ScheduledAction) -> str:
    return f"com.stage.{action.key}"


def _launch_path(action: ScheduledAction) -> Path:
    return _launch_agent_dir() / f"{_launch_label(action)}.plist"


def _write_launch_agent(action: ScheduledAction) -> Path:
    if action.repeat_minutes is not None and action.repeat_minutes < 60:
        schedule: list[dict[str, int]] = [{"Minute": minute} for minute in action.start_minutes]
    else:
        schedule = [
            {"Hour": hour, "Minute": minute}
            for hour in action.start_hours
            for minute in action.start_minutes
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
        raise ScheduleError("Stage could not find this user's macOS ID, so launchd cannot run it.")
    return f"gui/{getuid()}"


def _macos_exists(action: ScheduledAction) -> bool:
    if not _launch_path(action).is_file():
        return False
    return (
        _query(("launchctl", "print", f"{_launch_domain()}/{_launch_label(action)}")).returncode
        == 0
    )


def _enable_macos(actions: Sequence[ScheduledAction] = _ACTIONS) -> None:
    paths = {action: _write_launch_agent(action) for action in actions}
    active: list[ScheduledAction] = []
    try:
        for action in actions:
            _execute(("launchctl", "bootout", f"{_launch_domain()}/{_launch_label(action)}"))
            _run(("launchctl", "bootstrap", _launch_domain(), str(paths[action])))
            active.append(action)
    except ScheduleError:
        for action in active:
            _execute(("launchctl", "bootout", f"{_launch_domain()}/{_launch_label(action)}"))
        raise


def _disable_macos(actions: Sequence[ScheduledAction] = _ACTIONS) -> None:
    for action in actions:
        _execute(("launchctl", "bootout", f"{_launch_domain()}/{_launch_label(action)}"))
        _launch_path(action).unlink(missing_ok=True)


def _systemd_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "systemd" / "user"


def _systemd_name(action: ScheduledAction) -> str:
    return f"stage-{action.key}"


def _systemd_argument(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_units(action: ScheduledAction, interpreter: str | None = None) -> tuple[str, str]:
    name = _systemd_name(action)
    command = " ".join(_systemd_argument(value) for value in _command(action, interpreter))
    service = (
        f"[Unit]\nDescription={action.label}\n\n[Service]\nType=oneshot\nExecStart={command}\n"
    )
    hours = ",".join(f"{hour:02d}" for hour in action.start_hours)
    if action.repeat_minutes is not None and action.repeat_minutes < 60:
        hours = "*"
    minutes = ",".join(f"{minute:02d}" for minute in action.start_minutes)
    calendar = f"*-*-* {hours}:{minutes}:00"
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
    return _query(("systemctl", "--user", "is-enabled", name)).returncode == 0


def _enable_linux(actions: Sequence[ScheduledAction] = _ACTIONS) -> None:
    for action in actions:
        _write_systemd_units(action)
    _run(("systemctl", "--user", "daemon-reload"))
    active: list[ScheduledAction] = []
    try:
        for action in actions:
            _run(("systemctl", "--user", "enable", "--now", f"{_systemd_name(action)}.timer"))
            active.append(action)
    except ScheduleError:
        for action in active:
            _execute(("systemctl", "--user", "disable", "--now", f"{_systemd_name(action)}.timer"))
        raise


def _disable_linux(actions: Sequence[ScheduledAction] = _ACTIONS) -> None:
    for action in actions:
        name = _systemd_name(action)
        _execute(("systemctl", "--user", "disable", "--now", f"{name}.timer"))
        for suffix in ("service", "timer"):
            (_systemd_dir() / f"{name}.{suffix}").unlink(missing_ok=True)
    _run(("systemctl", "--user", "daemon-reload"))


if __name__ == "__main__":
    main()
