import html
import re

LONGEST_TAG = 4096

_RAW_OPEN = re.compile(rf"<(script|style)\b[^>]{{0,{LONGEST_TAG}}}>", re.IGNORECASE)
_RAW_CLOSE = {
    "script": re.compile(r"</script\s*>", re.IGNORECASE),
    "style": re.compile(r"</style\s*>", re.IGNORECASE),
}
_BLOCK_BREAKS = re.compile(r"</(p|div|li|tr|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PICTOGRAPH = "\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff"
_JOINER = "\ufe0f\u200d"
_PICTOGRAPHS = re.compile(
    f"[{_PICTOGRAPH}{_JOINER}]{{0,64}}[{_PICTOGRAPH}][{_PICTOGRAPH}{_JOINER}]{{0,64}}"
)
_BLANK_LINES = re.compile(r"\n{3,}")


def _drop_raw_text_elements(text: str) -> str:
    kept: list[str] = []
    index = 0
    unclosed: set[str] = set()
    while (opened := _RAW_OPEN.search(text, index)) is not None:
        name = opened.group(1).lower()
        closed = None if name in unclosed else _RAW_CLOSE[name].search(text, opened.end())
        if closed is None:
            unclosed.add(name)
            kept.append(text[index : opened.end()])
            index = opened.end()
            continue
        kept.append(text[index : opened.start()])
        kept.append(" ")
        index = closed.end()
    kept.append(text[index:])
    return "".join(kept)


def _drop_tags(text: str) -> str:
    kept: list[str] = []
    index = 0
    closing = text.find(">")
    while (opening := text.find("<", index)) != -1:
        if closing < opening:
            closing = text.find(">", opening + 1)
        if closing == -1:
            break
        span = closing - opening - 1
        if 1 <= span <= LONGEST_TAG:
            kept.append(text[index:opening])
            index = closing + 1
            closing = text.find(">", index)
        else:
            kept.append(text[index : opening + 1])
            index = opening + 1
    kept.append(text[index:])
    return "".join(kept)


def _strip_line_ends(text: str) -> str:
    return "\n".join(line.rstrip(" \t") for line in text.split("\n"))


def strip_html(raw: str) -> str:
    text = html.unescape(raw)
    text = _drop_raw_text_elements(text)
    text = _BLOCK_BREAKS.sub("\n", text)
    text = _drop_tags(text)
    text = html.unescape(text)
    text = _CONTROL.sub("", text)
    text = _strip_line_ends(text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def collapse_whitespace(raw: str) -> str:
    stripped = _CONTROL.sub("", raw)
    cleaned = " ".join(_PICTOGRAPHS.sub(" ", stripped).split())
    return cleaned or " ".join(stripped.split())
