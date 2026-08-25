import json
import os
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from stage.domain import SyncOutcome
from stage.domain.text import dump, summary
from stage.paths import data_dir

if TYPE_CHECKING:
    from stage.domain import DiscoveryEvent, SyncEvent

HEARTBEAT_INTERVAL_SECONDS = 1.0
_HEARTBEAT_MISSES_BEFORE_UNRESPONSIVE = 3
_STATE_RETRY_DELAYS_SECONDS = (0.0, 0.02, 0.1)
_ACTIVE_PHASES = frozenset({"waiting", "syncing", "discovering", "checking"})
_OUTCOMES = frozenset(outcome.value for outcome in SyncOutcome)


def state_path(action: str) -> Path:
    root = data_dir() / "schedule"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{action}.json"


def read_state(action: str) -> dict[str, object] | None:
    return read_state_path(state_path(action))


def read_state_path(path: Path) -> dict[str, object] | None:
    for delay in _STATE_RETRY_DELAYS_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            with path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError):
            continue
        return payload if isinstance(payload, dict) else None
    return None


def state_for_status(
    payload: dict[str, object] | None, *, now: datetime | None = None
) -> dict[str, object] | None:
    if payload is None or payload.get("phase") not in _ACTIVE_PHASES:
        return payload
    interval = _positive_number(payload, "heartbeat_interval_seconds")
    heartbeat = _time(payload.get("heartbeat_at"))
    if interval <= 0 or heartbeat is None:
        return payload
    age = (now or datetime.now(UTC)).astimezone(UTC) - heartbeat
    if age < timedelta(seconds=interval * _HEARTBEAT_MISSES_BEFORE_UNRESPONSIVE):
        return payload
    status = dict(payload)
    status["phase"] = "unresponsive"
    status["heartbeat_age_seconds"] = round(age.total_seconds())
    return status


def is_active(payload: dict[str, object] | None) -> bool:
    status = state_for_status(payload)
    return bool(status and status.get("phase") in _ACTIVE_PHASES)


