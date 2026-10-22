import base64
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from stage.domain.ai_apps import (
    AiApp,
    ConfigFormat,
    Editor,
    LinkStyle,
    OwnCommand,
    Reach,
    SettingsFile,
    Shape,
    System,
    Where,
    install_link,
)
from stage.services.app_config import BACKUP_SUFFIX
from stage.services.connect import (
    Machine,
    ServerEntry,
    Status,
    Target,
    connect,
    current_system,
    disconnect,
    expand,
    instructions,
    read_status,
    targets,
)

ENTRY = ServerEntry("/opt/stage/bin/stage", ("mcp",), {"STAGE_DB": "/data/stage.db"})
SERVERS = ("mcpServers",)


def _machine(tmp_path: Path, **environ: str) -> Machine:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    path = tmp_path / "bin"
    path.mkdir(exist_ok=True)
    return Machine(current_system(), home, {"PATH": str(path), **environ})


def _program(machine: Machine, name: str) -> Path:
    folder = Path(machine.environ["PATH"])
    target = folder / (f"{name}.exe" if os.name == "nt" else name)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def _app(
    key: str = "demo",
    *,
    where: str = "~/.demo/mcp.json",
    kind: ConfigFormat = ConfigFormat.JSON,
    key_path: tuple[str, ...] = SERVERS,
    shape: Shape = Shape.PLAIN,
    **fields: Any,
) -> AiApp:
    file = SettingsFile((Where.everywhere(where),), kind, key_path, shape)
    fields.setdefault("markers", (Where.everywhere("~/.demo"),))
    return AiApp(key, key.title(), file=file, **fields)


class Recorder:
    def __init__(self, code: int = 0, said: str = "") -> None:
        self.calls: list[list[str]] = []
        self.code = code
        self.said = said

    def __call__(self, command: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(command))
        return subprocess.CompletedProcess(list(command), self.code, "", self.said)


@pytest.mark.parametrize(
    ("system", "template", "expected"),
    [
        (System.MACOS, "{support}/Claude/x.json", "/h/Library/Application Support/Claude/x.json"),
        (System.WINDOWS, "{appdata}/Claude/x.json", "/h/AppData/Roaming/Claude/x.json"),
        (System.LINUX, "{config}/Claude/x.json", "/h/.config/Claude/x.json"),
        (System.LINUX, "{data}/Jan/data", "/h/.local/share/Jan/data"),
        (System.MACOS, "~/.cursor/mcp.json", "/h/.cursor/mcp.json"),
        (System.MACOS, "{env:CODEX_HOME|~/.codex}/config.toml", "/h/.codex/config.toml"),
        (
            System.MACOS,
            "{env:JAN_DATA_ROOT|{support}/Jan/data}/m.json",
            "/h/Library/Application Support/Jan/data/m.json",
        ),
    ],
)
def test_places_expand_for_every_system(system: System, template: str, expected: str) -> None:
    assert expand(template, Machine(system, Path("/h"), {})) == expected


def test_settings_that_move_a_folder_win_over_the_default() -> None:
    machine = Machine(
        System.LINUX,
        Path("/h"),
        {"CODEX_HOME": "/elsewhere", "XDG_CONFIG_HOME": "/xdg", "APPDATA": "C:/Roaming"},
    )

    assert expand("{env:CODEX_HOME|~/.codex}/config.toml", machine) == "/elsewhere/config.toml"
    assert expand("{config}/goose/config.yaml", machine) == "/xdg/goose/config.yaml"
    assert expand("{appdata}/Zed", machine) == "C:/Roaming/Zed"
    assert expand("{env:EMPTY|~/x}", Machine(System.LINUX, Path("/h"), {"EMPTY": " "})) == "/h/x"


