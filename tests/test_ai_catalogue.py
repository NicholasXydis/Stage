import re
from pathlib import Path

import pytest

from stage.domain.ai_apps import AiApp, ConfigFormat, System, shaped
from stage.services.ai_catalogue import APPS, EDITORS
from stage.services.app_config import with_entry
from stage.services.connect import (
    SERVER_NAME,
    Machine,
    ServerEntry,
    Target,
    expand,
    instructions,
)

ENTRY = ServerEntry("/opt/stage/bin/stage", ("mcp",), {"STAGE_DB": "/data/stage.db"})
LEFTOVER = re.compile(r"\{[a-z:A-Z_|]+\}")
IDS = [app.key for app in APPS]


def test_every_app_has_a_unique_key() -> None:
    assert len(IDS) == len(set(IDS))
    assert all(re.fullmatch(r"[a-z0-9-]+", key) for key in IDS)


@pytest.mark.parametrize("app", APPS, ids=IDS)
@pytest.mark.parametrize("system", list(System))
def test_every_place_expands_completely(app: AiApp, system: System) -> None:
    machine = Machine(system, Path("/h"), {})
    templates = [where.on(system) for where in app.markers + app.version_files]
    if app.file is not None:
        templates += [where.on(system) for where in app.file.where]
    for template in filter(None, templates):
        expanded = expand(template.replace("{user}", "/u"), machine, "/u")
        assert not LEFTOVER.search(expanded), expanded
        assert expanded.startswith(("/", "C:/"))


@pytest.mark.parametrize(
    "app", [app for app in APPS if app.file is not None], ids=lambda app: app.key
)
def test_every_entry_is_written_in_its_apps_format(app: AiApp) -> None:
    assert app.file is not None
    entry = shaped(app.file.shape, SERVER_NAME, ENTRY.command, ENTRY.args, ENTRY.env)
    written = with_entry("", app.file.format, app.file.key, SERVER_NAME, entry)

    assert ENTRY.command in written
    assert "/data/stage.db" in written
    if app.file.format is ConfigFormat.YAML:
        assert len(app.file.key) == 1


@pytest.mark.parametrize(
    "app", [app for app in APPS if app.command is not None], ids=lambda app: app.key
)
def test_every_own_command_names_stage_and_carries_its_settings(app: AiApp) -> None:
    assert app.command is not None
    add = app.command.add
    assert "{name}" in add or "{json}" in add
    assert "{env}" in add or "{entry}" in add or "{json}" in add
    assert ("{env}" in add) == bool(app.command.env_flag)
    if "--" in add:
        assert add.index("--") < add.index("{command}")
    assert "{args}" in add or "{each-arg}" in add or "{entry}" in add or "{json}" in add
    if "{env}" in add and "{name}" in add:
        assert add.index("{name}") < add.index("{env}")


@pytest.mark.parametrize("app", APPS, ids=IDS)
def test_every_app_has_a_way_in_and_instructions_without_gaps(app: AiApp) -> None:
    settings = Path("/h/settings") if app.file is not None else None
    target = Target(app, app.name, settings)

    text = instructions(target, ENTRY)

    assert app.file or app.command or app.link or app.screens
    assert ENTRY.command in text
    assert not LEFTOVER.search(text), text


def test_editors_expand_on_every_system() -> None:
    for editor in EDITORS:
        for system in System:
            user = editor.user.on(system)
            assert user and not LEFTOVER.search(expand(user, Machine(system, Path("/h"), {})))


def test_docs_are_links() -> None:
    assert all(app.docs.startswith("https://") for app in APPS if app.docs)
