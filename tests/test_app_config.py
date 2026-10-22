import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from stage.domain.ai_apps import ConfigFormat
from stage.services.app_config import (
    BACKUP_SUFFIX,
    MAX_CONFIG_BYTES,
    UnwritableError,
    entry_in,
    has_entry,
    read_config,
    with_entry,
    without_entry,
    write_config,
)

ENTRY: dict[str, Any] = {
    "command": "/Users/Nicolás X/.local/share/uv/tools/stage-cli/bin/python",
    "args": ["-m", "stage", "mcp"],
    "env": {"STAGE_DB": "/Users/Nicolás X/stage.db"},
}
WINDOWS_ENTRY: dict[str, Any] = {
    "command": "C:\\Users\\Nicolás X\\AppData\\Roaming\\uv\\tools\\stage-cli\\Scripts\\python.exe",
    "args": ["-m", "stage", "mcp"],
    "env": {"STAGE_DB": "C:\\Users\\Nicolás X\\stage.db"},
}
OTHER = {"command": "npx", "args": ["-y", "@acme/server"], "env": {"ACME_KEY": "secret-123"}}
JSON_KEY = ("mcpServers",)
SERVERS = ("mcp_servers",)


def _json(text: str) -> Any:
    return json.loads(text)


def test_json_starts_a_missing_file_with_only_stage() -> None:
    edited = with_entry("", ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)

    assert _json(edited) == {"mcpServers": {"stage": ENTRY}}
    assert edited.endswith("}\n")
    assert "Nicolás" in edited


def test_json_keeps_every_other_server_and_setting_in_place() -> None:
    original = json.dumps(
        {"theme": "dark", "mcpServers": {"acme": OTHER, "stage": {"command": "old"}}, "z": 1},
        indent=4,
    )
    edited = with_entry(original, ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)

    loaded = _json(edited)
    assert list(loaded) == ["theme", "mcpServers", "z"]
    assert list(loaded["mcpServers"]) == ["acme", "stage"]
    assert loaded["mcpServers"]["acme"] == OTHER
    assert loaded["mcpServers"]["stage"] == ENTRY
    assert '\n    "theme"' in edited


def test_json_keeps_a_tab_indent() -> None:
    original = '{\n\t"mcpServers": {}\n}\n'

    assert '\n\t"mcpServers"' in with_entry(original, ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{\n  // my servers\n  "mcpServers": {}\n}', "comments"),
        ('{"mcpServers": {"a": {"command": "x"},}}', "trailing commas"),
        ('{"mcpServers": {', "not valid JSON"),
        ('{"mcpServers": {}, "mcpServers": {}}', "twice"),
        ("[1, 2]", "at the top"),
        ('{"mcpServers": []}', "something other"),
        ('{"mcpServers": "none"}', "something other"),
    ],
)
def test_json_refuses_a_file_it_cannot_edit_safely(text: str, reason: str) -> None:
    with pytest.raises(UnwritableError, match=reason):
        with_entry(text, ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)


def test_a_link_inside_a_broken_file_is_not_mistaken_for_a_comment() -> None:
    with pytest.raises(UnwritableError, match="not valid JSON"):
        with_entry('{"url": "https://x.test/a", ', ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)


def test_json_reaches_a_nested_or_dotted_key() -> None:
    edited = with_entry('{"amp.mcpServers": {}}', ConfigFormat.JSON, ("amp.mcpServers",), "s", {})
    nested = with_entry("{}", ConfigFormat.JSON, ("mcp", "servers"), "stage", ENTRY)

    assert _json(edited) == {"amp.mcpServers": {"s": {}}}
    assert _json(nested) == {"mcp": {"servers": {"stage": ENTRY}}}


def test_json_removal_takes_only_stage_and_leaves_an_absent_entry_alone() -> None:
    original = json.dumps({"mcpServers": {"acme": OTHER, "stage": ENTRY}}, indent=2)
    removed = without_entry(original, ConfigFormat.JSON, JSON_KEY, "stage")

    assert _json(removed) == {"mcpServers": {"acme": OTHER}}
    assert without_entry(removed, ConfigFormat.JSON, JSON_KEY, "stage") == removed
    assert without_entry("", ConfigFormat.JSON, JSON_KEY, "stage") == ""


