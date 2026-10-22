import logging
import os
import sys
from pathlib import Path

import pytest

from stage.services.connect import (
    EXPECTED_TOOL,
    PATH_SETTINGS,
    ServerEntry,
    bare_environment,
    self_test,
    server_entry,
)


def test_an_entry_runs_the_stage_program_beside_this_python(tmp_path: Path) -> None:
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    program = tmp_path / "bin" / "stage"
    program.write_text("", encoding="utf-8")

    entry = server_entry(str(python), environ={})

    assert entry.command == str(program)
    assert entry.args == ("mcp",)
    assert entry.env == {}


def test_without_the_program_an_entry_runs_this_python_as_a_module(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")

    entry = server_entry(str(python), environ={})

    assert entry.command == str(python)
    assert entry.args == ("-m", "stage", "mcp")


def test_the_installed_entry_is_an_absolute_path() -> None:
    assert Path(server_entry(environ={}).command).is_absolute()


def test_an_entry_carries_every_stage_setting_as_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = {name: f"relative/{name.lower()}" for name in PATH_SETTINGS}
    settings["STAGE_DB"] = "~/stage.db"
    settings["UNRELATED"] = "kept out"

    entry = server_entry(environ=settings)

    assert set(entry.env) == set(PATH_SETTINGS)
    assert all(Path(value).is_absolute() for value in entry.env.values())
    assert entry.env["STAGE_REGISTRY"] == str((tmp_path / "relative/stage_registry").resolve())
    assert entry.env["STAGE_DB"] == str(Path("~/stage.db").expanduser().resolve())


def test_a_blank_setting_is_left_out_and_a_chosen_database_wins(tmp_path: Path) -> None:
    entry = server_entry(
        environ={"STAGE_DB": "  ", "STAGE_LEXICON": ""}, database=tmp_path / "x.db"
    )

    assert entry.env == {"STAGE_DB": str((tmp_path / "x.db").resolve())}


def test_the_bare_environment_is_what_a_desktop_app_passes() -> None:
    bare = bare_environment({"PATH": "/opt/homebrew/bin", "STAGE_DB": "/x", "SECRET": "1"})

    assert "STAGE_DB" not in bare and "SECRET" not in bare
    assert "/opt/homebrew/bin" not in bare["PATH"]


async def test_the_self_test_starts_stage_and_lists_its_tools(tmp_path: Path) -> None:
    database = tmp_path / "self-test.db"

    result = await self_test(server_entry(environ={"STAGE_DB": str(database)}))

    assert result.passed, result.detail
    assert result.tools >= 10
    assert "answered" in result.detail
    assert not database.exists()


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        (ServerEntry(str(Path("/nonexistent/stage/python")), ("-m", "stage", "mcp")), "No such"),
        (ServerEntry(sys.executable, ("-c", "import sys; sys.exit('broken install')")), "broken"),
        (ServerEntry(sys.executable, ("-c", "print('not a server')")), "Stage"),
    ],
)
async def test_the_self_test_says_why_stage_did_not_start(entry: ServerEntry, said: str) -> None:
    result = await self_test(entry, timeout=30)

    assert not result.passed
    assert said in result.detail


async def test_the_self_test_gives_up_on_a_silent_program(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = ServerEntry(sys.executable, ("-c", "import time; time.sleep(60)"))

    with caplog.at_level(logging.DEBUG):
        result = await self_test(entry, timeout=1)

    assert not result.passed
    assert "within 1 seconds" in result.detail
    assert logging.getLogger("mcp").level == logging.NOTSET


async def test_something_that_is_not_stage_does_not_pass(tmp_path: Path) -> None:
    script = tmp_path / "impostor.py"
    script.write_text(
        "from mcp.server import MCPServer\n"
        "server = MCPServer('impostor')\n"
        "@server.tool()\n"
        "def hello() -> str:\n"
        "    return 'hi'\n"
        "server.run()\n",
        encoding="utf-8",
    )

    result = await self_test(ServerEntry(sys.executable, (str(script),)), timeout=30)

    assert not result.passed
    assert "not Stage" in result.detail
    assert EXPECTED_TOOL == "search_postings"


@pytest.mark.skipif(os.name == "nt", reason="POSIX search path")
def test_the_bare_path_never_includes_a_users_own_tools() -> None:
    assert (
        bare_environment({"PATH": "/Users/x/.local/bin"})["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    )
