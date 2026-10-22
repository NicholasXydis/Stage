import json
import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stage.cli.app import app
from stage.domain.ai_apps import AiApp, ConfigFormat, SettingsFile, Shape, Where
from stage.services.connect import SelfTest

ANSI = re.compile(r"\x1b\[[0-9;]*m")

ALPHA = AiApp(
    "alpha",
    "Alpha",
    file=SettingsFile((Where.everywhere("~/.alpha/mcp.json"),), ConfigFormat.JSON, ("mcpServers",)),
    markers=(Where.everywhere("~/.alpha"),),
    note="Alpha reads it on its next start.",
)
BETA = AiApp(
    "beta-app",
    "Beta App",
    file=SettingsFile(
        (Where.everywhere("~/.beta/config.toml"),), ConfigFormat.TOML, ("mcp_servers",), Shape.PLAIN
    ),
    markers=(Where.everywhere("~/.beta"),),
)
GAMMA = AiApp(
    "gamma",
    "Gamma",
    markers=(Where.everywhere("~/.gamma"),),
    screens="Open Gamma's settings and paste:\n{json}",
)
DELTA = AiApp(
    "delta",
    "Delta",
    file=SettingsFile((Where.everywhere("~/.delta/mcp.json"),), ConfigFormat.JSON, ("mcpServers",)),
    markers=(Where.everywhere("~/.delta"),),
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from stage.services import ai_catalogue

    folder = Path.home()
    for name in (".alpha", ".beta", ".gamma"):
        (folder / name).mkdir(parents=True)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(ai_catalogue, "APPS", (ALPHA, BETA, GAMMA, DELTA))
    monkeypatch.setattr(ai_catalogue, "EDITORS", ())
    monkeypatch.setattr("stage.services.connect.self_test", _passing)
    return folder


async def _passing(*_: object, **__: object) -> "SelfTest":
    return SelfTest(True, "Stage answered with 10 tools in 0.1s", 10)


def _run(*arguments: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(arguments))
    return result.exit_code, " ".join(ANSI.sub("", result.output).split())


def test_status_lists_only_what_is_installed(home: Path) -> None:
    code, said = _run("connect", "--status")

    assert code == 0
    assert "Alpha" in said and "Beta App" in said and "Gamma" in said
    assert "Delta" not in said
    assert "not connected" in said and "check in the app" in said


def test_every_known_app_can_be_listed(home: Path) -> None:
    assert "Delta" in _run("connect", "--status", "--every")[1]


def test_without_a_terminal_stage_asks_for_names_instead_of_guessing(home: Path) -> None:
    code, said = _run("connect")

    assert code == 2
    assert "stage connect alpha" in said
    assert not (home / ".alpha" / "mcp.json").exists()


def test_named_apps_are_connected_and_their_notes_shown(home: Path) -> None:
    code, said = _run("connect", "alpha", "Beta App")

    assert code == 0
    stored = json.loads((home / ".alpha" / "mcp.json").read_text(encoding="utf-8"))
    assert stored["mcpServers"]["Stage"]["args"] == ["mcp"]
    assert (
        "Stage"
        in tomllib.loads((home / ".beta" / "config.toml").read_text(encoding="utf-8"))[
            "mcp_servers"
        ]
    )
    assert "Alpha reads it on its next start." in said
    assert "~/.alpha/mcp.json" in said
    assert "connected" in _run("connect", "--status")[1]


def test_all_connects_everything_found_and_shows_steps_for_the_rest(home: Path) -> None:
    code, said = _run("connect", "--all")

    assert code == 0
    assert (home / ".alpha" / "mcp.json").exists() and (home / ".beta" / "config.toml").exists()
    assert "Open Gamma's settings and paste" in said and '"Stage"' in said
    assert not (home / ".delta").exists()


def test_a_dry_run_writes_nothing(home: Path) -> None:
    code, said = _run("connect", "--all", "--dry-run")

    assert code == 0 and "would create" in said
    assert not (home / ".alpha" / "mcp.json").exists()


def test_a_misspelt_app_gets_a_suggestion_and_a_missing_one_says_so(home: Path) -> None:
    misspelt = _run("connect", "alhpa")
    missing = _run("connect", "delta")

    assert misspelt[0] == 2 and "Did you mean alpha?" in misspelt[1]
    assert missing[0] == 2 and "did not find delta" in missing[1]


def test_connecting_twice_changes_nothing_the_second_time(home: Path) -> None:
    _run("connect", "alpha")
    before = (home / ".alpha" / "mcp.json").read_bytes()

    code, said = _run("connect", "alpha")

    assert code == 0 and "already connected" in said
    assert (home / ".alpha" / "mcp.json").read_bytes() == before


def test_a_file_stage_cannot_read_fails_loudly_and_is_left_alone(home: Path) -> None:
    broken = home / ".alpha" / "mcp.json"
    broken.write_text("{ not json", encoding="utf-8")

    code, said = _run("connect", "alpha")

    assert code == 1
    assert "not valid JSON" in said and "Add this to" in said
    assert broken.read_text(encoding="utf-8") == "{ not json"


def test_disconnecting_removes_only_stage(home: Path) -> None:
    target = home / ".alpha" / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    _run("connect", "alpha")

    code, said = _run("disconnect", "alpha")

    assert code == 0 and "removed Stage" in said
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "mcpServers": {"other": {"command": "x"}}
    }


