from time import monotonic
from weakref import WeakKeyDictionary

from rich.text import Text

from stage.domain.text import cells, counted, one_line, plain_error, sanitize, truncate

REPEAT_SECONDS = 3.0
_TOLD: WeakKeyDictionary[object, tuple[object, float]] = WeakKeyDictionary()


def cell(value: str, width: int = 0) -> Text:
    return Text(truncate(value, width) if width else one_line(value), no_wrap=True)


def _escaped(text: str) -> str:
    escaped = text.replace("[", "\\[")
    return f"{escaped} " if escaped.endswith("\\") else escaped


def quoted(value: str, width: int = 0) -> str:
    clean = sanitize(value)
    return _escaped(clean[:width] if width else clean)


def inline(value: str, width: int = 0) -> str:
    return _escaped(truncate(value, width) if width else one_line(value))


def headline(value: str, width: int, lines: int = 2) -> str:
    words = one_line(value).split(" ")
    rows: list[str] = []
    for index, word in enumerate(words):
        joined = f"{rows[-1]} {word}" if rows else word
        if rows and cells(joined) <= width:
            rows[-1] = joined
        elif len(rows) == lines:
            rows[-1] = " ".join((rows[-1], *words[index:]))
            break
        else:
            rows.append(word)
    return "\n".join(inline(row, width) for row in rows)


def sentence(message: str) -> str:
    text = plain_error(one_line(message)).strip()
    first = text.split(" ", 1)[0]
    if first.isalpha() and first.islower():
        text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else f"{text}."


def told(
    screen: object, message: str, severity: str = "information", *, subject: object = None
) -> None:
    text = sanitize(message)
    now = monotonic()
    key = (text, subject)
    last = _TOLD.get(screen)
    if last is not None and last[0] == key and now - last[1] < REPEAT_SECONDS:
        return
    _TOLD[screen] = (key, now)
    screen.notify(text, severity=severity, markup=False)  # type: ignore[attr-defined]


__all__ = ["cell", "counted", "headline", "inline", "quoted", "sentence", "told"]
