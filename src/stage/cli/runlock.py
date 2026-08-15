import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

LOCK_NAME = "network.lock"


class AnotherRunInProgressError(Exception):
    pass


def lock_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / LOCK_NAME


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
        recorded = path.with_suffix(".owner").read_text(encoding="utf-8").strip()
    except OSError:
        return "another stage process"
    return recorded or "another stage process"


@contextmanager
def single_run(command: str, path: Path | None = None) -> Iterator[None]:
    target = path or lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    owner = target.with_suffix(".owner")
    try:
        handle = target.open("a+b")
    except OSError as exc:
        raise AnotherRunInProgressError(
            f"{target} could not be opened: {exc.strerror or exc}"
        ) from exc
    with handle:
        if not _try_lock(handle):
            raise AnotherRunInProgressError(
                f"{_holder(target)} is already running; a second {command} would double the rate"
            )
        with suppress(OSError):
            owner.write_text(
                f"stage {command} (pid {os.getpid()}, started {datetime.now(UTC).isoformat()})",
                encoding="utf-8",
            )
        try:
            yield
        finally:
            with suppress(OSError):
                owner.unlink(missing_ok=True)
