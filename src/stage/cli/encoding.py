import codecs
from typing import Protocol, cast


class _ReconfigurableOutput(Protocol):
    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> object:
        pass


def configure_terminal_output(stdout: object, platform: str) -> None:
    if not hasattr(stdout, "reconfigure"):
        return
    output = cast(_ReconfigurableOutput, stdout)
    if platform == "win32":
        output.reconfigure(encoding="utf-8")
    elif _encoding_name(getattr(stdout, "encoding", None)) != "utf-8":
        output.reconfigure(errors="replace")


def _encoding_name(encoding: object) -> str:
    try:
        return codecs.lookup(str(encoding)).name
    except LookupError:
        return ""
