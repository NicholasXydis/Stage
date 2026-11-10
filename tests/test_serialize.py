import sys
from io import BytesIO, TextIOWrapper

import pytest

from stage.cli import serialize
from stage.cli.encoding import configure_terminal_output


class _ReconfigurableOutput:
    def __init__(self, encoding: str | None = None) -> None:
        self.encoding = encoding
        self.errors: str | None = None

    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None:
        self.encoding = encoding or self.encoding
        self.errors = errors or self.errors


def test_configure_terminal_output_uses_utf8_on_windows() -> None:
    stdout = _ReconfigurableOutput()

    configure_terminal_output(stdout, "win32")

    assert stdout.encoding == "utf-8"


def test_configure_terminal_output_leaves_utf8_stdout_unchanged() -> None:
    stdout = _ReconfigurableOutput("UTF-8")

    configure_terminal_output(stdout, "linux")

    assert (stdout.encoding, stdout.errors) == ("UTF-8", None)


@pytest.mark.parametrize("encoding", ["ascii", "ANSI_X3.4-1968", "latin-1", None, "no-such-codec"])
def test_configure_terminal_output_replaces_what_a_narrow_terminal_cannot_show(
    encoding: str | None,
) -> None:
    stdout = _ReconfigurableOutput(encoding)

    configure_terminal_output(stdout, "linux")

    assert stdout.errors == "replace"


def test_a_plain_ascii_terminal_prints_accents_as_question_marks() -> None:
    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="ascii")

    configure_terminal_output(stdout, "darwin")
    stdout.write("Développeur")
    stdout.flush()

    assert buffer.getvalue() == b"D?veloppeur"


def test_emit_writes_utf8_when_stdout_has_a_binary_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)

    serialize.emit('{"title":"東京"}')

    assert buffer.getvalue() == '{"title":"東京"}\n'.encode()


def test_the_command_line_sets_up_a_narrow_terminal_on_every_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stage.cli import options

    buffer = BytesIO()
    stdout = TextIOWrapper(buffer, encoding="ascii", newline="\n")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(options, "app", lambda: print("Développeur"))

    options.main()
    stdout.flush()

    assert buffer.getvalue() == b"D?veloppeur\n"
