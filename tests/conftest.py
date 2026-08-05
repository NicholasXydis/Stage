from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import Company, Platform


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "stage.db"


@pytest.fixture
def run_time() -> datetime:
    return datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def acme() -> Company:
    return Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("STAGE_DB", str(tmp_path / "env-stage.db"))
    yield
