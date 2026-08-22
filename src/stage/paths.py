import getpass
import os
import stat
import subprocess
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "stage"
_DIRS = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=False)


def data_dir() -> Path:
    path = Path(_DIRS.user_data_dir)
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
            f"the embedded PDF font is missing from {packaged.parent} — "
            "export --format csv needs no font"
        )
    return packaged


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


def _restrict_windows_permissions(path: Path) -> None:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows").rstrip("\\/")
    executable = f"{root}\\System32\\icacls.exe"
    principal = f"{os.environ.get('USERDOMAIN', '.')}\\{getpass.getuser()}"
    result = subprocess.run(
        (executable, str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PermissionError(f"could not restrict permissions on {path}: {detail}")
