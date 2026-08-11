import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

FAST_PATH_BUDGET_MS = 100.0
RUNS = 5

DEPENDENCY_FLOOR = "import asyncio, sqlite3, typer, rich.console, yaml"
RUNNER = "from stage.cli.app import app; app()"

FAST_PATH_COMMANDS = (
    ("list",),
    ("search", "montreal"),
    ("show", "greenhouse:absent:1"),
    ("export", "--format", "csv", "--force"),
)


def _best(arguments: list[str], env: dict[str, str]) -> float:
    fastest = float("inf")
    for _ in range(RUNS):
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, *arguments], capture_output=True, env=env, check=False
        )
        assert result.returncode in (0, 1), (
            f"{arguments} exited {result.returncode}: {result.stderr.decode(errors='replace')}"
        )
        fastest = min(fastest, time.perf_counter() - started)
    return fastest * 1000


@pytest.fixture
def budget_env(tmp_path: Path) -> dict[str, str]:
    return dict(os.environ, STAGE_DB=str(tmp_path / "startup.db"))


def test_the_fast_path_stays_inside_its_budget_over_the_dependency_floor(
    budget_env: dict[str, str], tmp_path: Path
) -> None:
    floor = _best(["-c", DEPENDENCY_FLOOR], budget_env)
    over_budget: list[str] = []
    measured: list[str] = []

    for command in FAST_PATH_COMMANDS:
        arguments = ["-c", RUNNER, *command]
        if command[0] == "export":
            arguments += ["--out", str(tmp_path / f"{command[0]}.csv")]
        elapsed = _best(arguments, budget_env)
        cost = elapsed - floor
        measured.append(f"{command[0]} {elapsed:.0f}ms (floor + {cost:.0f}ms)")
        if cost > FAST_PATH_BUDGET_MS:
            over_budget.append(f"{command[0]} costs {cost:.0f}ms over the floor")

    assert not over_budget, (
        f"{FAST_PATH_BUDGET_MS:.0f}ms over a {floor:.0f}ms floor: {', '.join(measured)}"
    )