class ScheduleStateWriter:
    def __init__(self, path: Path, payload: dict[str, object]) -> None:
        self.path = path
        self._payload = payload
        self._last_write = 0.0

    @classmethod
    def start(
        cls,
        action: str,
        log_path: Path,
        *,
        destination: Path | None = None,
        now: datetime | None = None,
    ) -> "ScheduleStateWriter":
        moment = now or datetime.now(UTC)
        stamp = _stamp(moment)
        return cls(
            destination or state_path(action),
            {
                "schema_version": 2,
                "action": action,
                "run_id": uuid.uuid4().hex,
                "phase": "waiting",
                "triggered_at": stamp,
                "updated_at": stamp,
                "heartbeat_at": stamp,
                "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                "log_path": str(log_path),
                "progress": _progress(),
            },
        )

    @classmethod
    def open(cls, path: Path, action: str) -> "ScheduleStateWriter":
        payload = read_state_path(path)
        if payload is None or payload.get("action") != action:
            return cls.start(action, Path(), destination=path)
        return cls(path, payload)

    @property
    def path_is_blocked(self) -> bool:
        self._reload()
        return self._payload.get("phase") == "blocked"

    def waiting(self, delay_seconds: float) -> None:
        moment = datetime.now(UTC)
        self._set(
            force=True,
            phase="waiting",
            jitter_seconds=round(delay_seconds),
            starts_after=_stamp(moment + timedelta(seconds=delay_seconds)),
        )

    def started(self, phase: str) -> None:
        values: dict[str, object] = {"phase": phase}
        if not self._payload.get("started_at"):
            values["started_at"] = _stamp(datetime.now(UTC))
        self._set(values, force=True)

    def heartbeat(self) -> None:
        self._reload()
        self._set(force=True, heartbeat_at=_stamp(datetime.now(UTC)))

    def checking(self) -> None:
        self._reload()
        self._set(force=True, phase="checking")

    def complete(
        self,
        *,
        sync_exit_code: int | None,
        doctor_exit_code: int | None,
        exit_code: int,
    ) -> None:
        self._reload()
        values: dict[str, object] = {
            "finished_at": _stamp(datetime.now(UTC)),
            "sync_exit_code": sync_exit_code,
            "doctor_exit_code": doctor_exit_code,
            "exit_code": exit_code,
        }
        if self._payload.get("phase") != "blocked":
            values["phase"] = "finished"
            values["outcome"] = _outcome(
                self._payload.get("sync_outcome"), exit_code, doctor_exit_code
            )
        self._set(values, force=True)

    def failed(self, detail: str, *, exit_code: int = 2) -> None:
        self._reload()
        self._set(
            force=True,
            phase="finished",
            outcome=SyncOutcome.FAILURE.value,
            finished_at=_stamp(datetime.now(UTC)),
            exit_code=exit_code,
            error=summary(detail, 240),
        )

    def blocked(self, detail: str) -> None:
        self._reload()
        self._set(
            force=True,
            phase="blocked",
            blocked_at=_stamp(datetime.now(UTC)),
            error=summary(detail, 240),
        )

    def sync_event(self, event: "SyncEvent") -> None:
        from stage.domain import (
            CompanyDeferred,
            CompanyFailed,
            CompanyFinished,
            CompanyStarted,
            CompanyUnchanged,
            SourceBlocked,
            SourceFailed,
            SourceStarted,
            SyncFinished,
            SyncStarted,
        )

        progress = _progress_from(self._payload)
        values: dict[str, object] = {"phase": "syncing"}
        if isinstance(event, SyncStarted):
            progress["total"] = event.companies
        elif isinstance(event, SourceStarted):
            progress["source"] = event.source
            progress["source_total"] = event.companies
            progress["source_complete"] = 0
        elif isinstance(event, CompanyStarted):
            progress["source"] = event.source
            progress["company"] = event.company
        elif isinstance(event, (CompanyFinished, CompanyUnchanged, CompanyFailed, CompanyDeferred)):
            _increment(progress, "complete")
            _increment(progress, "source_complete")
            progress["company"] = event.company
            if isinstance(event, CompanyFinished):
                _increment(progress, "fetched", event.fetched)
                if event.degraded:
                    _increment(progress, "warnings")
            elif isinstance(event, (CompanyFailed, CompanyDeferred)):
                _increment(progress, "warnings")
        elif isinstance(event, (SourceBlocked, SourceFailed)):
            progress["source"] = event.source
            _increment(progress, "warnings")
        elif isinstance(event, SyncFinished):
            values["sync_outcome"] = event.outcome.value
            progress["fetched"] = max(_number(progress, "fetched"), event.added + event.updated)
            _increment(progress, "warnings", len(event.failed_sources))
        values["progress"] = progress
        self._set(values, force=isinstance(event, SyncFinished))

    def discovery_event(self, event: "DiscoveryEvent") -> None:
        from stage.domain import DiscoveryFinished, DiscoveryStarted, PlatformProbed, ProbeVerdict

        progress = _progress_from(self._payload)
        values: dict[str, object] = {"phase": "discovering"}
        if isinstance(event, DiscoveryStarted):
            progress["total"] = event.probes_planned
        elif isinstance(event, PlatformProbed):
            _increment(progress, "complete")
            progress["company"] = event.result.company
            progress["source"] = event.result.candidate.label
            if event.result.verdict is ProbeVerdict.UNVERIFIED:
                _increment(progress, "warnings")
        elif isinstance(event, DiscoveryFinished):
            progress["fetched"] = len(event.matched)
            _increment(progress, "warnings", event.errors + len(event.unverified))
        values["progress"] = progress
        self._set(values, force=isinstance(event, DiscoveryFinished))

    def _reload(self) -> None:
        payload = read_state_path(self.path)
        if payload is not None and payload.get("run_id") == self._payload.get("run_id"):
            self._payload = payload

    def _set(
        self,
        values: Mapping[str, object] | None = None,
        *,
        force: bool = False,
        **extra: object,
    ) -> None:
        if values is not None:
            self._payload.update(values)
        self._payload.update(extra)
        self._payload["updated_at"] = _stamp(datetime.now(UTC))
        now = time.monotonic()
        if not force and now - self._last_write < 0.5:
            return
        self._write()
        self._last_write = now

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staged: Path | None = None
        try:
            with suppress(OSError):
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    staged = Path(stream.name)
                    stream.write(dump(self._payload))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                for delay in _STATE_RETRY_DELAYS_SECONDS:
                    if delay:
                        time.sleep(delay)
                    with suppress(OSError):
                        staged.replace(self.path)
                        return
        finally:
            if staged is not None:
                with suppress(OSError):
                    staged.unlink(missing_ok=True)


def _outcome(sync_outcome: object, exit_code: int, doctor_exit_code: int | None) -> str:
    if exit_code >= 2 or doctor_exit_code not in (None, 0):
        return SyncOutcome.FAILURE.value
    if isinstance(sync_outcome, str) and sync_outcome in _OUTCOMES:
        return sync_outcome
    return SyncOutcome.PARTIAL.value if exit_code else SyncOutcome.SUCCESS.value


def _progress() -> dict[str, object]:
    return {
        "total": 0,
        "complete": 0,
        "source": "",
        "source_total": 0,
        "source_complete": 0,
        "company": "",
        "fetched": 0,
        "warnings": 0,
    }


def _progress_from(payload: dict[str, object]) -> dict[str, object]:
    progress = _progress()
    stored = payload.get("progress")
    if isinstance(stored, dict):
        for key in progress:
            if key in stored:
                progress[key] = stored[key]
    return progress


def _increment(progress: dict[str, object], key: str, amount: int = 1) -> None:
    progress[key] = _number(progress, key) + amount


def _number(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) else 0


def _positive_number(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    return float(value) if isinstance(value, int | float) and value > 0 else 0.0


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo is not None else None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()
