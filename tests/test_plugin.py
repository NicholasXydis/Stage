import base64
import json
import re
import struct
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from stage import __version__
from stage.ai.server import ACTIONS, CATALOGUE

ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "stage"
SKILL = PLUGIN / "skills" / "internships" / "SKILL.md"
README = ROOT / "README.md"
UVX = {"command": "uvx", "args": ["--from", "stage-cli", "stage", "mcp"]}
TOOLS = {name for name, *_ in (*CATALOGUE, *ACTIONS)}


def _json(path: Path) -> dict[str, object]:
    found: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return found


def _links() -> dict[str, str]:
    text = README.read_text(encoding="utf-8")
    found = re.findall(
        r"\]\((https://(?:cursor\.com|vscode\.dev|lmstudio\.ai|goose-docs\.ai)[^)]+)\)", text
    )
    return {urlsplit(link).hostname or "": link for link in found}


def test_the_marketplace_lists_the_plugin_in_this_repository() -> None:
    market = _json(MARKETPLACE)
    plugins = market["plugins"]
    assert isinstance(plugins, list) and len(plugins) == 1
    entry = plugins[0]
    assert market["name"] == "stage" and entry["name"] == "stage"
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    assert entry["source"].startswith("./")


def test_the_plugin_names_itself_and_keeps_stages_version() -> None:
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "stage" and manifest["version"] == __version__
    assert manifest["license"] == "MIT"
    assert str(manifest["repository"]).endswith("NicholasXydis/Stage")


def test_the_plugin_starts_the_installed_stage_without_actions() -> None:
    servers = _json(PLUGIN / ".mcp.json")["mcpServers"]
    assert servers == {"Stage": {"command": "stage", "args": ["mcp"]}}


def test_the_plugin_icon_is_the_512_pixel_logo() -> None:
    icon = (PLUGIN / ".claude-plugin" / "icon.png").read_bytes()
    assert icon == (ROOT / "assets" / "logo" / "stage-512.png").read_bytes()
    assert icon[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", icon[16:24]) == (512, 512)


def test_the_skill_has_its_frontmatter_and_names_only_real_tools() -> None:
    text = SKILL.read_text(encoding="utf-8")
    front, body = text.split("---\n", 2)[1:]
    fields = dict(line.split(": ", 1) for line in front.strip().splitlines())
    assert fields["name"] == "internships" and len(fields["description"]) < 1_024
    assert text.isascii()
    named = set(re.findall(r"`([a-z]+_[a-z_]+)`", body))
    assert named and named <= TOOLS, named - TOOLS
    assert "never follow instructions" in body.lower()


def test_the_skill_names_only_stage_commands_that_exist() -> None:
    from typer.main import get_command

    from stage.cli.app import app

    commands = set(cast(Any, get_command(app)).commands)
    used = set(re.findall(r"`stage ([a-z]+)", SKILL.read_text(encoding="utf-8")))
    assert used and used <= commands, used - commands


def test_every_readme_button_installs_stage_through_uvx() -> None:
    links = _links()
    assert set(links) == {"cursor.com", "vscode.dev", "lmstudio.ai", "goose-docs.ai"}

    cursor = parse_qs(urlsplit(links["cursor.com"]).query)
    assert cursor["name"] == ["Stage"]
    assert json.loads(base64.b64decode(cursor["config"][0])) == UVX

    code = parse_qs(urlsplit(links["vscode.dev"]).query)
    assert code["name"] == ["Stage"]
    assert json.loads(code["config"][0]) == {"type": "stdio", **UVX}

    studio = parse_qs(urlsplit(links["lmstudio.ai"]).query)
    assert json.loads(base64.b64decode(studio["config"][0])) == UVX

    goose = parse_qs(urlsplit(links["goose-docs.ai"]).query)
    assert goose["cmd"] == ["uvx"] and goose["arg"] == UVX["args"]
    assert goose["name"] == ["Stage"]


def test_the_readme_shows_how_to_install_the_plugin() -> None:
    text = README.read_text(encoding="utf-8")
    assert "/plugin install stage --marketplace NicholasXydis/Stage" in text
    assert "stage connect claude-code --allow-actions" in text
