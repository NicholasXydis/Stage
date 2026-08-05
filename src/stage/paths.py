import os
import stat
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
    packaged = Path(__file__).resolve().parent / "data" / "companies.yaml"
    if packaged.exists():
        return packaged
    return config_dir() / "companies.yaml"


def lexicon_dir() -> Path:
    override = os.environ.get("STAGE_LEXICON")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "lexicon"


def capture_dir() -> Path:
    override = os.environ.get("STAGE_CAPTURE_DIR")
    root = Path(override).expanduser() if override else data_dir() / "captured"
    root.mkdir(parents=True, exist_ok=True)
    return root


def restrict_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
