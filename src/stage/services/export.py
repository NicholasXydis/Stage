import csv
import io
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from stage.domain import (
    DEFAULT_WINDOW_DAYS,
    ExportFormat,
    Job,
    JobFilters,
    dump,
    first_line,
    sanitize,
    truncate,
)
from stage.paths import font_path
from stage.services.query import list_jobs
from stage.storage import AsyncRepository

FORMULA_PREFIXES = ("=", "+", "-", "@")
FORMULA_GUARD = "'"


def _place(raw: str) -> str:
    from stage.normalize.location import display_location

    return display_location(raw)


COLUMNS: tuple[tuple[str, Callable[[Job], str]], ...] = (
    ("company", lambda job: job.company),
    ("title", lambda job: job.title_raw),
    ("location", lambda job: _place(job.location_raw)),
    ("location_bucket", lambda job: job.location.value),
    ("term", lambda job: job.term),
    ("role", lambda job: job.role.value),
    ("language", lambda job: job.language.value),
    ("first_seen", lambda job: job.first_seen.astimezone(UTC).date().isoformat()),
    ("source", lambda job: job.source),
    ("apply_url", lambda job: job.apply_url_raw),
    ("id", lambda job: job.id),
)

PDF_COLUMNS = ("company", "title", "location", "term", "first_seen")
PDF_WIDTHS = (26, 46, 22, 14, 14)
PDF_CELL_LIMIT = 300
PDF_LOGGER = "fpdf"


class ExportError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    fmt: ExportFormat
    count: int
    total_matching: int
    notes: tuple[str, ...] = ()


def default_filename(fmt: ExportFormat, when: datetime) -> str:
    return f"stage-export-{when.astimezone(UTC):%Y%m%d}.{fmt.value}"


def export_root() -> Path:
    import os

    override = os.environ.get("STAGE_EXPORT_DIR", "").strip()
    return Path(override).expanduser() if override else Path.cwd()


def resolve_destination(
    destination: Path | None, fmt: ExportFormat, when: datetime, force: bool
) -> Path:
    target = (destination or export_root() / default_filename(fmt, when)).expanduser()
    if target.is_dir():
        target = target / default_filename(fmt, when)
    target = target.resolve()
    if not target.parent.is_dir():
        raise ExportError(f"{target.parent} does not exist — create it or pass another --out")
    if target.exists() and not force:
        raise ExportError(f"{target} already exists — pass --force to overwrite it")
    return target


def _cell(value: str) -> str:
    clean = sanitize(value).replace("\n", " ").replace("\r", " ").strip()
    return " ".join(clean.split())


def _guard(value: str) -> str:
    return FORMULA_GUARD + value if value.startswith(FORMULA_PREFIXES) else value


