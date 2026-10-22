import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from stage.domain.ai_apps import (
    AiApp,
    ConfigFormat,
    Editor,
    Reach,
    Shape,
    System,
    in_range,
    install_link,
    shaped,
    version_key,
)
from stage.services.app_config import (
    UnwritableError,
    entry_in,
    read_config,
    with_entry,
    without_entry,
    write_config,
)

SERVER_NAME = "Stage"
OLD_NAMES = ("stage",)
NAMES = (SERVER_NAME, *OLD_NAMES)
PATH_SETTINGS = (
    "STAGE_DB",
    "STAGE_REGISTRY",
    "STAGE_LEXICON",
    "STAGE_EXPORT_DIR",
    "STAGE_CAPTURE_DIR",
)
SELF_TEST_SECONDS = 60.0
COMMAND_SECONDS = 60.0
LOCK_STALE_SECONDS = 10.0
LOCK_WAIT_SECONDS = 5.0
EXPECTED_TOOL = "search_postings"
POSIX_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
WINDOWS_KEPT = ("SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
ERROR_TAIL = 600
BATCH_SUFFIXES = (".cmd", ".bat")

_ENV_TOKEN = re.compile(r"\{env:([A-Z0-9_]+)\|([^{}]*(?:\{[a-z]+\}[^{}]*)*)\}")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class Status(StrEnum):
    CONNECTED = "connected"
    OUTDATED = "outdated"
    BROKEN = "broken"
    ABSENT = "not connected"
    UNREADABLE = "unreadable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ServerEntry:
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SelfTest:
    passed: bool
    detail: str
    tools: int = 0


@dataclass(frozen=True, slots=True)
class Machine:
    system: System
    home: Path
    environ: Mapping[str, str]

    @classmethod
    def here(cls) -> "Machine":
        return cls(current_system(), Path.home(), dict(os.environ))


@dataclass(frozen=True, slots=True)
class Target:
    app: AiApp
    label: str
    settings: Path | None = None
    program: str | None = None
    version: str = ""
    editor: str = ""
    alternates: tuple[Path, ...] = ()

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.settings, *self.alternates) if self.settings is not None else ()

    @property
    def reach(self) -> Reach:
        if self.program is not None and self.app.command is not None:
            return Reach.COMMAND
        if self.settings is not None and self.app.file is not None and self.app.edits_file:
            return Reach.FILE
        if self.app.link is not None:
            return Reach.LINK
        return Reach.SCREENS


@dataclass(frozen=True, slots=True)
class Reading:
    status: Status
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    target: Target
    status: Status
    changed: bool
    message: str
    backup: Path | None = None
    instructions: str = ""
    link: str = ""


def current_system() -> System:
    name = platform.system()
    if name == "Darwin":
        return System.MACOS
    if name == "Windows":
        return System.WINDOWS
    return System.LINUX


def server_entry(
    executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    database: Path | None = None,
) -> ServerEntry:
    settings = os.environ if environ is None else environ
    env = {
        name: str(Path(value).expanduser().resolve())
        for name in PATH_SETTINGS
        if (value := settings.get(name, "").strip())
    }
    if database is not None:
        env["STAGE_DB"] = str(database.expanduser().resolve())
    python = Path(executable or sys.executable)
    script = python.with_name("stage.exe" if python.suffix.lower() == ".exe" else "stage")
    if script.is_file():
        return ServerEntry(str(script), ("mcp",), env)
    return ServerEntry(str(python), ("-m", "stage", "mcp"), env)


def expand(template: str, machine: Machine, user: str | None = None) -> str:
    def environment(match: re.Match[str]) -> str:
        value = machine.environ.get(match.group(1), "").strip()
        return value or expand(match.group(2), machine, user)

    text = _ENV_TOKEN.sub(environment, template)
    home = str(machine.home)
    tokens = {
        "{support}": f"{home}/Library/Application Support",
        "{appdata}": machine.environ.get("APPDATA") or f"{home}/AppData/Roaming",
        "{localappdata}": machine.environ.get("LOCALAPPDATA") or f"{home}/AppData/Local",
        "{config}": machine.environ.get("XDG_CONFIG_HOME") or f"{home}/.config",
        "{data}": machine.environ.get("XDG_DATA_HOME") or f"{home}/.local/share",
        "{lmstudio}": _lmstudio_home(machine),
        "{hermes}": _hermes_home(machine),
    }
    if user is not None:
        tokens["{user}"] = user
    for token, value in tokens.items():
        text = text.replace(token, value)
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    return text