def test_lm_studio_follows_its_pointer_then_its_old_folder(tmp_path: Path) -> None:
    machine = _machine(tmp_path)

    assert expand("{lmstudio}/mcp.json", machine) == f"{machine.home}/.lmstudio/mcp.json"
    (machine.home / ".cache" / "lm-studio").mkdir(parents=True)
    assert expand("{lmstudio}", machine) == str(machine.home / ".cache" / "lm-studio")
    (machine.home / ".lmstudio-home-pointer").write_text("/custom/lms\n", encoding="utf-8")
    assert expand("{lmstudio}", machine) == "/custom/lms"


def test_hermes_follows_its_active_profile_but_never_out_of_its_folder(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    root = machine.home / ".hermes"
    root.mkdir()

    assert expand("{hermes}/config.yaml", machine) == f"{root}/config.yaml"
    (root / "active_profile").write_text("work\n", encoding="utf-8")
    assert expand("{hermes}", machine) == str(root / "profiles" / "work")
    (root / "active_profile").write_text("../../etc", encoding="utf-8")
    assert expand("{hermes}", machine) == str(root)


def test_only_installed_apps_are_offered(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    present = _app(
        "present", where="~/.present/mcp.json", markers=(Where.everywhere("~/.present"),)
    )
    absent = _app("absent", where="~/.absent/mcp.json", markers=(Where.everywhere("~/.absent"),))
    (machine.home / ".present").mkdir()

    found = targets(machine, (present, absent), ())

    assert [target.app.key for target in found] == ["present"]
    assert {target.app.key for target in targets(machine, (present, absent), (), every=True)} == {
        "present",
        "absent",
    }


def test_an_app_with_its_own_command_is_reached_through_it(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    app = _app(command=OwnCommand("demo", ("mcp", "add")), programs=("demo",))
    _program(machine, "demo")

    (target,) = targets(machine, (app,), ())

    assert target.reach is Reach.COMMAND
    assert target.program is not None and Path(target.program).stem == "demo"


def test_apps_sharing_one_file_are_offered_once(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    (machine.home / ".demo").mkdir()
    first = _app("codex")
    second = _app("chatgpt")

    (target,) = targets(machine, (first, second), ())

    assert target.label == "Codex, Chatgpt"


def _editor(machine: Machine, name: str, *installed: str) -> Editor:
    root = machine.home / f".{name}" / "extensions"
    for folder in installed:
        (root / folder).mkdir(parents=True)
    user = machine.home / name / "User"
    return Editor(name, name.title(), Where.everywhere(str(user)), str(root))


def test_an_extension_is_found_in_every_editor_that_has_it(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    code = _editor(machine, "code", "acme.helper-1.2.0", "acme.helper-1.10.0", "other.thing-9.0.0")
    cursor = _editor(machine, "cursor", "Acme.Helper-2.0.0-darwin-arm64")
    bare = _editor(machine, "bare")
    per_editor = _app(
        "helper", where="{user}/globalStorage/acme.helper/mcp.json", extension="acme.helper"
    )

    found = targets(machine, (per_editor,), (code, cursor, bare))

    assert [target.label for target in found] == ["Helper in Code", "Helper in Cursor"]
    assert [target.version for target in found] == ["1.10.0", "2.0.0"]
    assert found[0].settings == machine.home / "code/User/globalStorage/acme.helper/mcp.json"


def test_an_extension_version_picks_the_file_it_reads(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    old = _editor(machine, "code", "acme.helper-4.1.24")
    new = _editor(machine, "cursor", "acme.helper-4.1.25")
    home_file = _app("helper", where="~/.helper/mcp.json", extension="acme.helper", since="4.0.0")
    legacy = _app(
        "helper-legacy",
        where="{user}/globalStorage/acme.helper/mcp.json",
        extension="acme.helper",
        since="0",
        before="4.1.25",
    )

    found = targets(machine, (home_file, legacy), (old, new))

    assert [target.app.key for target in found] == ["helper", "helper-legacy"]
    assert found[1].label == "Helper-Legacy in Code"


def test_a_tool_found_without_any_editor_extension_still_counts(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    (machine.home / ".demo").mkdir()
    shared = _app("cline", extension="acme.cline")

    (target,) = targets(machine, (shared,), ())

    assert target.app.key == "cline"


def test_an_editor_older_than_a_cutoff_gets_the_old_file(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    package = machine.home / "app" / "package.json"
    package.parent.mkdir()
    (machine.home / ".demo").mkdir()
    old = _app(
        "old-file",
        version_files=(Where.everywhere(str(package)),),
        since="0",
        before="1.140",
    )

    assert targets(machine, (old,), ()) == ()
    package.write_text('{"version": "1.139.2"}', encoding="utf-8")
    assert [target.app.key for target in targets(machine, (old,), ())] == ["old-file"]
    package.write_text('{"version": "1.140.0"}', encoding="utf-8")
    assert targets(machine, (old,), ()) == ()


def _target(tmp_path: Path, app: AiApp | None = None) -> Target:
    machine = _machine(tmp_path)
    (machine.home / ".demo").mkdir(exist_ok=True)
    (found,) = targets(machine, (app or _app(),), ())
    return found


def test_connecting_writes_the_entry_once_and_keeps_a_backup(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    target.settings.write_text('{"mcpServers": {"other": {"command": "x"}}}\n', encoding="utf-8")

    first = connect(target, ENTRY)
    second = connect(target, ENTRY)

    written = json.loads(target.settings.read_text(encoding="utf-8"))
    assert written["mcpServers"]["Stage"] == {
        "command": ENTRY.command,
        "args": ["mcp"],
        "env": {"STAGE_DB": "/data/stage.db"},
    }
    assert written["mcpServers"]["other"] == {"command": "x"}
    assert first.changed and first.backup == target.settings.with_name(f"mcp.json{BACKUP_SUFFIX}")
    assert not second.changed and second.message == "already connected"
    assert read_status(target, ENTRY).status is Status.CONNECTED


def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    target = _target(tmp_path)

    outcome = connect(target, ENTRY, dry_run=True)

    assert not outcome.changed and "would create" in outcome.message
    assert target.settings is not None and not target.settings.exists()


@pytest.mark.parametrize(
    ("stored", "status"),
    [
        (None, Status.ABSENT),
        ({"command": "/opt/stage/bin/stage", "args": ["mcp"], "env": {}}, Status.OUTDATED),
        ({"command": "/gone/stage", "args": ["mcp"]}, Status.BROKEN),
    ],
)
def test_status_tells_a_stale_entry_from_a_missing_one(
    tmp_path: Path, stored: dict[str, Any] | None, status: Status
) -> None:
    target = _target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    servers = {"stage": stored} if stored else {}
    target.settings.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    assert read_status(target, ENTRY).status is status


def test_a_file_stage_cannot_edit_gets_instructions_instead(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    target.settings.write_text('{\n  // mine\n  "mcpServers": {}\n}\n', encoding="utf-8")

    outcome = connect(target, ENTRY)

    assert not outcome.changed and outcome.status is Status.UNREADABLE
    assert "comments" in outcome.message
    assert '"Stage"' in outcome.instructions
    assert "// mine" in target.settings.read_text(encoding="utf-8")
    assert read_status(target, ENTRY).status is Status.ABSENT


def test_every_shape_and_format_reads_back_as_connected(tmp_path: Path) -> None:
    cases = [
        (ConfigFormat.JSON, ("mcp",), Shape.OPENCODE),
        (ConfigFormat.JSON, ("amp.mcpServers",), Shape.PLAIN),
        (ConfigFormat.TOML, ("mcp_servers",), Shape.PLAIN),
        (ConfigFormat.YAML, ("extensions",), Shape.GOOSE),
        (ConfigFormat.YAML, ("mcp_servers",), Shape.ENABLED),
    ]
    for index, (kind, key, shape) in enumerate(cases):
        app = _app(
            f"demo{index}", where=f"~/.demo/{index}.cfg", kind=kind, key_path=key, shape=shape
        )
        target = _target(tmp_path, app)
        assert connect(target, ENTRY).changed
        assert read_status(target, ENTRY).status is Status.CONNECTED
        assert disconnect(target, ENTRY).changed
        assert read_status(target, ENTRY).status is Status.ABSENT


def test_an_app_that_rewrites_its_settings_must_be_closed_first(tmp_path: Path) -> None:
    app = _app(process="Demo", closed_first=True)
    target = _target(tmp_path, app)

    refused = connect(target, ENTRY, running=lambda name: name == "Demo")
    allowed = connect(target, ENTRY, running=lambda _: False)

    assert not refused.changed and "Quit Demo first" in refused.message
    assert allowed.changed


def test_a_program_path_with_a_space_is_refused_where_it_cannot_run(tmp_path: Path) -> None:
    target = _target(tmp_path, _app(spaceless=True))
    spaced = ServerEntry("/Users/Ann Lee/.local/bin/stage", ("mcp",))

    outcome = connect(target, spaced)

    assert not outcome.changed and "space" in outcome.message


def test_a_held_lock_is_waited_for_and_a_stale_one_is_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import connect as engine

    app = AiApp(
        "locked",
        "Locked",
        file=SettingsFile(
            (Where.everywhere("~/.demo/mcp.json"),), ConfigFormat.JSON, SERVERS, locked=True
        ),
        markers=(Where.everywhere("~/.demo"),),
    )
    target = _target(tmp_path, app)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    lock = target.settings.with_name("mcp.json.lock")
    lock.mkdir()
    monkeypatch.setattr(engine, "LOCK_WAIT_SECONDS", 0.3)

    held = connect(target, ENTRY)
    os.utime(lock, (0, 0))
    cleared = connect(target, ENTRY)

    assert not held.changed and "being saved" in held.message
    assert cleared.changed and not lock.exists()


def _command_app(**fields: Any) -> AiApp:
    return _app(
        "tool",
        command=OwnCommand(
            "tool",
            ("mcp", "add", "{name}", "{env}", "--", "{command}", "{args}"),
            ("mcp", "remove", "{name}"),
            "--env",
        ),
        programs=("tool",),
        **fields,
    )


def _command_target(tmp_path: Path, **fields: Any) -> Target:
    machine = _machine(tmp_path)
    _program(machine, "tool")
    (machine.home / ".demo").mkdir(exist_ok=True)
    (found,) = targets(machine, (_command_app(**fields),), ())
    return found


def test_an_apps_own_command_gets_every_setting_after_a_separator(tmp_path: Path) -> None:
    target = _command_target(tmp_path)
    runner = Recorder()

    outcome = connect(target, ENTRY, runner=runner)

    assert outcome.changed
    (call,) = runner.calls
    assert call[1:] == [
        "mcp",
        "add",
        "Stage",
        "--env",
        "STAGE_DB=/data/stage.db",
        "--",
        ENTRY.command,
        "mcp",
    ]


def test_an_outdated_entry_is_removed_before_it_is_added_again(tmp_path: Path) -> None:
    target = _command_target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    old = {"mcpServers": {"stage": {"command": "/old/stage", "args": ["mcp"]}}}
    target.settings.write_text(json.dumps(old), encoding="utf-8")
    runner = Recorder()

    connect(target, ENTRY, runner=runner)

    assert [call[1:3] for call in runner.calls] == [["mcp", "remove"], ["mcp", "add"]]


def test_a_refused_command_falls_back_to_the_file_when_that_is_safe(tmp_path: Path) -> None:
    target = _command_target(tmp_path)

    outcome = connect(target, ENTRY, runner=Recorder(1, "not logged in"))

    assert outcome.changed and "added Stage to" in outcome.message
    assert read_status(target, ENTRY).status is Status.CONNECTED


def test_a_file_its_app_keeps_rewriting_is_never_edited_directly(tmp_path: Path) -> None:
    target = _command_target(tmp_path, edits_file=False)

    outcome = connect(target, ENTRY, runner=Recorder(1, "not logged in"))

    assert not outcome.changed and "not logged in" in outcome.message
    assert "Run:" in outcome.instructions
    assert target.settings is not None and not target.settings.exists()


def test_a_command_with_no_way_to_pass_settings_is_not_used(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    _program(machine, "tool")
    (machine.home / ".demo").mkdir()
    app = _app(
        command=OwnCommand("tool", ("mcp", "add", "{name}", "--", "{command}")), programs=("tool",)
    )
    (target,) = targets(machine, (app,), ())
    runner = Recorder()

    outcome = connect(target, ENTRY, runner=runner)

    assert not runner.calls
    assert outcome.changed and read_status(target, ENTRY).status is Status.CONNECTED


def test_disconnecting_runs_the_apps_own_removal(tmp_path: Path) -> None:
    target = _command_target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    stored = {"mcpServers": {"stage": {"command": ENTRY.command, "args": ["mcp"]}}}
    target.settings.write_text(json.dumps(stored), encoding="utf-8")
    runner = Recorder()

    outcome = disconnect(target, ENTRY, runner=runner)

    assert outcome.changed and runner.calls[0][1:] == ["mcp", "remove", "stage"]


def test_disconnecting_what_was_never_connected_does_nothing(tmp_path: Path) -> None:
    target = _target(tmp_path)

    outcome = disconnect(target, ENTRY)

    assert not outcome.changed and outcome.message == "was not connected"
    assert target.settings is not None and not target.settings.exists()


def test_an_app_without_a_file_gets_instructions_with_the_real_values(tmp_path: Path) -> None:
    app = AiApp(
        "screens",
        "Screens",
        markers=(Where.everywhere("~/.demo"),),
        screens="Open Settings, then paste:\n{json}",
    )
    target = _target(tmp_path, app)

    outcome = connect(target, ENTRY)

    assert not outcome.changed
    pasted = json.loads(outcome.instructions.split("paste:\n", 1)[1])
    assert pasted["mcpServers"]["Stage"]["command"] == ENTRY.command


def test_install_links_carry_the_entry_safely() -> None:
    entry = ServerEntry("/a b/stage+x", ("mcp",), {"STAGE_DB": "/d"})
    for style in LinkStyle:
        link = install_link(style, "stage", entry.command, entry.args, entry.env)
        query = parse_qs(urlsplit(link).query)
        (encoded,) = next(iter(query.values()))
        decoded = json.loads(base64.b64decode(encoded))
        server = decoded.get("mcpServers", {}).get("stage", decoded)
        assert server["command"] == "/a b/stage+x"
        assert " " not in link


def test_instructions_name_the_file_and_the_command(tmp_path: Path) -> None:
    target = _command_target(tmp_path)

    text = instructions(target, ENTRY)

    assert "Run:" in text and "--env STAGE_DB=/data/stage.db" in text
    assert str(target.settings) in text


def test_toml_entries_survive_windows_paths(tmp_path: Path) -> None:
    app = _app(
        "codex", where="~/.demo/config.toml", kind=ConfigFormat.TOML, key_path=("mcp_servers",)
    )
    target = _target(tmp_path, app)
    windows = ServerEntry(
        "C:\\Users\\Ann Lee\\.local\\bin\\stage.exe", ("mcp",), {"STAGE_DB": "C:\\d\\s.db"}
    )

    assert connect(target, windows).changed
    assert target.settings is not None
    stored = tomllib.loads(target.settings.read_text(encoding="utf-8"))["mcp_servers"]["Stage"]
    assert stored["command"] == windows.command


@pytest.mark.skipif(sys.platform == "win32", reason="checks the POSIX program lookup")
def test_a_program_that_is_not_executable_is_not_used(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    (Path(machine.environ["PATH"]) / "tool").write_text("", encoding="utf-8")
    (machine.home / ".demo").mkdir()

    (target,) = targets(machine, (_command_app(),), ())

    assert target.reach is Reach.FILE


def test_a_flag_shaped_argument_never_reaches_a_command_without_a_separator(
    tmp_path: Path,
) -> None:
    machine = _machine(tmp_path)
    _program(machine, "tool")
    (machine.home / ".demo").mkdir()
    app = _app(
        command=OwnCommand(
            "tool", ("mcp", "add", "{env}", "{name}", "{command}", "{args}"), (), "-e"
        ),
        programs=("tool",),
    )
    (target,) = targets(machine, (app,), ())
    runner = Recorder()
    fallback = ServerEntry(sys.executable, ("-m", "stage", "mcp"))

    outcome = connect(target, fallback, runner=runner)

    assert not runner.calls
    assert outcome.changed and read_status(target, fallback).status is Status.CONNECTED


def test_a_locked_file_in_a_folder_that_does_not_exist_yet_is_created(tmp_path: Path) -> None:
    app = AiApp(
        "locked",
        "Locked",
        file=SettingsFile(
            (Where.everywhere("~/.demo/data/settings/mcp.json"),),
            ConfigFormat.JSON,
            SERVERS,
            locked=True,
        ),
        markers=(Where.everywhere("~/.demo"),),
    )
    target = _target(tmp_path, app)

    outcome = connect(target, ENTRY)

    assert outcome.changed
    assert target.settings is not None and target.settings.exists()
    assert not target.settings.with_name("mcp.json.lock").exists()


def test_without_its_command_an_app_that_rewrites_its_file_only_gets_steps(
    tmp_path: Path,
) -> None:
    app = _app(
        command=OwnCommand("absent-tool", ("mcp", "add-json", "{name}", "{entry}")),
        edits_file=False,
    )
    target = _target(tmp_path, app)

    outcome = connect(target, ENTRY)

    assert target.reach is Reach.SCREENS
    assert not outcome.changed and "Run:" in outcome.instructions
    assert "could not find the absent-tool command" in outcome.message
    assert target.settings is not None and not target.settings.exists()
    assert str(target.settings) not in outcome.instructions


def test_a_command_that_reports_success_but_writes_nothing_is_not_believed(
    tmp_path: Path,
) -> None:
    trusted = _command_target(tmp_path)
    (tmp_path / "second").mkdir()
    untrusted = _command_target(tmp_path / "second", edits_file=False)

    fallback = connect(trusted, ENTRY, runner=Recorder(0, "Invalid env format"))
    refused = connect(untrusted, ENTRY, runner=Recorder(0, "Invalid env format"))

    assert fallback.changed and read_status(trusted, ENTRY).status is Status.CONNECTED
    assert not refused.changed
    assert "said it worked, but Stage is not in its settings" in refused.message
    assert "Invalid env format" in refused.message


@pytest.mark.skipif(sys.platform == "win32", reason="checks the POSIX executable bit")
def test_a_command_bundled_in_an_editor_extension_is_found_newest_first(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    (machine.home / ".demo").mkdir()
    editor = _editor(machine, "code")
    root = Path(editor.extensions)
    for version, runnable in (("2.1.9", True), ("2.1.10", True), ("2.2.0", False)):
        binary = root / f"acme.tool-{version}-darwin-arm64" / "bin" / "tool"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755 if runnable else 0o644)
    app = _app(
        command=OwnCommand("tool", ("mcp", "add", "{name}", "{entry}")),
        bundled=("acme.tool-*/bin/tool",),
    )

    (target,) = targets(machine, (app,), (editor,))

    assert target.reach is Reach.COMMAND
    assert target.program is not None and "acme.tool-2.1.10-" in target.program


class Writer(Recorder):
    def __init__(self, target: Path, server: dict[str, Any]) -> None:
        super().__init__()
        self.target = target
        self.server = server

    def __call__(self, command: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.target.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            json.loads(self.target.read_text(encoding="utf-8")) if self.target.exists() else {}
        )
        servers = {**existing.get("mcpServers", {}), "Stage": self.server}
        self.target.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return super().__call__(command)


def test_an_entry_the_app_wrote_to_another_of_its_files_is_found_not_duplicated(
    tmp_path: Path,
) -> None:
    machine = _machine(tmp_path)
    _program(machine, "tool")
    (machine.home / ".demo").mkdir()
    app = AiApp(
        "tool",
        "Tool",
        file=SettingsFile(
            (Where.everywhere("~/.demo/first.json"), Where.everywhere("~/.demo/second.jsonc")),
            ConfigFormat.JSON,
            SERVERS,
        ),
        command=OwnCommand("tool", ("mcp", "add", "{name}", "{entry}")),
        programs=("tool",),
        markers=(Where.everywhere("~/.demo"),),
    )
    (target,) = targets(machine, (app,), ())
    second = machine.home / ".demo" / "second.jsonc"
    server = {"command": ENTRY.command, "args": ["mcp"], "env": dict(ENTRY.env)}

    outcome = connect(target, ENTRY, runner=Writer(second, server))

    assert outcome.changed and outcome.message.startswith("ran ")
    assert not (machine.home / ".demo" / "first.json").exists()
    assert read_status(target, ENTRY).status is Status.CONNECTED
    removed = disconnect(target, ENTRY)
    assert removed.changed and str(second) in removed.message
    assert json.loads(second.read_text(encoding="utf-8")) == {"mcpServers": {}}


OLD = {"command": ENTRY.command, "args": ["mcp"], "env": dict(ENTRY.env)}


def test_an_entry_under_the_old_lowercase_name_is_renamed_in_place(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    stored = {"mcpServers": {"other": {"command": "x"}, "stage": OLD}}
    target.settings.write_text(json.dumps(stored), encoding="utf-8")

    reading = read_status(target, ENTRY)
    outcome = connect(target, ENTRY)

    assert reading.status is Status.OUTDATED and "old name stage" in reading.detail
    assert outcome.changed
    servers = json.loads(target.settings.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"other", "Stage"}
    assert read_status(target, ENTRY).status is Status.CONNECTED


def test_an_apps_own_command_removes_the_old_name_before_adding_the_new(tmp_path: Path) -> None:
    target = _command_target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    target.settings.write_text(json.dumps({"mcpServers": {"stage": OLD}}), encoding="utf-8")
    runner = Recorder()

    connect(target, ENTRY, runner=runner)

    assert runner.calls[0][1:] == ["mcp", "remove", "stage"]
    assert runner.calls[1][1:4] == ["mcp", "add", "Stage"]


def test_an_old_entry_is_cleared_when_the_apps_command_cannot_remove_it(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    _program(machine, "tool")
    (machine.home / ".demo").mkdir()
    app = _app(command=OwnCommand("tool", ("--add", "{json}")), programs=("tool",))
    (target,) = targets(machine, (app,), ())
    assert target.settings is not None
    new = {"command": ENTRY.command, "args": ["mcp"], "env": dict(ENTRY.env)}
    target.settings.write_text(json.dumps({"mcpServers": {"stage": OLD}}), encoding="utf-8")

    connect(target, ENTRY, runner=Writer(target.settings, new))

    servers = json.loads(target.settings.read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == ["Stage"]


def test_disconnecting_removes_the_old_name_and_the_new(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert target.settings is not None
    target.settings.parent.mkdir(parents=True, exist_ok=True)
    both = {"mcpServers": {"stage": OLD, "Stage": OLD, "other": {"command": "x"}}}
    target.settings.write_text(json.dumps(both), encoding="utf-8")

    assert disconnect(target, ENTRY).changed
    assert json.loads(target.settings.read_text(encoding="utf-8")) == {
        "mcpServers": {"other": {"command": "x"}}
    }
