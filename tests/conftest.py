import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.domain import Company, Platform


@pytest.fixture
def windows_launcher() -> Callable[[int], bytes]:
    def build(subsystem: int) -> bytes:
        image = bytearray(b"\x00" * 512)
        image[0:2] = b"MZ"
        offset = 0x80
        image[0x3C:0x40] = offset.to_bytes(4, "little")
        image[offset : offset + 4] = b"PE\x00\x00"
        image[offset + 0x5C : offset + 0x5E] = subsystem.to_bytes(2, "little")
        return bytes(image)

    return build


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    distributed = config.getoption("dist", default="no") != "no"
    worker = os.environ.get("PYTEST_XDIST_WORKER") is not None
    if not (distributed or worker):
        return
    skip = pytest.mark.skip(reason="asserts wall clock; run 'pytest -m serial' without -n")
    for item in items:
        if "serial" in item.keywords:
            item.add_marker(skip)


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
    monkeypatch.setenv("STAGE_CAPTURE_DIR", str(tmp_path / "captured"))
    yield