def test_reading_an_entry_back() -> None:
    text = with_entry("", ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)

    assert entry_in(text, ConfigFormat.JSON, JSON_KEY, "stage") == ENTRY
    assert entry_in(text, ConfigFormat.JSON, JSON_KEY, "acme") is None
    assert entry_in("", ConfigFormat.JSON, JSON_KEY, "stage") is None
    assert has_entry(text, ConfigFormat.JSON, JSON_KEY, "stage")
    assert not has_entry("{}", ConfigFormat.JSON, JSON_KEY, "stage")


CODEX = """# my codex settings
model = "o4"

[mcp_servers.acme]
command = "npx"
args = ["-y", "@acme/server"]

[mcp_servers.acme.env]
ACME_KEY = "secret-123"

# profiles below
[profiles.fast]
model = "o4-mini"
"""


@pytest.mark.parametrize("entry", [ENTRY, WINDOWS_ENTRY])
def test_toml_adds_a_table_and_keeps_every_line_already_there(entry: dict[str, Any]) -> None:
    edited = with_entry(CODEX, ConfigFormat.TOML, SERVERS, "stage", entry)

    assert edited.startswith(CODEX.rstrip("\n"))
    assert tomllib.loads(edited)["mcp_servers"]["stage"] == entry
    assert tomllib.loads(edited)["mcp_servers"]["acme"]["env"] == {"ACME_KEY": "secret-123"}


def test_toml_replaces_stage_without_touching_its_neighbours() -> None:
    first = with_entry(CODEX, ConfigFormat.TOML, SERVERS, "stage", {"command": "old", "args": []})
    old = '[mcp_servers.stage]\ncommand = "old"\n\n[mcp_servers.stage.env]\nX = "1"\n\n'
    second = with_entry(
        CODEX.replace("# profiles below", old + "# profiles below"),
        ConfigFormat.TOML,
        SERVERS,
        "stage",
        ENTRY,
    )

    assert tomllib.loads(first)["mcp_servers"]["stage"] == {"command": "old", "args": []}
    assert tomllib.loads(second)["mcp_servers"]["stage"] == ENTRY
    assert "# profiles below\n[profiles.fast]" in second
    assert second.count("[mcp_servers.stage]") == 1


def test_toml_finds_a_quoted_header() -> None:
    text = '["mcp_servers"."stage"]\ncommand = "old"\n'
    edited = with_entry(text, ConfigFormat.TOML, SERVERS, "stage", ENTRY)

    assert tomllib.loads(edited)["mcp_servers"]["stage"] == ENTRY
    assert "old" not in edited


@pytest.mark.parametrize(
    "text",
    [
        'mcp_servers = { acme = { command = "npx" } }\n',
        'mcp_servers.stage.command = "old"\n',
        "[mcp_servers\n",
    ],
)
def test_toml_refuses_what_a_table_cannot_be_added_to(text: str) -> None:
    with pytest.raises(UnwritableError):
        with_entry(text, ConfigFormat.TOML, SERVERS, "stage", ENTRY)


def test_toml_removal_keeps_the_comment_above_the_next_table() -> None:
    added = with_entry(CODEX, ConfigFormat.TOML, SERVERS, "stage", ENTRY)
    placed = added.replace("# profiles below\n[profiles.fast]", "").rstrip("\n")
    moved = placed + "\n\n# profiles below\n[profiles.fast]\nmodel = 'o4-mini'\n"
    removed = without_entry(moved, ConfigFormat.TOML, SERVERS, "stage")

    assert "stage" not in tomllib.loads(removed)["mcp_servers"]
    assert "# profiles below\n[profiles.fast]" in removed
    assert removed.startswith("# my codex settings")


