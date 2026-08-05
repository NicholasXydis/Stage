import html
import re

_SCRIPTS = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_BREAKS = re.compile(r"</(p|div|li|tr|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+(?=\n)")


def strip_html(raw: str) -> str:
    text = html.unescape(raw)
    text = _SCRIPTS.sub(" ", text)
    text = _BLOCK_BREAKS.sub("\n", text)
    text = _TAGS.sub("", text)
    text = html.unescape(text)
    text = _CONTROL.sub("", text)
    text = _TRAILING_SPACE.sub("", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def collapse_whitespace(raw: str) -> str:
    return " ".join(_CONTROL.sub("", raw).split())
