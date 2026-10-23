import copy
import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from stage.domain.ai_apps import ConfigFormat

MAX_CONFIG_BYTES = 8 * 1024 * 1024
BACKUP_SUFFIX = ".stage.bak"
NEW_FILE_MODE = 0o600

_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_PLAIN_YAML_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class UnwritableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ConfigText:
    path: Path
    text: str
    raw: bytes | None
    bom: bool = False
    crlf: bool = False

    @property
    def exists(self) -> bool:
        return self.raw is not None


def read_config(path: Path) -> ConfigText:
    target = _target(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return ConfigText(target, "", None)
    except IsADirectoryError as exc:
        raise UnwritableError(f"{target} is a folder, not a file") from exc
    except PermissionError as exc:
        raise UnwritableError(f"{target} cannot be read with your permissions") from exc
    except OSError as exc:
        raise UnwritableError(f"{target} cannot be read: {exc.strerror or exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise UnwritableError(f"{target} is larger than a settings file should be")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnwritableError(f"{target} is not UTF-8 text") from exc
    bom = text.startswith("﻿")
    if bom:
        text = text[1:]
    crlf = "\r\n" in text
    return ConfigText(target, text.replace("\r\n", "\n"), raw, bom, crlf)


def write_config(current: ConfigText, text: str) -> Path | None:
    body = text.replace("\n", "\r\n") if current.crlf else text
    data = (("﻿" if current.bom else "") + body).encode("utf-8")
    if current.raw == data:
        return None
    target = current.path
    if current.raw is not None:
        if not os.access(target, os.W_OK):
            raise UnwritableError(f"{target} is read-only")
        try:
            again = target.read_bytes()
        except OSError as exc:
            raise UnwritableError(f"{target} cannot be read again: {exc.strerror or exc}") from exc
        if again != current.raw:
            raise UnwritableError(
                f"{target} changed while Stage was editing it. Close the app that uses it "
                "and try again."
            )
    mode = _mode(target) if current.raw is not None else NEW_FILE_MODE
    backup = None
    if current.raw is not None:
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        _replace(backup, current.raw, mode)
    _replace(target, data, mode)
    return backup


def entry_in(
    text: str, kind: ConfigFormat, key: Sequence[str], name: str
) -> Mapping[str, Any] | None:
    parent = _walk(_load(text, kind, lenient=True), key[:-1], create=False)
    node = parent.get(key[-1]) if parent is not None and key else None
    if isinstance(node, list):
        named = [item for item in node if isinstance(item, Mapping) and item.get("name") == name]
        return named[0] if named else None
    if not isinstance(node, Mapping):
        return None
    found = node.get(name)
    return found if isinstance(found, Mapping) else None


def has_entry(text: str, kind: ConfigFormat, key: Sequence[str], name: str) -> bool:
    node = _walk(_load(text, kind, lenient=True), key, create=False)
    return node is not None and name in node


def with_entry(
    text: str, kind: ConfigFormat, key: Sequence[str], name: str, entry: Mapping[str, Any]
) -> str:
    original = _load(text, kind)
    expected = copy.deepcopy(original)
    node = _walk(expected, key, create=True)
    assert node is not None
    node[name] = copy.deepcopy(dict(entry))
    if kind is ConfigFormat.JSON:
        edited = _dump_json(expected, text)
    elif kind is ConfigFormat.TOML:
        edited = _toml_with(text, key, name, entry, original)
    else:
        edited = _yaml_with(text, key, name, entry)
    _verify(edited, kind, expected)
    return edited


def without_entry(text: str, kind: ConfigFormat, key: Sequence[str], name: str) -> str:
    original = _load(text, kind)
    expected = copy.deepcopy(original)
    node = _walk(expected, key, create=False)
    if node is None or name not in node:
        return text
    del node[name]
    if kind is ConfigFormat.JSON:
        edited = _dump_json(expected, text)
    elif kind is ConfigFormat.TOML:
        edited = _toml_without(text, key, name)
    else:
        edited = _yaml_without(text, key, name)
    try:
        _verify(edited, kind, expected)
    except UnwritableError:
        _verify(edited, kind, _pruned(expected, key))
    return edited


def _pruned(root: dict[str, Any], key: Sequence[str]) -> dict[str, Any]:
    trimmed = copy.deepcopy(root)
    for depth in range(len(key), 0, -1):
        parent = _walk(trimmed, key[: depth - 1], create=False)
        if parent is None or parent.get(key[depth - 1]) != {}:
            break
        del parent[key[depth - 1]]
    return trimmed


def _target(path: Path) -> Path:
    if path.is_symlink():
        resolved = Path(os.path.realpath(path))
        if not resolved.exists():
            raise UnwritableError(f"{path} is a link to {resolved}, which does not exist")
        return resolved
    return path


def _mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return NEW_FILE_MODE


def _replace(target: Path, data: bytes, mode: int) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    except OSError as exc:
        raise UnwritableError(f"cannot write in {target.parent}: {exc.strerror or exc}") from exc
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UnwritableError(f"cannot write {target}: {exc.strerror or exc}") from exc
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load(text: str, kind: ConfigFormat, *, lenient: bool = False) -> dict[str, Any]:
    if not text.strip():
        return {}
    match kind:
        case ConfigFormat.JSON:
            loaded = _load_json(_without_comments(text) if lenient else text)
        case ConfigFormat.TOML:
            try:
                loaded = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise UnwritableError(f"is not valid TOML ({exc})") from exc
        case ConfigFormat.YAML:
            try:
                loaded = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise UnwritableError("is not valid YAML") from exc
            if loaded is None:
                return {}
    if not isinstance(loaded, dict):
        raise UnwritableError("does not hold a set of named settings at the top")
    return loaded


def _load_json(text: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for name, value in pairs:
            if name in seen:
                raise UnwritableError(f"names {name!r} twice in one place")
            seen[name] = value
        return seen

    try:
        return json.loads(text, object_pairs_hook=unique)
    except json.JSONDecodeError as exc:
        if _json_has_comments(text):
            raise UnwritableError(
                "has comments or trailing commas, which Stage leaves alone"
            ) from exc
        raise UnwritableError(f"is not valid JSON (line {exc.lineno})") from exc


def _without_comments(text: str) -> str:
    kept: list[str] = []
    index = 0
    quoted = False
    while index < len(text):
        character = text[index]
        if quoted:
            kept.append(character)
            if character == "\\" and index + 1 < len(text):
                kept.append(text[index + 1])
                index += 1
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
            kept.append(character)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        elif character in "}]":
            while kept and kept[-1].isspace():
                kept.pop()
            if kept and kept[-1] == ",":
                kept.pop()
            kept.append(character)
        else:
            kept.append(character)
        index += 1
    return "".join(kept)


def _json_has_comments(text: str) -> bool:
    quoted = False
    escaped = False
    previous = ""
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif (previous == "/" and character in "/*") or (previous == "," and character in "}]"):
            return True
        if not character.isspace():
            previous = character
    return False


def _walk(root: dict[str, Any], key: Sequence[str], *, create: bool) -> dict[str, Any] | None:
    node = root
    for part in key:
        child = node.get(part)
        if child is None:
            if not create:
                return None
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise UnwritableError(f"keeps {'.'.join(key)} as something other than a set of servers")
        node = child
    return node


def _verify(text: str, kind: ConfigFormat, expected: dict[str, Any]) -> None:
    try:
        reread = _load(text, kind)
    except UnwritableError as exc:
        raise UnwritableError("could not be edited safely, so Stage left it alone") from exc
    if reread != expected:
        raise UnwritableError("could not be edited safely, so Stage left it alone")


def _dump_json(root: dict[str, Any], original: str) -> str:
    return json.dumps(root, indent=_json_indent(original), ensure_ascii=False) + "\n"


def _json_indent(text: str) -> int | str:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip()
        if stripped and stripped != line:
            margin = line[: len(line) - len(stripped)]
            return "\t" if margin.startswith("\t") else len(margin)
    return 2


def _toml_key(name: str) -> str:
    return name if _BARE_TOML_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, Sequence):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise UnwritableError(f"cannot write {type(value).__name__} values")


def _toml_header(key: Sequence[str], name: str) -> re.Pattern[str]:
    def part(segment: str) -> str:
        escaped = re.escape(segment)
        return f"(?:{escaped}|\"{escaped}\"|'{escaped}')"

    joined = r"\s*\.\s*".join(part(segment) for segment in (*key, name))
    return re.compile(rf"^\s*\[\s*{joined}\s*(?:\]|\.)")


def _toml_without(text: str, key: Sequence[str], name: str) -> str:
    header = _toml_header(key, name)
    lines = text.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if not header.match(lines[index]):
            kept.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].lstrip().startswith("["):
            end += 1
        tail = end
        while tail > index + 1 and (
            not lines[tail - 1].strip() or lines[tail - 1].lstrip().startswith("#")
        ):
            tail -= 1
        kept.extend(lines[tail:end])
        index = end
    while len(kept) > 1 and not kept[-1].strip() and not kept[-2].strip():
        kept.pop()
    return "\n".join(kept)


def _toml_with(
    text: str,
    key: Sequence[str],
    name: str,
    entry: Mapping[str, Any],
    original: dict[str, Any],
) -> str:
    node = _walk(copy.deepcopy(original), key, create=False)
    base = _toml_without(text, key, name) if node is not None and name in node else text
    table = ".".join(_toml_key(part) for part in (*key, name))
    body = "\n".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in entry.items())
    block = f"[{table}]\n{body}\n"
    trimmed = base.rstrip("\n")
    return f"{trimmed}\n\n{block}" if trimmed.strip() else block


