from pathlib import Path

import pytest

from stage.cli.logfile import open_request_log, rotate


def _fill(path: Path, size: int) -> None:
    path.write_text("x" * size, encoding="utf-8")


def test_a_small_log_is_not_rotated(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"
    _fill(log, 100)

    assert rotate(log, max_bytes=1000) is False
    assert log.read_text(encoding="utf-8") == "x" * 100
    assert list(tmp_path.iterdir()) == [log]


def test_a_full_log_rotates_to_generation_one(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"
    _fill(log, 1000)

    assert rotate(log, max_bytes=1000) is True
    assert not log.exists()
    assert (tmp_path / "requests.jsonl.1").read_text(encoding="utf-8") == "x" * 1000


def test_generations_shift_and_the_oldest_is_dropped(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"

    for marker in ("first", "second", "third", "fourth"):
        log.write_text(marker.ljust(1000, "."), encoding="utf-8")
        rotate(log, max_bytes=1000, generations=3)

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ["requests.jsonl.1", "requests.jsonl.2", "requests.jsonl.3"]
    assert (tmp_path / "requests.jsonl.1").read_text(encoding="utf-8").startswith("fourth")
    assert (tmp_path / "requests.jsonl.3").read_text(encoding="utf-8").startswith("second")


def test_opening_appends_and_rotates_once_per_run(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"

    with open_request_log(log, max_bytes=1000) as stream:
        stream.write("a" * 600 + "\n")
    with open_request_log(log, max_bytes=1000) as stream:
        stream.write("b" * 600 + "\n")

    assert log.stat().st_size > 1000
    assert not (tmp_path / "requests.jsonl.1").exists()

    with open_request_log(log, max_bytes=1000) as stream:
        stream.write("c\n")

    assert log.read_text(encoding="utf-8") == "c\n"
    assert (tmp_path / "requests.jsonl.1").exists()


def test_the_parent_directory_is_created(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "audit" / "requests.jsonl"

    with open_request_log(log) as stream:
        stream.write("{}\n")

    assert log.read_text(encoding="utf-8") == "{}\n"


def test_at_least_one_generation_is_required(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"
    _fill(log, 2000)

    with pytest.raises(ValueError, match="at least one generation"):
        rotate(log, max_bytes=1000, generations=0)