def render_csv(jobs: Sequence[Job]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([name for name, _ in COLUMNS])
    for job in jobs:
        writer.writerow([_guard(_cell(read(job))) for _, read in COLUMNS])
    return buffer.getvalue()


def render_json(jobs: Sequence[Job]) -> str:
    return dump([asdict(job) for job in jobs])


def _markdown_cell(value: str) -> str:
    return _cell(value).replace("\\", "\\\\").replace("|", "\\|")


def render_markdown(jobs: Sequence[Job], *, generated_at: datetime) -> str:
    names = [name for name, _ in COLUMNS]
    lines = [
        f"# Stage export — {generated_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}",
        "",
        f"{len(jobs)} posting(s).",
        "",
        "| " + " | ".join(names) + " |",
        "| " + " | ".join("---" for _ in names) + " |",
    ]
    for job in jobs:
        cells = [_markdown_cell(read(job)) for _, read in COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _write_text(target: Path, payload: str, *, encoding: str = "utf-8") -> None:
    try:
        target.write_text(payload, encoding=encoding, newline="")
    except OSError as exc:
        raise ExportError(_write_failure(target, exc)) from exc


def _write_failure(target: Path, exc: OSError) -> str:
    return (
        f"could not write {target.name} ({exc.strerror or type(exc).__name__}). Check the "
        "destination is writable and has room, then try again"
    )


@contextmanager
def _replaced(target: Path) -> Iterator[Path]:
    staged = target.with_name(f"{target.name}.partial")
    try:
        yield staged
        staged.replace(target)
    except OSError as exc:
        raise ExportError(_write_failure(target, exc)) from exc
    finally:
        staged.unlink(missing_ok=True)


class _RendererNotes(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def write_pdf(jobs: Sequence[Job], target: Path, *, generated_at: datetime) -> tuple[str, ...]:
    collected = _RendererNotes()
    logger = logging.getLogger(PDF_LOGGER)
    logger.addHandler(collected)
    try:
        _render_pdf(jobs, target, generated_at=generated_at)
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(_write_failure(target, exc)) from exc
    except Exception as exc:
        raise ExportError(
            f"the PDF renderer refused this page ({type(exc).__name__}: {first_line(str(exc))}). "
            "Export csv, json or md instead, or narrow the filters"
        ) from exc
    finally:
        logger.removeHandler(collected)
    return tuple(truncate(first_line(message), 160) for message in collected.messages)


def _render_pdf(jobs: Sequence[Job], target: Path, *, generated_at: datetime) -> None:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    reader = dict(COLUMNS)
    pdf = FPDF(orientation="landscape", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_font("dejavu", style="", fname=str(font_path()))
    pdf.add_page()
    pdf.set_font("dejavu", size=14)
    pdf.cell(0, 8, _cell(f"Stage export — {generated_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}"))
    pdf.ln(10)
    pdf.set_font("dejavu", size=8)
    pdf.cell(0, 5, _cell(f"{len(jobs)} posting(s)"))
    pdf.ln(8)

    with pdf.table(
        col_widths=PDF_WIDTHS,
        line_height=5,
        text_align="LEFT",
        headings_style=FontFace(emphasis="", fill_color=(232, 232, 232)),
    ) as table:
        header = table.row()
        for name in PDF_COLUMNS:
            header.cell(name)
        for job in jobs:
            row = table.row()
            for name in PDF_COLUMNS:
                row.cell(truncate(_cell(reader[name](job)), PDF_CELL_LIMIT))
    pdf.output(str(target))


async def export_jobs(
    repository: AsyncRepository,
    filters: JobFilters,
    *,
    fmt: ExportFormat,
    destination: Path | None = None,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    force: bool = False,
    now: datetime | None = None,
    query: str = "",
) -> ExportResult:
    moment = now or datetime.now(UTC)
    target = resolve_destination(destination, fmt, moment, force)
    if query.strip():
        from stage.services.query import search_jobs

        listing = await search_jobs(repository, query, filters, window_days=window_days, now=moment)
    else:
        listing = await list_jobs(repository, filters, window_days=window_days, now=moment)
    jobs = listing.jobs

    notes: tuple[str, ...] = ()
    with _replaced(target) as staged:
        if fmt is ExportFormat.CSV:
            _write_text(staged, render_csv(jobs), encoding="utf-8-sig")
        elif fmt is ExportFormat.JSON:
            _write_text(staged, render_json(jobs))
        elif fmt is ExportFormat.MD:
            _write_text(staged, render_markdown(jobs, generated_at=moment))
        else:
            notes = write_pdf(jobs, staged, generated_at=moment)

    return ExportResult(
        path=target,
        fmt=fmt,
        count=len(jobs),
        total_matching=listing.total_matching,
        notes=notes,
    )


__all__ = [
    "COLUMNS",
    "FORMULA_PREFIXES",
    "ExportError",
    "ExportResult",
    "default_filename",
    "export_jobs",
    "render_csv",
    "render_json",
    "render_markdown",
    "resolve_destination",
    "write_pdf",
]
