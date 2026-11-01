import importlib
import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from platformdirs import PlatformDirs

APP_NAME = "stage"
_DIRS = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=False)


def data_root() -> Path:
    return Path(_DIRS.user_data_dir)


def data_dir() -> Path:
    path = data_root()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    path = Path(_DIRS.user_config_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    override = os.environ.get("STAGE_DB")
    if override:
        return Path(override).expanduser().resolve()
    return data_dir() / "stage.db"


def registry_path() -> Path:
    override = os.environ.get("STAGE_REGISTRY")
    if override:
        return Path(override).expanduser().resolve()
    data = Path(__file__).resolve().parent / "data"
    for packaged in (data / "companies", data / "companies.yaml"):
        if packaged.exists():
            return packaged
    return config_dir() / "companies.yaml"


def lexicon_dir() -> Path:
    override = os.environ.get("STAGE_LEXICON")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "lexicon"


def font_path() -> Path:
    packaged = Path(__file__).resolve().parent / "data" / "fonts" / "DejaVuSans.ttf"
    if not packaged.exists():
        raise FileNotFoundError(
            f"the embedded PDF font is missing from {packaged.parent}; "
            "export --format csv needs no font"
        )
    return packaged


def logo_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "logo"


def card_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "cards" / "postings.html"


def capture_dir() -> Path:
    override = os.environ.get("STAGE_CAPTURE_DIR")
    root = Path(override).expanduser() if override else data_dir() / "captured"
    root.mkdir(parents=True, exist_ok=True)
    return root


def restrict_permissions(path: Path) -> None:
    if os.name == "nt":
        _restrict_windows_permissions(path)
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def create_private(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


def tighten_permissions(*paths: Path) -> None:
    if os.name == "nt":
        return
    for path in paths:
        try:
            mode = path.stat().st_mode
            if mode & 0o077:
                path.chmod(stat.S_IMODE(mode) & 0o700)
        except OSError:
            continue


@contextmanager
def exclusive(lock_path: Path) -> Iterator[None]:
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            msvcrt = cast(Any, importlib.import_module("msvcrt"))
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl = cast(Any, importlib.import_module("fcntl"))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def discard_new(path: Path) -> None:
    for leftover in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            continue


def _restrict_windows_permissions(path: Path) -> None:
    system = os.environ.get("SYSTEMROOT", r"C:\Windows").rstrip("\\/") + "\\System32"
    result = subprocess.run(
        (
            f"{system}\\icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{_user_sid(system)}:(F)",
        ),
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PermissionError(f"could not restrict permissions on {path}: {detail}")


@lru_cache(maxsize=1)
def _user_sid(system: str) -> str:
    result = subprocess.run(
        (f"{system}\\whoami.exe", "/user", "/fo", "csv", "/nh"),
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    sid = result.stdout.strip().rsplit(",", 1)[-1].strip().strip('"')
    if result.returncode != 0 or not sid.startswith("S-1-"):
        detail = (result.stderr or result.stdout).strip() or f"whoami exited {result.returncode}"
        raise PermissionError(f"could not read this account's security id: {detail}")
    return sid
