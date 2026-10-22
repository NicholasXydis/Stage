import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote, urlencode


class ConfigFormat(StrEnum):
    JSON = "json"
    TOML = "toml"
    YAML = "yaml"


class System(StrEnum):
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"


class Shape(StrEnum):
    PLAIN = "plain"
    STDIO = "stdio"
    COPILOT = "copilot"
    OPENCODE = "opencode"
    GOOSE = "goose"
    NOT_DISABLED = "not-disabled"
    STDIO_NOT_DISABLED = "stdio-not-disabled"
    ENABLED = "enabled"
    JAN = "jan"
    FIVE = "5ire"
    DIVE = "dive"


class LinkStyle(StrEnum):
    CHERRY = "cherry"
    DEEPCHAT = "deepchat"
    CHATBOX = "chatbox"


class Reach(StrEnum):
    COMMAND = "command"
    FILE = "file"
    LINK = "link"
    SCREENS = "screens"


@dataclass(frozen=True, slots=True)
class Where:
    macos: str | None = None
    windows: str | None = None
    linux: str | None = None

    def on(self, system: System) -> str | None:
        match system:
            case System.MACOS:
                return self.macos
            case System.WINDOWS:
                return self.windows
            case _:
                return self.linux

    @classmethod
    def everywhere(cls, template: str) -> "Where":
        return cls(template, template, template)


@dataclass(frozen=True, slots=True)
class SettingsFile:
    where: tuple[Where, ...]
    format: ConfigFormat
    key: tuple[str, ...]
    shape: Shape = Shape.PLAIN
    locked: bool = False


@dataclass(frozen=True, slots=True)
class OwnCommand:
    program: str
    add: tuple[str, ...]
    remove: tuple[str, ...] = ()
    env_flag: str = ""


@dataclass(frozen=True, slots=True)
class Editor:
    key: str
    name: str
    user: Where
    extensions: str
    bundles: tuple[Where, ...] = ()


@dataclass(frozen=True, slots=True)
class AiApp:
    key: str
    name: str
    file: SettingsFile | None = None
    command: OwnCommand | None = None
    link: LinkStyle | None = None
    programs: tuple[str, ...] = ()
    markers: tuple[Where, ...] = ()
    version_files: tuple[Where, ...] = ()
    bundled: tuple[str, ...] = ()
    extension: str = ""
    since: str = ""
    before: str = ""
    process: str = ""
    closed_first: bool = False
    restart: bool = False
    spaceless: bool = False
    edits_file: bool = True
    screens: str = ""
    note: str = ""
    docs: str = ""

    @property
    def reaches(self) -> tuple[Reach, ...]:
        found: list[Reach] = []
        if self.command is not None:
            found.append(Reach.COMMAND)
        if self.file is not None:
            found.append(Reach.FILE)
        if self.link is not None:
            found.append(Reach.LINK)
        found.append(Reach.SCREENS)
        return tuple(found)


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:4])


def in_range(version: str, since: str, before: str) -> bool:
    if not version:
        return not since
    found = version_key(version)
    if since and found < version_key(since):
        return False
    return not (before and found >= version_key(before))


def shaped(
    shape: Shape, name: str, command: str, args: Sequence[str], env: Mapping[str, str]
) -> dict[str, Any]:
    plain: dict[str, Any] = {"command": command, "args": list(args), "env": dict(env)}
    match shape:
        case Shape.PLAIN:
            return plain
        case Shape.STDIO:
            return {"type": "stdio", **plain}
        case Shape.COPILOT:
            return {"type": "stdio", **plain, "tools": ["*"]}
        case Shape.OPENCODE:
            return {
                "type": "local",
                "command": [command, *args],
                "environment": dict(env),
                "enabled": True,
            }
        case Shape.GOOSE:
            return {
                "name": name,
                "type": "stdio",
                "cmd": command,
                "args": list(args),
                "envs": dict(env),
                "enabled": True,
                "timeout": 300,
            }
        case Shape.NOT_DISABLED:
            return {**plain, "disabled": False}
        case Shape.STDIO_NOT_DISABLED:
            return {"type": "stdio", **plain, "disabled": False}
        case Shape.ENABLED:
            return {**plain, "enabled": True}
        case Shape.JAN:
            return {**plain, "active": True}
        case Shape.FIVE:
            return {"name": name, **plain, "isActive": True}
        case _:
            return {"transport": "stdio", "enabled": True, **plain}


def install_link(
    style: LinkStyle, name: str, command: str, args: Sequence[str], env: Mapping[str, str]
) -> str:
    inner = shaped(Shape.PLAIN, name, command, args, env)
    match style:
        case LinkStyle.CHERRY:
            payload = _base64({"mcpServers": {name: inner}})
            return f"cherrystudio://mcp/install?{urlencode({'servers': payload})}"
        case LinkStyle.DEEPCHAT:
            payload = _base64({"mcpServers": {name: inner}})
            return f"deepchat://mcp/install?{urlencode({'code': payload})}"
        case _:
            payload = _base64({"name": name, **inner})
            return f"chatbox://mcp/install?server={quote(payload, safe='')}"


def _base64(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")
