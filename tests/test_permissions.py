import os
from collections.abc import Iterator
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import stage.paths as paths

WHOAMI = '"nickspc\\nick","S-1-5-21-3918952166-317903979-140229498-1010"\r\n'


@pytest.fixture(autouse=True)
def fresh_sid() -> Iterator[None]:
    paths._user_sid.cache_clear()
    yield
    paths._user_sid.cache_clear()


def test_windows_database_permissions_remove_inheritance_and_grant_only_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stage.db"
    target.touch()
    calls: list[tuple[str, ...]] = []

    def run(
        args: tuple[str, ...], *, check: bool, capture_output: bool, text: bool, errors: str
    ) -> CompletedProcess[str]:
        calls.append(args)
        assert errors == "replace", "an account name whoami cannot spell must not crash Stage"
        return CompletedProcess(args, 0, WHOAMI if args[0].endswith("whoami.exe") else "", "")

    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setattr("stage.paths.subprocess.run", run)

    paths._restrict_windows_permissions(target)

    assert calls == [
        (r"C:\Windows\System32\whoami.exe", "/user", "/fo", "csv", "/nh"),
        (
            r"C:\Windows\System32\icacls.exe",
            str(target),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-21-3918952166-317903979-140229498-1010:(F)",
        ),
    ], "the grant names the account by its security id, so USERDOMAIN cannot break it"


def test_windows_permission_failure_stops_database_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stage.db"
    target.touch()

    def run(
        args: tuple[str, ...], *, check: bool, capture_output: bool, text: bool, errors: str
    ) -> CompletedProcess[str]:
        if args[0].endswith("whoami.exe"):
            return CompletedProcess(args, 0, WHOAMI, "")
        return CompletedProcess(args, 1, "", "access denied")

    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr("stage.paths.subprocess.run", run)

    with pytest.raises(PermissionError, match="access denied"):
        paths._restrict_windows_permissions(target)


def test_an_unreadable_account_id_stops_before_changing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stage.db"
    target.touch()
    calls: list[tuple[str, ...]] = []

    def run(
        args: tuple[str, ...], *, check: bool, capture_output: bool, text: bool, errors: str
    ) -> CompletedProcess[str]:
        calls.append(args)
        return CompletedProcess(args, 1, "", "ERROR: no user")

    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr("stage.paths.subprocess.run", run)

    with pytest.raises(PermissionError, match="security id: ERROR: no user"):
        paths._restrict_windows_permissions(target)
    assert [args[0] for args in calls] == [r"C:\Windows\System32\whoami.exe"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_database_left_readable_by_an_old_version_is_made_private(tmp_path: Path) -> None:
    from stage.storage.sqlite_repo import SqliteRepository

    target = tmp_path / "stage.db"
    SqliteRepository.connect(target).close()
    target.chmod(0o644)

    SqliteRepository.connect(target).close()

    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_new_database_writes_its_journal_privately(tmp_path: Path) -> None:
    from stage.storage.sqlite_repo import SqliteRepository

    target = tmp_path / "stage.db"
    repository = SqliteRepository.connect(target)
    journal = tmp_path / "stage.db-wal"

    assert journal.exists() and journal.stat().st_mode & 0o777 == 0o600
    repository.close()


def test_a_new_database_that_cannot_be_made_private_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage import sqlite_repo

    def refuse(path: Path) -> None:
        raise PermissionError(f"could not restrict permissions on {path}: access denied")

    monkeypatch.setattr(sqlite_repo, "restrict_permissions", refuse)
    target = tmp_path / "stage.db"

    with pytest.raises(PermissionError, match="access denied"):
        sqlite_repo.SqliteRepository.connect(target)

    assert list(tmp_path.iterdir()) == []


def test_a_new_tracker_that_cannot_be_made_private_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.storage.tracker import TrackerStore

    def refuse(path: Path) -> None:
        raise PermissionError(f"could not restrict permissions on {path}: access denied")

    monkeypatch.setattr(paths, "restrict_permissions", refuse)
    target = tmp_path / "tracker.db"

    with pytest.raises(PermissionError, match="access denied"):
        TrackerStore.connect(target)

    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_tightening_never_makes_a_read_only_database_writable(tmp_path: Path) -> None:
    target = tmp_path / "stage.db"
    target.write_bytes(b"")
    target.chmod(0o444)

    paths.tighten_permissions(target)

    assert target.stat().st_mode & 0o777 == 0o400