def test_nothing_found_is_said_plainly(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.services import ai_catalogue

    monkeypatch.setattr(ai_catalogue, "APPS", (DELTA,))

    code, said = _run("connect", "--all")

    assert code == 1 and "found no AI app" in said


def test_a_chosen_database_is_written_into_the_entry(home: Path, tmp_path: Path) -> None:
    _run("connect", "alpha", "--db", str(tmp_path / "mine.db"))

    stored = json.loads((home / ".alpha" / "mcp.json").read_text(encoding="utf-8"))
    assert stored["mcpServers"]["Stage"]["env"]["STAGE_DB"] == str((tmp_path / "mine.db").resolve())


@pytest.mark.parametrize(
    ("answer", "chosen"),
    [("1 3", ["alpha", "gamma"]), ("all", ["alpha", "beta-app", "gamma"]), ("2,2", ["beta-app"])],
)
def test_choosing_by_number(home: Path, answer: str, chosen: list[str]) -> None:
    from stage.cli.commands.ai import _picked
    from stage.cli.render import terminal
    from stage.services.connect import Machine, targets

    found = targets(Machine.here())

    assert [target.app.key for target in _picked(terminal(), found, answer)] == chosen


@pytest.mark.parametrize("answer", ["", "0", "9", "one"])
def test_an_empty_or_wrong_choice_changes_nothing(home: Path, answer: str) -> None:
    import typer

    from stage.cli.commands.ai import _picked
    from stage.cli.render import terminal
    from stage.services.connect import Machine, targets

    with pytest.raises(typer.Exit):
        _picked(terminal(), targets(Machine.here()), answer)
    assert not (home / ".alpha" / "mcp.json").exists()


def test_the_self_test_runs_after_a_change_and_its_failure_is_an_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*_: object, **__: object) -> SelfTest:
        return SelfTest(False, "Stage failed to start: boom")

    monkeypatch.setattr("stage.services.connect.self_test", failing)

    code, said = _run("connect", "alpha")

    assert code == 1 and "Self-test: Stage failed to start: boom" in said


def test_the_self_test_is_skipped_when_nothing_changed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    async def counted(*arguments: object, **__: object) -> SelfTest:
        calls.append(arguments)
        return SelfTest(True, "fine", 10)

    monkeypatch.setattr("stage.services.connect.self_test", counted)
    _run("connect", "alpha")
    _run("connect", "alpha")
    _run("connect", "alpha", "--dry-run")

    assert len(calls) == 1


def test_doctor_names_connected_apps_and_how_to_fix_a_stale_one(home: Path, tmp_path: Path) -> None:
    _run("connect", "alpha", "beta-app")
    stale = home / ".beta" / "config.toml"
    text = stale.read_text(encoding="utf-8")
    stale.write_text(text.replace('args = ["mcp"]', 'args = ["mcp", "--old"]'), encoding="utf-8")

    said = _run("doctor", "--db", str(tmp_path / "d.db"))[1]
    payload = json.loads(
        CliRunner().invoke(app, ["doctor", "--json", "--db", str(tmp_path / "d.db")]).stdout
    )

    assert "AI apps" in said and "Alpha connected" in said
    assert "Beta App outdated" in said and "run stage connect beta-app" in said
    assert [entry["status"] for entry in payload["ai_apps"]] == ["connected", "outdated"]


def test_doctor_says_when_no_app_is_connected(home: Path, tmp_path: Path) -> None:
    assert (
        "none connected; stage connect sets one up"
        in _run("doctor", "--db", str(tmp_path / "d.db"))[1]
    )


def test_a_file_stage_cannot_edit_on_the_way_out_says_what_to_delete(home: Path) -> None:
    _run("connect", "alpha")
    target = home / ".alpha" / "mcp.json"
    target.write_text("// mine\n" + target.read_text(encoding="utf-8"), encoding="utf-8")

    code, said = _run("disconnect", "alpha")

    assert code == 1
    assert "Delete the Stage entry under mcpServers in that file yourself." in said
    assert target.read_text(encoding="utf-8").startswith("// mine")


def test_every_without_status_is_refused_instead_of_ignored(home: Path) -> None:
    code, said = _run("connect", "--every", "alpha")

    assert code == 2 and "--every lists apps with --status" in said
    assert not (home / ".alpha" / "mcp.json").exists()


def test_commands_and_snippets_keep_full_paths_for_copying(home: Path) -> None:
    broken = home / ".alpha" / "mcp.json"
    broken.write_text("{ not json", encoding="utf-8")

    said = CliRunner().invoke(app, ["connect", "alpha"]).output

    assert f"Add this to {broken}:" in said
    assert '"command": "~' not in said and "Add this to ~" not in said
