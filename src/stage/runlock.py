import importlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, cast

LOCK_NAME = "network.lock"


class AnotherRunInProgressError(Exception):
    def __init__(self, message: str, holder: str = "") -> None:
        super().__init__(message)
        self.holder = holder


def lock_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / LOCK_NAME


def run_lock_path(database: Path) -> Path:
    return database.with_name(f".{database.name}.lock")


def _try_lock(handle: Any) -> bool:
    try:
        if os.name == "nt":
            msvcrt = cast(Any, importlib.import_module("msvcrt"))

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = cast(Any, importlib.import_module("fcntl"))
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _holder(path: Path) -> str:
    try:
        return path.with_suffix(".owner").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@contextmanager
def single_run(
    command: str,
    path: Path | None = None,
    *,
    wait_s: float = 0.0,
    busy: str = "",
    waiting: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    target = path or lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    owner = target.with_suffix(".owner")
    try:
        handle = target.open("a+b")
    except OSError as exc:
        raise AnotherRunInProgressError(
            f"Stage could not open its run lock at {target} ({exc.strerror or exc}). "
            f"Check that {target.parent} is writable."
        ) from exc
    with handle:
        deadline = clock() + max(0.0, wait_s)
        announced = False
        while not _try_lock(handle):
            if clock() >= deadline:
                holder = _holder(target)
                advice = (busy or f"run stage {command} again when it finishes").rstrip(".")
                raise AnotherRunInProgressError(
                    f"{holder or f'Another program holding {target}'} is already running. "
                    f"{advice[:1].upper()}{advice[1:]}.",
                    holder or f"another program holding {target}",
                )
            if waiting is not None and not announced:
                waiting(_holder(target) or f"another program holding {target}")
                announced = True
            sleep(min(1.0, max(0.0, deadline - clock())))
        with suppress(OSError):
            started = datetime.now().astimezone()
            owner.write_text(
                f"stage {command} (pid {os.getpid()}, started {started:%Y-%m-%d %H:%M})",
                encoding="utf-8",
            )
        try:
            yield
        finally:
            with suppress(OSError):
                owner.unlink(missing_ok=True)
