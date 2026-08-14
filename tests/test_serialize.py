import sys
from io import BytesIO, TextIOWrapper

import pytest

from stage.cli import serialize


def test_emit_writes_utf8_when_stdout_has_a_binary_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)

    serialize.emit('{"title":"東京"}')

    assert buffer.getvalue() == '{"title":"東京"}\n'.encode()
