from pathlib import Path

import pytest

from stage.paths import capture_dir, config_dir, data_dir, database_path, lexicon_dir, registry_path


def test_the_database_override_wins_over_the_platform_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere" / "stage.db"
    monkeypatch.setenv("STAGE_DB", str(target))
    assert database_path() == target.resolve(), "STAGE_DB stopped overriding the database path"


def test_the_database_falls_back_to_app_data_when_no_override_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STAGE_DB", raising=False)
    resolved = database_path()
    assert resolved.parent == data_dir(), "the default database left the platform data directory"
    assert resolved.name == "stage.db"


def test_an_empty_override_is_not_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE_DB", "")
    assert database_path().parent == data_dir(), (
        "an empty variable must read as absent rather than as the current directory"
    )


def test_the_registry_prefers_the_override_then_the_packaged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STAGE_REGISTRY", raising=False)
    packaged = registry_path()
    assert packaged.exists(), "the packaged registry must ship inside the installed package"
    assert packaged.parent.name == "data", "the registry stopped resolving relative to the package"

    override = tmp_path / "companies.yaml"
    override.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("STAGE_REGISTRY", str(override))
    assert registry_path() == override.resolve(), "STAGE_REGISTRY stopped overriding the registry"


def test_the_lexicon_override_replaces_the_packaged_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STAGE_LEXICON", raising=False)
    assert lexicon_dir().name == "lexicon", "the packaged lexicon stopped resolving"

    monkeypatch.setenv("STAGE_LEXICON", str(tmp_path / "words"))
    assert lexicon_dir() == (tmp_path / "words").resolve()


def test_the_capture_directory_is_created_wherever_it_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "deep" / "captures"
    monkeypatch.setenv("STAGE_CAPTURE_DIR", str(target))
    resolved = capture_dir()
    assert resolved.is_dir(), "a capture directory must exist before an adapter writes into it"
    assert resolved == target


def test_a_user_directory_is_created_on_demand() -> None:
    assert data_dir().is_dir(), "the data directory must exist after it is resolved"
    assert config_dir().is_dir(), "the config directory must exist after it is resolved"
