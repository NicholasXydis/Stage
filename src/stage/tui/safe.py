from time import monotonic
from weakref import WeakKeyDictionary

from rich.text import Text

from stage.domain.text import counted, sanitize, truncate

REPEAT_SECONDS = 3.0
_TOLD: WeakKeyDictionary[object, tuple[str, float]] = WeakKeyDictionary()


def cell(value: str, width: int = 0) -> Text:
    return Text(truncate(value, width) if width else sanitize(value), no_wrap=True)


def quoted(value: str, width: int = 0) -> str:
    clean = sanitize(value)
    return (clean[:width] if width else clean).replace("[", "\\[")


def told(screen: object, message: str, severity: str = "information") -> None:
    text = sanitize(message)
    now = monotonic()
    last = _TOLD.get(screen)
    if last is not None and last[0] == text and now - last[1] < REPEAT_SECONDS:
        return
    _TOLD[screen] = (text, now)
    screen.notify(text, severity=severity, markup=False)  # type: ignore[attr-defined]


__all__ = ["cell", "counted", "quoted", "told"]
