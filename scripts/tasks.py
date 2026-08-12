import argparse
import os
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path


TASK_NAMES = ("Stage sync", "Stage discover")
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_stage_exe() -> Path:
    return project_root() / ".venv" / "Scripts" / "stage.exe"


def default_log_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "stage" / "logs"


def task_time(value: str) -> str:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as error:
        raise argparse.ArgumentTypeError("use 24-hour HH:MM") from error
    return value


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def new_log_path(log_dir: Path, prefix: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.log"


def run_logged(command: Sequence[str], log_path: Path, *, append: bool = False) -> int:
    mode = "a" if append else "w"
    environment = os.environ | {"PYTHONIOENCODING": "utf-8"}
    with log_path.open(mode, encoding="utf-8") as log:
        result = subprocess.run(
            command,
            check=False,
            encoding="utf-8",
            env=environment,
            errors="replace",
            stderr=subprocess.STDOUT,
            stdout=log,
            text=True,
        )
    return result.returncode


def prune_logs(log_dir: Path, prefix: str, keep: int) -> None:
    logs = sorted(log_dir.glob(f"{prefix}-*.log"), key=lambda path: path.stat().st_mtime)
    for log in logs[:-keep]:
        log.unlink()


def run_sync(args: argparse.Namespace) -> int:
    log_path = new_log_path(args.log_dir, "sync")
    sync_command = [str(args.stage_exe), "sync"]
    if args.dry_run:
        sync_command.append("--dry-run")
    sync_exit = run_logged(sync_command, log_path)
    doctor_exit = run_logged([str(args.stage_exe), "doctor"], log_path, append=True)
    prune_logs(args.log_dir, "sync", 30)
    return doctor_exit if doctor_exit else sync_exit


def run_discover(args: argparse.Namespace) -> int:
    log_path = new_log_path(args.log_dir, "discover")
    exit_code = run_logged(
        [
            str(args.stage_exe),
            "discover",
            "--unregistered",
            "--apply",
            "--limit",
            str(args.limit),
        ],
        log_path,
    )
    prune_logs(args.log_dir, "discover", 12)
    return exit_code


def task_command(python: Path, command: str, *arguments: str) -> str:
    return subprocess.list2cmdline(
        [str(python), str(Path(__file__).resolve()), command, *arguments]
    )


def remove_tasks() -> None:
    for name in TASK_NAMES:
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", name, "/F"],
            check=False,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        message = "removed" if result.returncode == 0 else "was not registered"
        print(f"{name} {message}")


def create_task(name: str, schedule: str, time: str, command: str, day: str | None = None) -> None:
    task = ["schtasks", "/Create", "/F", "/SC", schedule, "/ST", time, "/TN", name, "/TR", command]
    if day is not None:
        task.extend(["/D", day])
    subprocess.run(task, check=True)


def install_tasks(args: argparse.Namespace) -> int:
    if args.uninstall:
        remove_tasks()
        return 0
    python = project_root() / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise SystemExit(f"missing project interpreter: {python}")
    create_task("Stage sync", "DAILY", args.sync_time, task_command(python, "sync"))
    create_task(
        "Stage discover",
        "WEEKLY",
        args.discover_time,
        task_command(python, "discover", "--limit", str(args.discover_limit)),
        args.discover_day,
    )
    print("registered:")
    for name in TASK_NAMES:
        subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"], check=True)
    print('run one now: schtasks /Run /TN "Stage sync"')
    print("remove both: uv run python scripts/tasks.py install --uninstall")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or install the Stage Windows Scheduled Tasks."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="run the daily sync and doctor checks")
    sync.add_argument("--stage-exe", default=default_stage_exe(), type=Path)
    sync.add_argument("--log-dir", default=default_log_dir(), type=Path)
    sync.add_argument("--dry-run", action="store_true")
    sync.set_defaults(handler=run_sync)

    discover = commands.add_parser("discover", help="run weekly unregistered-company discovery")
    discover.add_argument("--stage-exe", default=default_stage_exe(), type=Path)
    discover.add_argument("--log-dir", default=default_log_dir(), type=Path)
    discover.add_argument("--limit", default=40, type=positive_int)
    discover.set_defaults(handler=run_discover)

    install = commands.add_parser("install", help="install or remove the two Windows tasks")
    install.add_argument("--uninstall", action="store_true")
    install.add_argument("--sync-time", default="09:00", type=task_time)
    install.add_argument("--discover-day", choices=DAYS, default="MON", type=str.upper)
    install.add_argument("--discover-time", default="09:00", type=task_time)
    install.add_argument("--discover-limit", default=40, type=positive_int)
    install.set_defaults(handler=install_tasks)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
