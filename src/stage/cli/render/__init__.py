from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.markup import escape
from rich.segment import Segment, Segments
from rich.style import Style
from rich.table import Table
from rich.text import Text

from stage.domain import web_url
from stage.domain.text import first_line as _first_line
from stage.domain.text import plain_error, sentence
from stage.domain.text import sanitize as _sanitize
from stage.domain.text import summary as _summary
from stage.domain.text import truncate as _truncate


def terminal(*, stderr: bool = False) -> Console:
    return Console(emoji=False, stderr=stderr)


def splash(console: Console) -> None:
    from stage.banner import ACCENT, banner

    console.print(Text(banner(console.width), style=f"bold {ACCENT}"))


def sanitize(value: str) -> str:
    return escape(_sanitize(value))


def truncate(value: str, width: int) -> str:
    return escape(_truncate(value, width))


def first_line(value: str) -> str:
    return escape(_first_line(value))


def summary(value: str, width: int) -> str:
    return escape(_summary(value, width))


def cut(value: str, width: int) -> str:
    if value.isascii() and value.isprintable():
        return value if len(value) <= width else value[: max(width - 3, 0)] + "..."
    return _truncate(value, width)


cut_often = cache(cut)


def plain(value: str, style: str = "") -> Text:
    return Text(_sanitize(value), style=style)


def clipped(value: str, width: int, style: str = "") -> Text:
    return Text(_truncate(value, width), style=style)


def failure(exc: BaseException) -> Text:
    return Text(_sanitize(sentence(error_text(str(exc)))), style="red")


def error_text(error: str) -> str:
    return plain_error(error).replace("; ", ", ")


def error_cell(error: str, width: int) -> str:
    return summary(error_text(error), width) if error.strip() else ""


@cache
def place(raw: str) -> str:
    from stage.normalize.location import display_location

    return display_location(raw)


def rule() -> Text:
    return Text("-" * 60, style="dim")


def quoted(value: str, width: int) -> str:
    return f'"{truncate(value, width)}"'


def link(text: Text, raw: str) -> None:
    url = web_url(raw)
    if url is not None:
        text.stylize(f"link {url}")


def duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "[red]never[/red]"
    seconds = (now - when).total_seconds()
    if seconds < -60:
        return "[yellow]in the future (check this computer's clock)[/yellow]"
    return duration(max(seconds, 0)) + " ago"


def ratio(value: float | None) -> str:
    return "[dim]-[/dim]" if value is None else f"{value:.0%}"


def more(console: Console, hidden: int) -> None:
    if hidden > 0:
        console.print(f"  [dim]... and {hidden:,} more (--all lists every row)[/dim]")


def table(*columns: str, right: Sequence[str] = (), title: str | None = None) -> Table:
    shown = Table(box=None, pad_edge=False, header_style="bold", title=title)
    for column in columns:
        shown.add_column(column, justify="right" if column in right else "left", overflow="fold")
    return shown


@dataclass(frozen=True, slots=True)
class Column:
    header: str
    width: int
    right: bool = False
    crop: bool = True


@dataclass(slots=True)
class Cell:
    text: str
    style: str = ""
    url: str = ""


def grid(console: Console, columns: Sequence[Column], rows: Iterable[Sequence[Cell]]) -> None:
    if sum(column.width for column in columns) + 2 * (len(columns) - 1) > console.width:
        console.print(_table(columns, rows))
        return
    header = [Cell(column.header) for column in columns]
    if console.color_system is None:
        lines = [_line(columns, cells) for cells in (header, *rows)]
        console.print(Segments([Segment("\n".join(lines) + "\n")]), end="", soft_wrap=True)
        return
    segments = [Segment(_line(columns, header), console.get_style("bold")), Segment.line()]
    for cells in rows:
        for index, (column, cell) in enumerate(zip(columns, cells, strict=True)):
            text, padding = _cut(cell.text, column.width)
            if index:
                segments.append(Segment("  "))
            if column.right and padding:
                segments.append(Segment(padding))
            segments.append(Segment(text, _style(console, cell)))
            if not column.right and padding:
                segments.append(Segment(padding))
        segments.append(Segment.line())
    console.print(Segments(segments), end="", soft_wrap=True)


def _line(columns: Sequence[Column], cells: Sequence[Cell]) -> str:
    return "  ".join(_fit(cell.text, column) for column, cell in zip(columns, cells, strict=True))


def _cut(text: str, width: int) -> tuple[str, str]:
    size = cell_len(text)
    if size > width:
        kept = set_cell_size(text, width)
        return kept, " " * (width - cell_len(kept))
    return text, " " * (width - size)


def _fit(text: str, column: Column) -> str:
    kept, padding = _cut(text, column.width)
    return padding + kept if column.right else kept + padding


def _style(console: Console, cell: Cell) -> Style | None:
    style = console.get_style(cell.style) if cell.style else None
    url = web_url(cell.url) if cell.url else None
    if url is None:
        return style
    return Style(link=url) if style is None else style + Style(link=url)


def _table(columns: Sequence[Column], rows: Iterable[Sequence[Cell]]) -> Table:
    shown = Table(box=None, pad_edge=False, header_style="bold")
    for column in columns:
        shown.add_column(
            column.header,
            width=column.width,
            justify="right" if column.right else "left",
            no_wrap=True,
            overflow="crop" if column.crop else "fold",
        )
    for cells in rows:
        texts = []
        for cell in cells:
            text = Text(cell.text, style=cell.style)
            link(text, cell.url)
            texts.append(text)
        shown.add_row(*texts)
    return shown