def _hermes_home(machine: Machine) -> str:
    chosen = machine.environ.get("HERMES_HOME", "").strip()
    if chosen:
        root = Path(chosen).expanduser()
    elif machine.system is System.WINDOWS:
        local = machine.environ.get("LOCALAPPDATA") or f"{machine.home}/AppData/Local"
        root = Path(local) / "hermes"
    else:
        root = machine.home / ".hermes"
    try:
        profile = (root / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        profile = ""
    if profile and "/" not in profile and "\\" not in profile and profile not in (".", ".."):
        return str(root / "profiles" / profile)
    return str(root)


def _lmstudio_home(machine: Machine) -> str:
    pointer = machine.home / ".lmstudio-home-pointer"
    try:
        pointed = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        pointed = ""
    if pointed:
        return pointed
    legacy = machine.home / ".cache" / "lm-studio"
    return str(legacy) if legacy.is_dir() else str(machine.home / ".lmstudio")


def targets(
    machine: Machine,
    apps: Sequence[AiApp] | None = None,
    editors: Sequence[Editor] | None = None,
    *,
    every: bool = False,
) -> tuple[Target, ...]:
    if apps is None or editors is None:
        from stage.services.ai_catalogue import APPS, EDITORS

        apps = APPS if apps is None else apps
        editors = EDITORS if editors is None else editors
    found: list[Target] = []
    for app in apps:
        if app.extension:
            found.extend(_extension_targets(app, editors, machine, every=every))
            continue
        version = _app_version(app, machine)
        if not every and not _installed(app, machine):
            continue
        if not every and not in_range(version, app.since, app.before):
            continue
        settings, alternates = _places(app, machine)
        program = _program(app, machine, editors)
        found.append(Target(app, app.name, settings, program, version, alternates=alternates))
    return _merged(found)


def _merged(found: Sequence[Target]) -> tuple[Target, ...]:
    kept: list[Target] = []
    for target in found:
        twin = next(
            (
                index
                for index, other in enumerate(kept)
                if target.settings is not None
                and other.settings == target.settings
                and other.app.file == target.app.file
            ),
            None,
        )
        if twin is None:
            kept.append(target)
            continue
        first = kept[twin]
        if target.app.name not in first.label.split(", "):
            kept[twin] = replace(first, label=f"{first.label}, {target.app.name}")
    return tuple(kept)


def _installed(app: AiApp, machine: Machine) -> bool:
    if _program_named(app.programs, machine) is not None:
        return True
    for marker in app.markers:
        template = marker.on(machine.system)
        if template and Path(expand(template, machine)).exists():
            return True
    return False


def _program_named(names: Sequence[str], machine: Machine) -> str | None:
    search = machine.environ.get("PATH", "")
    for name in names:
        located = shutil.which(name, path=search)
        if located:
            return located
    return None


def _program(app: AiApp, machine: Machine, editors: Sequence[Editor] = ()) -> str | None:
    if app.command is None:
        return None
    located = _program_named((app.command.program,), machine) or _bundled(app, machine, editors)
    if located is None:
        return None
    if machine.system is System.WINDOWS and located.lower().endswith(BATCH_SUFFIXES):
        return None
    return located


def _bundled(app: AiApp, machine: Machine, editors: Sequence[Editor]) -> str | None:
    found: list[tuple[tuple[int, ...], Path]] = []
    for editor in editors:
        root = Path(expand(editor.extensions, machine))
        for pattern in app.bundled:
            for candidate in root.glob(pattern):
                runnable = candidate.is_file() and (
                    machine.system is System.WINDOWS or os.access(candidate, os.X_OK)
                )
                if runnable:
                    folder = candidate.relative_to(root).parts[0]
                    found.append((version_key(folder), candidate))
    return str(max(found)[1]) if found else None


def _settings(app: AiApp, machine: Machine, user: str | None = None) -> Path | None:
    return _places(app, machine, user)[0]


def _places(
    app: AiApp, machine: Machine, user: str | None = None
) -> tuple[Path | None, tuple[Path, ...]]:
    if app.file is None:
        return None, ()
    candidates = [
        Path(expand(template, machine, user))
        for where in app.file.where
        if (template := where.on(machine.system))
    ]
    if not candidates:
        return None, ()
    chosen = next(
        (path for path in candidates if path.exists() or path.is_symlink()), candidates[0]
    )
    return chosen, tuple(path for path in candidates if path != chosen)


def _app_version(app: AiApp, machine: Machine) -> str:
    for where in app.version_files:
        template = where.on(machine.system)
        if not template:
            continue
        try:
            loaded = json.loads(Path(expand(template, machine)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict) and isinstance(loaded.get("version"), str):
            return str(loaded["version"])
    return ""


def _extension_targets(
    app: AiApp, editors: Sequence[Editor], machine: Machine, *, every: bool
) -> list[Target]:
    per_editor = app.file is not None and any(
        "{user}" in (where.on(machine.system) or "") for where in app.file.where
    )
    found: list[Target] = []
    for editor in editors:
        version = _extension_version(app.extension, editor, machine)
        if version is None and not every:
            continue
        if version is not None and not in_range(version, app.since, app.before):
            continue
        if not per_editor:
            label = app.name
            if any(target.label == label for target in found):
                continue
            settings, alternates = _places(app, machine)
            found.append(Target(app, label, settings, None, version or "", alternates=alternates))
            continue
        user = editor.user.on(machine.system)
        if not user:
            continue
        settings, alternates = _places(app, machine, expand(user, machine))
        label = f"{app.name} in {editor.name}"
        found.append(Target(app, label, settings, None, version or "", editor.key, alternates))
    if not found and not per_editor and _installed(app, machine) and not app.before:
        found.append(
            Target(
                app,
                app.name,
                *_places(app, machine)[:1],
                _program(app, machine, editors),
                alternates=_places(app, machine)[1],
            )
        )
    return found


def _extension_version(extension: str, editor: Editor, machine: Machine) -> str | None:
    root = Path(expand(editor.extensions, machine))
    prefix = extension.lower() + "-"
    try:
        names = [child.name for child in root.iterdir() if child.is_dir()]
    except OSError:
        return None
    versions = [
        name[len(prefix) :].split("-", 1)[0] for name in names if name.lower().startswith(prefix)
    ]
    if not versions:
        return None
    return max(versions, key=version_key)


def desired(target: Target, entry: ServerEntry) -> dict[str, Any]:
    assert target.app.file is not None
    return shaped(target.app.file.shape, SERVER_NAME, entry.command, entry.args, entry.env)


def read_status(target: Target, entry: ServerEntry) -> Reading:
    file = target.app.file
    if file is None or target.settings is None:
        return Reading(Status.UNKNOWN)
    holder = _holder(target)
    if holder is None:
        return Reading(Status.ABSENT)
    path, name, found = holder
    if found is None:
        return Reading(Status.UNREADABLE, f"{path} could not be read")
    command, args, env = _parts(found)
    if command != entry.command and not Path(command).exists() and shutil.which(command) is None:
        return Reading(Status.BROKEN, f"it runs {command}, which no longer exists")
    if name != SERVER_NAME:
        return Reading(Status.OUTDATED, f"it is listed under the old name {name}")
    if (command, args, env) == (entry.command, list(entry.args), dict(entry.env)):
        return Reading(Status.CONNECTED)
    return Reading(Status.OUTDATED, "it runs Stage with older settings")


def _holder(target: Target) -> tuple[Path, str, Mapping[str, Any] | None] | None:
    file = target.app.file
    assert file is not None
    unreadable: Path | None = None
    texts: dict[Path, str] = {}
    for path in target.files:
        try:
            texts[path] = read_config(path).text
        except UnwritableError:
            if path == target.settings:
                unreadable = path
    for name in NAMES:
        for path, text in texts.items():
            try:
                found = entry_in(text, file.format, file.key, name)
            except UnwritableError:
                if path == target.settings:
                    unreadable = path
                continue
            if found is not None:
                return path, name, found
    return (unreadable, SERVER_NAME, None) if unreadable is not None else None


def _parts(found: Mapping[str, Any]) -> tuple[str, list[str], dict[str, str]]:
    command = found.get("command", found.get("cmd", ""))
    args = found.get("args", [])
    if isinstance(command, list):
        command, args = (command[0] if command else ""), command[1:]
    env = found.get("env", found.get("envs", found.get("environment", {}))) or {}
    return (
        str(command),
        [str(item) for item in args] if isinstance(args, list) else [],
        {str(k): str(v) for k, v in env.items()} if isinstance(env, Mapping) else {},
    )


@dataclass(frozen=True, slots=True)
class AppHealth:
    key: str
    label: str
    status: Status
    detail: str = ""


def app_health(
    machine: Machine | None = None, entry: ServerEntry | None = None
) -> tuple[AppHealth, ...]:
    shown = (Status.CONNECTED, Status.OUTDATED, Status.BROKEN)
    chosen = entry or server_entry()
    found: list[AppHealth] = []
    for target in targets(machine or Machine.here()):
        reading = read_status(target, chosen)
        if reading.status in shown:
            found.append(
                AppHealth(
                    target.app.key.removesuffix("-legacy"),
                    target.label,
                    reading.status,
                    reading.detail,
                )
            )
    return tuple(found)


def connect(
    target: Target,
    entry: ServerEntry,
    *,
    dry_run: bool = False,
    runner: Runner | None = None,
    running: Callable[[str], bool] | None = None,
) -> Outcome:
    app = target.app
    if app.closed_first and app.process and (running or _is_running)(app.process):
        return Outcome(
            target,
            Status.UNKNOWN,
            False,
            f"Quit {app.name} first. It rewrites its settings while it is open and would "
            "undo the change.",
        )
    before = read_status(target, entry)
    if before.status is Status.CONNECTED:
        return Outcome(target, Status.CONNECTED, False, "already connected")
    if app.spaceless and " " in entry.command:
        return Outcome(
            target,
            Status.UNKNOWN,
            False,
            f"{app.name} cannot start a program whose path has a space in it, and Stage is "
            f"installed at {entry.command}. Install Stage in a folder without a space to use "
            f"it with {app.name}.",
        )
    reach = target.reach
    if reach is Reach.COMMAND and not _carries(app, entry):
        reach = (
            Reach.FILE if target.settings is not None and app.file is not None else Reach.SCREENS
        )
    match reach:
        case Reach.COMMAND:
            return _connect_by_command(target, entry, before, dry_run, runner or _run)
        case Reach.FILE:
            return _connect_by_file(target, entry, dry_run)
        case Reach.LINK:
            assert app.link is not None
            link = install_link(app.link, SERVER_NAME, entry.command, entry.args, entry.env)
            return Outcome(
                target,
                Status.UNKNOWN,
                False,
                f"{app.name} adds servers through its own install link. Confirm it in "
                f"{app.name} when it opens.",
                link=link,
                instructions=instructions(target, entry),
            )
        case Reach.SCREENS:
            if app.command is not None and target.program is None:
                said = (
                    f"Stage could not find the {app.command.program} command, so run this "
                    "yourself where it works."
                )
            elif app.file is None and app.screens.startswith(("Add", "Or add")):
                said = f"Stage cannot add itself to {app.name}, so here is what to add."
            else:
                said = f"{app.name} keeps its servers in its own settings screens."
            return Outcome(
                target, Status.UNKNOWN, False, said, instructions=instructions(target, entry)
            )


def _connect_by_file(target: Target, entry: ServerEntry, dry_run: bool) -> Outcome:
    file = target.app.file
    assert file is not None and target.settings is not None
    holder = _holder(target)
    path = holder[0] if holder is not None else target.settings
    try:
        current = read_config(path)
        edited = with_entry(
            current.text, file.format, file.key, SERVER_NAME, desired(target, entry)
        )
        for name in OLD_NAMES:
            edited = without_entry(edited, file.format, file.key, name)
        if dry_run:
            verb = "add Stage to" if current.exists else "create"
            return Outcome(target, Status.ABSENT, False, f"would {verb} {current.path}")
        with _locked(current.path if file.locked else None):
            backup = write_config(current, edited)
    except UnwritableError as exc:
        return Outcome(
            target,
            Status.UNREADABLE,
            False,
            f"{target.settings} {exc}",
            instructions=instructions(target, entry),
        )
    return Outcome(target, Status.CONNECTED, True, _wrote(target, current.path), backup)


def _wrote(target: Target, path: Path) -> str:
    after = " Quit and reopen it so it loads Stage." if target.app.restart else ""
    return f"added Stage to {path}.{after}"


def _connect_by_command(
    target: Target, entry: ServerEntry, before: Reading, dry_run: bool, runner: Runner
) -> Outcome:
    command = target.app.command
    assert command is not None and target.program is not None
    adding = [target.program, *fill(command.add, command.env_flag, entry)]
    holder = _holder(target) if target.settings is not None and target.app.file else None
    present = holder[1] if holder is not None else SERVER_NAME
    removing = (
        [target.program, *fill(command.remove, command.env_flag, entry, present)]
        if command.remove
        else []
    )
    if dry_run:
        return Outcome(target, Status.ABSENT, False, f"would run {_shown(adding)}")
    if removing and before.status in (Status.OUTDATED, Status.BROKEN, Status.UNKNOWN):
        runner(removing)
    result = runner(adding)
    if result.returncode != 0:
        said = " ".join((result.stderr or result.stdout or "").split())[-ERROR_TAIL:]
        if target.settings is not None and target.app.file is not None and _editable(target):
            fallback = _connect_by_file(target, entry, dry_run=False)
            if fallback.changed:
                return fallback
        return Outcome(
            target,
            Status.UNKNOWN,
            False,
            f"{Path(target.program).name} refused: {said or f'exit code {result.returncode}'}",
            instructions=instructions(target, entry),
        )
    _drop_old_names(target)
    after = read_status(target, entry)
    if after.status in (Status.CONNECTED, Status.UNKNOWN):
        return Outcome(target, Status.CONNECTED, True, f"ran {_shown(adding)}")
    said = " ".join((result.stdout or result.stderr or "").split())[-ERROR_TAIL:]
    if target.settings is not None and target.app.file is not None and _editable(target):
        fallback = _connect_by_file(target, entry, dry_run=False)
        if fallback.changed:
            return fallback
    return Outcome(
        target,
        after.status,
        False,
        f"{Path(target.program).name} said it worked, but Stage is not in its settings"
        + (f": {said}" if said else "."),
        instructions=instructions(target, entry),
    )


def _editable(target: Target) -> bool:
    return target.app.edits_file


def _drop_old_names(target: Target) -> None:
    file = target.app.file
    if file is None or not _editable(target):
        return
    for path in target.files:
        try:
            current = read_config(path)
            edited = current.text
            for name in OLD_NAMES:
                edited = without_entry(edited, file.format, file.key, name)
            if not current.exists or edited == current.text:
                continue
            with _locked(current.path if file.locked else None):
                write_config(current, edited)
        except UnwritableError:
            continue


def _carries(app: AiApp, entry: ServerEntry) -> bool:
    if app.command is None:
        return False
    fields = set(app.command.add)
    if any(arg.startswith("-") for arg in entry.args) and "{args}" in fields and "--" not in fields:
        return False
    return not entry.env or bool(fields & {"{env}", "{json}", "{entry}"})


def disconnect(
    target: Target,
    entry: ServerEntry,
    *,
    dry_run: bool = False,
    runner: Runner | None = None,
    running: Callable[[str], bool] | None = None,
) -> Outcome:
    app = target.app
    if app.closed_first and app.process and (running or _is_running)(app.process):
        return Outcome(target, Status.UNKNOWN, False, f"Quit {app.name} first.")
    before = read_status(target, entry)
    if before.status is Status.ABSENT:
        return Outcome(target, Status.ABSENT, False, "was not connected")
    if target.reach is Reach.COMMAND and app.command is not None and app.command.remove:
        assert target.program is not None
        present = _present_names(target)
        commands = [
            [target.program, *fill(app.command.remove, app.command.env_flag, entry, name)]
            for name in present
        ]
        if dry_run:
            shown = " and ".join(_shown(command) for command in commands)
            return Outcome(target, before.status, False, f"would run {shown}")
        for command in commands:
            (runner or _run)(command)
        if commands and read_status(target, entry).status is Status.ABSENT:
            return Outcome(
                target,
                Status.ABSENT,
                True,
                "ran " + " and ".join(_shown(command) for command in commands),
            )
    file = app.file
    if file is None or target.settings is None or not _editable(target):
        return Outcome(
            target,
            Status.UNKNOWN,
            False,
            f"Remove the server named {SERVER_NAME} in {app.name}'s settings.",
        )
    removed: list[Path] = []
    backup: Path | None = None
    for path in target.files:
        try:
            current = read_config(path)
            held = [
                name
                for name in NAMES
                if entry_in(current.text, file.format, file.key, name) is not None
            ]
            if not held:
                continue
            edited = current.text
            for name in held:
                edited = without_entry(edited, file.format, file.key, name)
            if dry_run:
                removed.append(current.path)
                continue
            with _locked(current.path if file.locked else None):
                backup = write_config(current, edited) or backup
            removed.append(current.path)
        except UnwritableError as exc:
            return Outcome(
                target,
                Status.UNREADABLE,
                False,
                f"{path} {exc}. Delete the {SERVER_NAME} entry under "
                f"{'.'.join(file.key)} in that file yourself.",
            )
    places = " and ".join(str(path) for path in removed)
    if dry_run:
        return Outcome(target, before.status, False, f"would remove Stage from {places}")
    if not removed:
        return Outcome(target, Status.ABSENT, False, "was not connected")
    return Outcome(target, Status.ABSENT, True, f"removed Stage from {places}", backup)


def _present_names(target: Target) -> list[str]:
    file = target.app.file
    if file is None:
        return [SERVER_NAME]
    present: list[str] = []
    for path in target.files:
        try:
            text = read_config(path).text
        except UnwritableError:
            continue
        for name in NAMES:
            try:
                held = entry_in(text, file.format, file.key, name) is not None
            except UnwritableError:
                held = False
            if held and name not in present:
                present.append(name)
    return present or [SERVER_NAME]


def fill(
    template: Sequence[str], env_flag: str, entry: ServerEntry, name: str = SERVER_NAME
) -> list[str]:
    filled: list[str] = []
    for part in template:
        match part:
            case "{command}":
                filled.append(entry.command)
            case "{args}":
                filled.extend(entry.args)
            case "{each-arg}":
                filled.extend(f"--arg={arg}" for arg in entry.args)
            case "{env}":
                for name, value in entry.env.items():
                    filled.extend((env_flag, f"{name}={value}"))
            case "{json}" | "{entry}":
                payload = shaped(Shape.STDIO, SERVER_NAME, entry.command, entry.args, entry.env)
                if part == "{json}":
                    payload = {"name": SERVER_NAME, **payload}
                filled.append(json.dumps(payload, ensure_ascii=False))
            case _:
                filled.append(part.replace("{name}", name))
    return filled


def _shown(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def instructions(target: Target, entry: ServerEntry) -> str:
    app = target.app
    plain = shaped(Shape.PLAIN, SERVER_NAME, entry.command, entry.args, entry.env)
    lines: list[str] = []
    if app.command is not None:
        program = target.program or app.command.program
        lines.append("Run:")
        lines.append("  " + _shown([program, *fill(app.command.add, app.command.env_flag, entry)]))
    if app.file is not None and target.settings is not None and app.edits_file:
        lines.append(
            f"Or add this to {target.settings}:" if lines else f"Add this to {target.settings}:"
        )
        lines.append(fragment(app.file.format, app.file.key, desired(target, entry)))
    if app.screens:
        lines.append(
            app.screens.replace("{json}", fragment(ConfigFormat.JSON, ("mcpServers",), plain))
            .replace("{name}", SERVER_NAME)
            .replace("{entry}", json.dumps(plain, ensure_ascii=False, indent=2))
            .replace("{command}", json.dumps(entry.command, ensure_ascii=False))
            .replace("{args}", json.dumps(list(entry.args), ensure_ascii=False))
        )
    if not lines or (app.screens and app.screens.endswith(":")):
        lines.append(f"  Name: {SERVER_NAME}")
        lines.append(f"  Command: {entry.command}")
        lines.append(f"  Arguments: {' '.join(entry.args)}")
        for name, value in entry.env.items():
            lines.append(f"  Environment: {name}={value}")
    return "\n".join(lines)


def fragment(kind: ConfigFormat, key: Sequence[str], value: Mapping[str, Any]) -> str:
    return with_entry("", kind, key, SERVER_NAME, value).rstrip("\n")


def _run(command: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(command), 124, "", "it did not finish in time")
    except OSError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def _is_running(process: str) -> bool:
    if os.name == "nt":
        listed = _run(["tasklist", "/FI", f"IMAGENAME eq {process}.exe", "/NH"])
        return f"{process.lower()}.exe" in listed.stdout.lower()
    return _run(["pgrep", "-x", process]).returncode == 0


@contextmanager
def _locked(path: Path | None) -> Iterator[None]:
    if path is None:
        yield
        return
    lock = path.with_name(path.name + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UnwritableError(f"cannot be created: {exc.strerror or exc}") from exc
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue
            if age > LOCK_STALE_SECONDS:
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() > deadline:
                raise UnwritableError("is being saved by its app right now; try again") from None
            time.sleep(0.1)
        except OSError as exc:
            raise UnwritableError(f"cannot be locked: {exc.strerror or exc}") from exc
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def bare_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    settings = os.environ if environ is None else environ
    if os.name == "nt":
        kept = {name: settings[name] for name in WINDOWS_KEPT if name in settings}
        root = settings.get("SYSTEMROOT", r"C:\Windows")
        return {**kept, "PATH": f"{root}\\System32;{root}"}
    return {"PATH": POSIX_PATH}


async def self_test(entry: ServerEntry, *, timeout: float = SELF_TEST_SECONDS) -> SelfTest:
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    parameters = StdioServerParameters(
        command=entry.command,
        args=list(entry.args),
        env={**bare_environment(), **entry.env},
    )
    started = time.perf_counter()
    with (
        _quiet("mcp"),
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors,
    ):
        try:
            with anyio.fail_after(timeout):
                async with Client(stdio_client(parameters, errlog=errors)) as client:
                    listed = await client.list_tools()
        except TimeoutError:
            return SelfTest(False, f"Stage did not answer within {timeout:.0f} seconds")
        except Exception as exc:
            return SelfTest(False, _failure(exc, errors))
    names = {tool.name for tool in listed.tools}
    if EXPECTED_TOOL not in names:
        return SelfTest(False, "something answered, but it was not Stage", len(names))
    took = time.perf_counter() - started
    return SelfTest(True, f"Stage answered with {len(names)} tools in {took:.1f}s", len(names))


def _failure(exc: BaseException, errors: TextIO) -> str:
    errors.seek(0)
    said = " ".join(errors.read().split())[-ERROR_TAIL:]
    if said:
        return f"Stage failed to start: {said}"
    reason = _innermost(exc)
    return f"Stage stopped before answering ({type(reason).__name__}: {reason})"


def _innermost(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


@contextmanager
def _quiet(name: str) -> Iterator[None]:
    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logger.setLevel(previous)