def _yaml_line(name: str, entry: Mapping[str, Any], indent: int) -> str:
    value = json.dumps(dict(entry), ensure_ascii=False, separators=(", ", ": "))
    return f"{' ' * indent}{name}: {value}"


def _yaml_section(lines: list[str], key: str) -> tuple[int, int] | None:
    header = re.compile(rf"^{re.escape(key)}\s*:")
    for index, line in enumerate(lines):
        if header.match(line):
            end = index + 1
            while end < len(lines) and (
                not lines[end].strip() or lines[end][0] in " \t#" or lines[end].startswith("-")
            ):
                end += 1
            return index, end
    return None


def _yaml_indent(lines: list[str], start: int, end: int) -> int:
    for line in lines[start + 1 : end]:
        stripped = line.lstrip(" ")
        if stripped and not stripped.startswith("#"):
            return len(line) - len(stripped)
    return 2


def _yaml_child(lines: list[str], start: int, end: int, name: str, indent: int) -> tuple[int, int]:
    child = re.compile(rf"^ {{{indent}}}{re.escape(name)}\s*:")
    for index in range(start + 1, end):
        if child.match(lines[index]):
            stop = index + 1
            while stop < end and (
                not lines[stop].strip() or len(lines[stop]) - len(lines[stop].lstrip(" ")) > indent
            ):
                stop += 1
            while stop > index + 1 and not lines[stop - 1].strip():
                stop -= 1
            return index, stop
    return -1, -1


