import os
import time
from pathlib import Path

import pytest

from stage.cli import housekeeping


def _age(path: Path, days: float) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


def test_a_small_capture_directory_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captures = tmp_path / "captured"
    captures.mkdir()
    for index in range(5):
        (captures / f"a-{index}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("stage.paths.capture_dir", lambda: captures)
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: tmp_path / "absent.jsonl")

    swept = housekeeping.tidy()

    assert swept.captures_removed == 0
    assert len(list(captures.iterdir())) == 5


def test_old_captures_beyond_the_keep_count_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captures = tmp_path / "captured"
    captures.mkdir()
    for index in range(housekeeping.CAPTURE_KEEP + 10):
        path = captures / f"a-{index}.json"
        path.write_text("{}", encoding="utf-8")
        _age(path, housekeeping.CAPTURE_KEEP_DAYS + 5 if index >= 5 else 0)
    monkeypatch.setattr("stage.paths.capture_dir", lambda: captures)
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: tmp_path / "absent.jsonl")

    swept = housekeeping.tidy()

    assert swept.captures_removed == 10
    assert len(list(captures.iterdir())) == housekeeping.CAPTURE_KEEP


def test_recent_captures_survive_however_many_there_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captures = tmp_path / "captured"
    captures.mkdir()
    for index in range(housekeeping.CAPTURE_KEEP + 20):
        (captures / f"a-{index}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("stage.paths.capture_dir", lambda: captures)
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: tmp_path / "absent.jsonl")

    swept = housekeeping.tidy()

    assert swept.captures_removed == 0


def test_a_dry_run_removes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captures = tmp_path / "captured"
    captures.mkdir()
    for index in range(housekeeping.CAPTURE_KEEP + 10):
        path = captures / f"a-{index}.json"
        path.write_text("{}", encoding="utf-8")
        _age(path, housekeeping.CAPTURE_KEEP_DAYS + 5)
    monkeypatch.setattr("stage.paths.capture_dir", lambda: captures)
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: tmp_path / "absent.jsonl")

    swept = housekeeping.tidy(dry_run=True)

    assert swept.captures_removed == 10
    assert len(list(captures.iterdir())) == housekeeping.CAPTURE_KEEP + 10


def test_an_oversized_journal_rotates_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = tmp_path / "probe-journal.jsonl"
    journal.write_text("x" * (housekeeping.JOURNAL_MAX_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr("stage.paths.capture_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: journal)

    swept = housekeeping.tidy()

    assert swept.journal_rotated
    assert not journal.exists()
    assert journal.with_suffix(".jsonl.1").is_file()


def test_a_small_journal_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = tmp_path / "probe-journal.jsonl"
    journal.write_text("small", encoding="utf-8")
    monkeypatch.setattr("stage.paths.capture_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: journal)

    swept = housekeeping.tidy()

    assert not swept.journal_rotated
    assert journal.read_text(encoding="utf-8") == "small"


def test_a_missing_data_directory_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("stage.paths.capture_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: tmp_path / "nope.jsonl")

    swept = housekeeping.tidy()

    assert swept == housekeeping.Housekeeping()


def test_purge_reports_what_it_swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from stage.cli.app import app

    captures = tmp_path / "captured"
    captures.mkdir()
    for index in range(housekeeping.CAPTURE_KEEP + 3):
        path = captures / f"a-{index}.json"
        path.write_text("{}", encoding="utf-8")
        _age(path, housekeeping.CAPTURE_KEEP_DAYS + 5)
    monkeypatch.setattr("stage.paths.capture_dir", lambda: captures)
    monkeypatch.setattr("stage.cli.logfile.probe_journal_path", lambda: tmp_path / "absent.jsonl")

    result = CliRunner().invoke(app, ["purge", "--db", str(tmp_path / "p.db")])

    assert result.exit_code == 0
    assert "3 old captured payload(s)" in result.stdout
