import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

STALE_AFTER = timedelta(minutes=10)
START_WAIT_SECONDS = 8.0
POLL_SECONDS = 0.2
LOG_TAIL = 400
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
STATES = {"waiting": "starting", "syncing": "running"}
RUNNING = ("starting", "running")
_CHILDREN: list["subprocess.Popen[bytes]"] = []


@dataclass(frozen=True, slots=True)
class SyncStart:
    started: bool
    detail: str
    progress: Mapping[str, object] = field(default_factory=dict)


def progress_path(database: Path) -> Path:
    return database.with_name(f".{database.name}.sync.json")


def log_path(database: Path) -> Path:
    return database.with_name(f".{database.name}.sync.log")


def sync_command(database: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "stage",
        "sync",
        "--db",
        str(database),
        "--progress-file",
        str(progress_path(database)),
    ]


def sync_progress(database: Path, now: datetime | None = None) -> dict[str, object] | None:
    _reap()
    try:
        payload = json.loads(progress_path(database).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    phase = str(payload.get("phase", ""))
    state = STATES.get(phase, phase)
    if state == "finished" and payload.get("error"):
        state = "failed"
    updated = _moment(payload.get("updated_at"))
    moment = now or datetime.now(UTC)
    if state in RUNNING and (updated is None or moment - updated > STALE_AFTER):
        state = "stopped"
    raw = payload.get("progress")
    progress: Mapping[str, object] = raw if isinstance(raw, dict) else {}
    return {
        "state": state,
        "started_at": payload.get("started_at") or payload.get("triggered_at"),
        "finished_at": payload.get("finished_at"),
        "outcome": payload.get("outcome"),
        "employers_checked": _count(progress, "complete"),
        "employers_total": max(_count(progress, "total"), _count(progress, "complete")),
        "warnings": _count(progress, "warnings"),
        "error": payload.get("error"),
    }


def start_sync(
    database: Path,
    *,
    wait: float = START_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncStart:
    current = sync_progress(database)
    if current is not None and current["state"] in RUNNING:
        return SyncStart(False, "A sync Stage started is already running.", current)
    database.parent.mkdir(parents=True, exist_ok=True)
    progress_path(database).unlink(missing_ok=True)
    try:
        process = _launch(database)
    except OSError as exc:
        return SyncStart(False, f"Stage could not start a sync: {exc.strerror or exc}.")
    _CHILDREN.append(process)
    deadline = time.monotonic() + wait
    while True:
        found = sync_progress(database)
        if found is not None and found["state"] == "blocked":
            said = str(found["error"] or "Another sync is already running.")
            return SyncStart(False, said, found)
        if found is not None and found["state"] == "failed":
            return SyncStart(False, str(found["error"] or "The sync stopped at once."), found)
        if found is not None and found["state"] == "finished":
            return SyncStart(True, "The sync already finished.", found)
        if found is not None and found["state"] in RUNNING:
            return SyncStart(True, "The sync started.", found)
        if process.poll() is not None:
            return SyncStart(False, f"The sync stopped at once: {_tail(database)}")
        if time.monotonic() >= deadline:
            return SyncStart(True, "The sync is starting.", found or {})
        sleep(POLL_SECONDS)


def _launch(database: Path) -> "subprocess.Popen[bytes]":
    with log_path(database).open("wb") as log:
        if os.name == "nt":
            return subprocess.Popen(
                sync_command(database),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=database.parent,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            )
        return subprocess.Popen(
            sync_command(database),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=database.parent,
            start_new_session=True,
        )


def _reap() -> None:
    _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]


def _tail(database: Path) -> str:
    try:
        said = log_path(database).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "no output"
    return " ".join(said.split())[-LOG_TAIL:] or "no output"


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _count(progress: Mapping[str, object], key: str) -> int:
    value = progress.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = [
    "SyncStart",
    "log_path",
    "progress_path",
    "start_sync",
    "sync_command",
    "sync_progress",
]
