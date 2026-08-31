from rich.markup import escape
from rich.text import Text

from stage.domain.text import sanitize


def cell(value: str, width: int = 0) -> Text:
    clean = sanitize(value)
    return Text(clean[:width] if width else clean, no_wrap=True)


def quoted(value: str, width: int = 0) -> str:
    clean = sanitize(value)
    return escape(clean[:width] if width else clean)


def told(screen: object, message: str, severity: str = "information") -> None:
    screen.notify(sanitize(message), severity=severity, markup=False)  # type: ignore[attr-defined]


__all__ = ["cell", "quoted", "told"]