HERMES = """# Hermes
model:
  provider: openrouter
mcp_servers:
    acme:
        command: npx
        args: ["-y", "@acme/server"]
    # keep this comment
display:
  theme: dark
"""


@pytest.mark.parametrize("entry", [ENTRY, WINDOWS_ENTRY])
def test_yaml_adds_one_line_under_the_section_and_keeps_the_rest(entry: dict[str, Any]) -> None:
    edited = with_entry(HERMES, ConfigFormat.YAML, SERVERS, "stage", entry)
    loaded = yaml.safe_load(edited)

    assert loaded["mcp_servers"]["stage"] == entry
    assert loaded["mcp_servers"]["acme"]["command"] == "npx"
    assert loaded["display"] == {"theme": "dark"}
    assert "    # keep this comment" in edited
    assert "\n    stage: {" in edited


def test_yaml_replaces_a_multi_line_stage_entry() -> None:
    text = HERMES.replace(
        "    # keep this comment",
        "    stage:\n        command: old\n        args:\n          - x\n    # keep this comment",
    )
    edited = with_entry(text, ConfigFormat.YAML, SERVERS, "stage", ENTRY)

    assert yaml.safe_load(edited)["mcp_servers"]["stage"] == ENTRY
    assert "old" not in edited
    assert "    # keep this comment" in edited


def test_yaml_starts_a_section_when_there_is_none() -> None:
    edited = with_entry("model: x\n", ConfigFormat.YAML, SERVERS, "stage", ENTRY)
    empty = with_entry("", ConfigFormat.YAML, SERVERS, "stage", ENTRY)
    braces = with_entry("mcp_servers: {}\nmodel: x\n", ConfigFormat.YAML, SERVERS, "stage", ENTRY)

    for text in (edited, empty, braces):
        assert yaml.safe_load(text)["mcp_servers"] == {"stage": ENTRY}


@pytest.mark.parametrize(
    "text",
    [
        "mcp_servers: {acme: {command: npx}}\n",
        "mcp_servers:\n- acme\n",
        "mcp_servers:\n  acme: {command: npx}\nmcp_servers:\n  other: {}\n",
        "- just\n- a list\n",
        "mcp_servers: [\n",
    ],
)
def test_yaml_refuses_what_it_cannot_edit_line_by_line(text: str) -> None:
    with pytest.raises(UnwritableError):
        with_entry(text, ConfigFormat.YAML, SERVERS, "stage", ENTRY)


def test_yaml_only_edits_a_top_level_section() -> None:
    with pytest.raises(UnwritableError, match="does not edit"):
        with_entry("", ConfigFormat.YAML, ("a", "b"), "stage", ENTRY)


def test_yaml_removal_leaves_an_empty_section_that_still_reads_as_one() -> None:
    added = with_entry("model: x\n", ConfigFormat.YAML, SERVERS, "stage", ENTRY)
    removed = without_entry(added, ConfigFormat.YAML, SERVERS, "stage")
    kept = without_entry(
        with_entry(HERMES, ConfigFormat.YAML, SERVERS, "stage", ENTRY),
        ConfigFormat.YAML,
        SERVERS,
        "stage",
    )

    assert yaml.safe_load(removed) == {"model": "x", "mcp_servers": {}}
    assert kept == HERMES


def test_a_missing_file_is_written_owner_only_without_a_backup(tmp_path: Path) -> None:
    target = tmp_path / "app" / "config.json"
    current = read_config(target)

    assert not current.exists
    assert write_config(current, "{}\n") is None
    assert target.read_text(encoding="utf-8") == "{}\n"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_change_keeps_one_backup_with_the_original_bytes_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_bytes(b'{"a": 1}\n')
    target.chmod(0o640)

    backup = write_config(read_config(target), '{"a": 2}\n')

    assert backup == tmp_path / f"config.json{BACKUP_SUFFIX}"
    assert backup.read_bytes() == b'{"a": 1}\n'
    assert target.read_bytes() == b'{"a": 2}\n'
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        assert stat.S_IMODE(backup.stat().st_mode) == 0o640


