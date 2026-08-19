import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

LEAN_BUDGET_MS = 100.0
LEXICON_BUDGET_MS = 140.0
RUNS = 5

if sys.version_info >= (3, 14):
    LEAN_BUDGET_MS = 130.0
    LEXICON_BUDGET_MS = 170.0

if os.name == "nt":
    LEAN_BUDGET_MS = 240.0
    LEXICON_BUDGET_MS = 260.0

DEPENDENCY_FLOOR = "import asyncio, sqlite3, typer, rich.console, yaml"
RUNNER = "from stage.cli.app import app; app()"

FAST_PATH_COMMANDS = (
    (LEAN_BUDGET_MS, ("list",)),
    (LEXICON_BUDGET_MS, ("search", "montreal")),
    (LEAN_BUDGET_MS, ("show", "greenhouse:absent:1")),
    (LEAN_BUDGET_MS, ("export", "--format", "csv", "--force")),
)

LEXICON_PROBE = (
    "import sys\n"
    "from stage.cli.app import app\n"
    "try:\n"
    "    app(['search', 'montreal'])\n"
    "except SystemExit:\n"
    "    pass\n"
    "print('LEXICON:' + str('stage.lexicon' in sys.modules), file=sys.stderr)\n"
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
    from stage.storage.sqlite_repo import SqliteRepository

    database = tmp_path / "startup.db"
    SqliteRepository.connect(database).close()
    return dict(os.environ, STAGE_DB=str(database))


@pytest.mark.serial
def test_the_fast_path_stays_inside_its_budget_over_the_dependency_floor(
    budget_env: dict[str, str], tmp_path: Path
) -> None:
    floor = _best(["-c", DEPENDENCY_FLOOR], budget_env)
    over_budget: list[str] = []
    measured: list[str] = []

    for budget, command in FAST_PATH_COMMANDS:
        arguments = ["-c", RUNNER, *command]
        if command[0] == "export":
            arguments += ["--out", str(tmp_path / f"{command[0]}.csv")]
        elapsed = _best(arguments, budget_env)
        cost = elapsed - floor
        measured.append(f"{command[0]} {elapsed:.0f}ms (floor + {cost:.0f}ms of {budget:.0f})")
        if cost > budget:
            over_budget.append(f"{command[0]} costs {cost:.0f}ms over the floor")

    assert not over_budget, f"floor {floor:.0f}ms: {', '.join(measured)}"


@pytest.mark.serial
def test_only_search_pays_the_lexicon_so_its_wider_budget_is_earned(
    budget_env: dict[str, str],
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", LEXICON_PROBE],
        capture_output=True,
        text=True,
        env=budget_env,
        check=False,
    )
    assert result.stderr.strip().splitlines()[-1] == "LEXICON:True", (
        "search folds its query, so a budget above the lean one must reflect a real load"
    )
