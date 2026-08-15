import sys
from io import BytesIO, TextIOWrapper

import pytest

from stage.cli import serialize


class _ReconfigurableOutput:
    def __init__(self) -> None:
        self.encoding: str | None = None

    def reconfigure(self, *, encoding: str) -> None:
        self.encoding = encoding


def test_configure_terminal_output_uses_utf8_on_windows() -> None:
    stdout = _ReconfigurableOutput()

    serialize.configure_terminal_output(stdout, "win32")

    assert stdout.encoding == "utf-8"


def test_configure_terminal_output_leaves_non_windows_stdout_unchanged() -> None:
    stdout = _ReconfigurableOutput()

    serialize.configure_terminal_output(stdout, "linux")

    assert stdout.encoding is None


def test_emit_writes_utf8_when_stdout_has_a_binary_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)

    serialize.emit('{"title":"東京"}')

    assert buffer.getvalue() == '{"title":"東京"}\n'.encode()