def test_an_unchanged_file_is_not_touched(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_bytes(b"{}\n")
    before = target.stat().st_mtime_ns

    assert write_config(read_config(target), "{}\n") is None
    assert target.stat().st_mtime_ns == before
    assert not (tmp_path / f"config.json{BACKUP_SUFFIX}").exists()


def test_windows_line_endings_and_a_byte_order_mark_survive(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_bytes(b'\xef\xbb\xbf{\r\n  "mcpServers": {}\r\n}\r\n')
    current = read_config(target)

    write_config(current, with_entry(current.text, ConfigFormat.JSON, JSON_KEY, "stage", ENTRY))

    data = target.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf{\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert json.loads(data.decode("utf-8-sig"))["mcpServers"]["stage"] == ENTRY


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need extra rights on Windows")
def test_a_linked_file_is_written_through_the_link(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "config.json"
    real.parent.mkdir()
    real.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(real)

    write_config(read_config(link), '{"a": 1}\n')

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert (real.parent / f"config.json{BACKUP_SUFFIX}").exists()


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need extra rights on Windows")
def test_a_link_to_nothing_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "config.json"
    link.symlink_to(tmp_path / "gone.json")

    with pytest.raises(UnwritableError, match="does not exist"):
        read_config(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_read_only_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o444)

    with pytest.raises(UnwritableError, match="read-only"):
        write_config(read_config(target), '{"a": 1}\n')
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_a_file_changed_by_its_app_meanwhile_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{}\n", encoding="utf-8")
    current = read_config(target)
    target.write_text('{"theirs": true}\n', encoding="utf-8")

    with pytest.raises(UnwritableError, match="changed while"):
        write_config(current, '{"ours": true}\n')
    assert target.read_text(encoding="utf-8") == '{"theirs": true}\n'


def test_files_that_are_not_settings_are_refused(tmp_path: Path) -> None:
    folder = tmp_path / "folder.json"
    folder.mkdir()
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00")
    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))

    for target, reason in ((folder, "folder"), (binary, "UTF-8"), (huge, "larger")):
        with pytest.raises(UnwritableError, match=reason):
            read_config(target)


def test_an_entry_is_read_back_from_a_file_with_comments_it_will_not_edit() -> None:
    text = (
        '{\n  // mine, with a link: "https://x.test/a"\n  "theme": "dark", /* inline */\n'
        '  "mcpServers": {"stage": {"command": "/a//b", "args": ["mcp",],},},\n}\n'
    )

    assert entry_in(text, ConfigFormat.JSON, JSON_KEY, "stage") == {
        "command": "/a//b",
        "args": ["mcp"],
    }
    assert has_entry(text, ConfigFormat.JSON, JSON_KEY, "stage")
    with pytest.raises(UnwritableError, match="comments"):
        with_entry(text, ConfigFormat.JSON, JSON_KEY, "stage", ENTRY)


def test_a_comment_marker_inside_a_string_is_kept() -> None:
    text = '{"mcpServers": {"stage": {"command": "C:\\\\x\\\\\\"//y", "args": ["/*", "*/"]}}}'

    assert entry_in(text, ConfigFormat.JSON, JSON_KEY, "stage") == {
        "command": 'C:\\x\\"//y',
        "args": ["/*", "*/"],
    }


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (
            '[[mcp_servers]]\nname = "other"\n\n[[mcp_servers]]\nname = "stage"\ncommand = "/s"\n',
            ConfigFormat.TOML,
        ),
        ("mcpServers:\n  - name: other\n  - name: stage\n    command: /s\n", ConfigFormat.YAML),
    ],
)
def test_an_entry_in_a_list_of_servers_is_found_by_name(text: str, kind: ConfigFormat) -> None:
    key = ("mcp_servers",) if kind is ConfigFormat.TOML else ("mcpServers",)

    found = entry_in(text, kind, key, "stage")

    assert found is not None and found["command"] == "/s"
    assert entry_in(text, kind, key, "missing") is None