def _yaml_parts(key: Sequence[str], name: str) -> str:
    if len(key) != 1 or not _PLAIN_YAML_KEY.match(key[0]) or not _PLAIN_YAML_KEY.match(name):
        raise UnwritableError("keeps its servers somewhere Stage does not edit")
    return key[0]


def _yaml_with(text: str, key: Sequence[str], name: str, entry: Mapping[str, Any]) -> str:
    section = _yaml_parts(key, name)
    lines = text.split("\n")
    found = _yaml_section(lines, section)
    if found is None:
        trimmed = text.rstrip("\n")
        prefix = f"{trimmed}\n" if trimmed.strip() else ""
        return f"{prefix}{section}:\n{_yaml_line(name, entry, 2)}\n"
    start, end = found
    inline = lines[start].split(":", 1)[1].split("#", 1)[0].strip()
    if inline in ("{}", "~", "null"):
        lines[start] = f"{section}:"
    elif inline:
        raise UnwritableError(f"writes {section} on one line, which Stage does not edit")
    indent = _yaml_indent(lines, start, end)
    if indent == 0:
        raise UnwritableError(f"keeps {section} as a list, not a set of servers")
    first, stop = _yaml_child(lines, start, end, name, indent)
    line = _yaml_line(name, entry, indent)
    if first < 0:
        lines.insert(start + 1, line)
    else:
        lines[first:stop] = [line]
    return "\n".join(lines)


def _yaml_without(text: str, key: Sequence[str], name: str) -> str:
    section = _yaml_parts(key, name)
    lines = text.split("\n")
    found = _yaml_section(lines, section)
    if found is None:
        return text
    start, end = found
    indent = _yaml_indent(lines, start, end)
    first, stop = _yaml_child(lines, start, end, name, indent)
    if first < 0:
        return text
    del lines[first:stop]
    remaining = [
        line
        for line in lines[start + 1 : end - (stop - first)]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not remaining:
        lines[start] = f"{section}: {{}}"
    return "\n".join(lines)
