from pathlib import Path
from subprocess import CompletedProcess

import pytest

import stage.paths as paths


def test_windows_database_permissions_remove_inheritance_and_grant_only_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stage.db"
    target.touch()
    calls: list[tuple[str, ...]] = []

    def run(
        args: tuple[str, ...], *, check: bool, capture_output: bool, text: bool
    ) -> CompletedProcess[str]:
        calls.append(args)
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("stage.paths.os.name", "nt")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("USERDOMAIN", "stage-machine")
    monkeypatch.setattr("stage.paths.getpass.getuser", lambda: "stage-user")
    monkeypatch.setattr("stage.paths.subprocess.run", run)

    paths.restrict_permissions(target)

    assert calls == [
        (
            r"C:\Windows\System32\icacls.exe",
            str(target),
            "/inheritance:r",
            "/grant:r",
            r"stage-machine\stage-user:(F)",
        )
    ]


def test_windows_permission_failure_stops_database_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stage.db"
    target.touch()

    def run(
        args: tuple[str, ...], *, check: bool, capture_output: bool, text: bool
    ) -> CompletedProcess[str]:
        return CompletedProcess(args, 1, "", "access denied")

    monkeypatch.setattr("stage.paths.os.name", "nt")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("USERDOMAIN", "stage-machine")
    monkeypatch.setattr("stage.paths.subprocess.run", run)

    with pytest.raises(PermissionError, match="access denied"):
        paths.restrict_permissions(target)
