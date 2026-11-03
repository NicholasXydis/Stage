import csv
import io
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Event
from typing import Any

from anyio import to_thread

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
from stage.domain.text import counted, plain_error
from stage.paths import font_path
from stage.services.query import list_jobs
from stage.storage import AsyncRepository

FORMULA_PREFIXES = ("=", "+", "-", "@")
FORMULA_GUARD = "'"
MAX_NAMES = 10_000


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


def default_filename(fmt: ExportFormat, when: datetime, stem: str = "stage-export") -> str:
    return f"{stem}-{when.astimezone():%Y%m%d}.{fmt.value}"


def export_root() -> Path:
    override = os.environ.get("STAGE_EXPORT_DIR", "").strip()
    return Path(override).expanduser() if override else Path.cwd()


def downloads_root() -> Path:
    override = os.environ.get("STAGE_EXPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def free_destination(
    root: Path, fmt: ExportFormat, when: datetime, stem: str = "stage-export"
) -> Path:
    first = root.resolve() / default_filename(fmt, when, stem)
    for number in range(1, MAX_NAMES + 1):
        suffix = f"-{number}" if number > 1 else ""
        candidate = first.with_name(f"{first.stem}{suffix}{first.suffix}")
        try:
            _create(candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ExportError(_write_failure(candidate, exc)) from exc
        return candidate
    raise ExportError(
        f"{first.parent} already holds {MAX_NAMES:,} exports from today. Move some of them out"
    )


def _create(path: Path) -> None:
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def resolve_destination(
    destination: Path | None,
    fmt: ExportFormat,
    when: datetime,
    force: bool,
    stem: str = "stage-export",
) -> Path:
    target = (destination or export_root() / default_filename(fmt, when, stem)).expanduser()
    if target.is_dir():
        target = target / default_filename(fmt, when, stem)
    target = target.resolve()
    if not target.parent.is_dir():
        raise ExportError(
            f"the folder {target.parent} does not exist. Create it or pass another --out"
        )
    if target.exists() and not force:
        raise ExportError(f"{target} already exists. Pass --force to replace it")
    return target


def _cell(value: str) -> str:
    clean = sanitize(value).replace("\n", " ").replace("\r", " ").strip()
    return " ".join(clean.split())


def _guard(value: str) -> str:
    return FORMULA_GUARD + value if value.startswith(FORMULA_PREFIXES) else value


def csv_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([_guard(_cell(value)) for value in row] for row in rows)
    return buffer.getvalue()


def markdown_table(
    title: str,
    generated_at: datetime,
    count: str,
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
) -> str:
    lines = [
        f"# {title}, {generated_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}",
        "",
        f"{count}.",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *("| " + " | ".join(_markdown_cell(value) for value in row) + " |" for row in rows),
    ]
    return "\n".join(lines) + "\n"


def _cells(jobs: Sequence[Job]) -> Iterator[list[str]]:
    return ([read(job) for _, read in COLUMNS] for job in jobs)


def render_csv(jobs: Sequence[Job]) -> str:
    return csv_table([name for name, _ in COLUMNS], _cells(jobs))


def render_json(jobs: Sequence[Job]) -> str:
    return dump([asdict(job) for job in jobs])


def _markdown_cell(value: str) -> str:
    text = _cell(value).replace("\\", "\\\\")
    for mark, safe in (("|", "\\|"), ("[", "\\["), ("]", "\\]"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(mark, safe)
    return text


def render_markdown(jobs: Sequence[Job], *, generated_at: datetime) -> str:
    names = [name for name, _ in COLUMNS]
    count = counted(len(jobs), "posting")
    return markdown_table("Stage export", generated_at, count, names, _cells(jobs))


def _write_text(target: Path, payload: str, *, encoding: str = "utf-8") -> None:
    try:
        target.write_text(payload, encoding=encoding, newline="")
    except OSError as exc:
        raise ExportError(_write_failure(target, exc)) from exc


def _write_bytes(target: Path, payload: bytes) -> None:
    try:
        target.write_bytes(payload)
    except OSError as exc:
        raise ExportError(_write_failure(target, exc)) from exc


def _write_failure(target: Path, exc: OSError) -> str:
    reason = exc.strerror or str(exc) or "the system refused"
    return (
        f"could not write {target.name} ({reason}). Check the "
        "destination is writable and has room, then try again"
    )


@contextmanager
def _replaced(target: Path) -> Iterator[Path]:
    staged = _staging(target)
    try:
        yield staged
        staged.replace(target)
    except OSError as exc:
        raise ExportError(_write_failure(target, exc)) from exc
    finally:
        staged.unlink(missing_ok=True)


def _staging(target: Path) -> Path:
    for _ in range(MAX_NAMES):
        staged = target.with_name(f".{target.name}.{secrets.token_hex(6)}.partial")
        try:
            _create(staged)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ExportError(_write_failure(target, exc)) from exc
        return staged
    raise ExportError(f"could not find a free name for a temporary file beside {target.name}")


class _RendererNotes(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def write_pdf(jobs: Sequence[Job], target: Path, *, generated_at: datetime) -> tuple[str, ...]:
    payload, notes = _pdf_payload(jobs, generated_at=generated_at)
    _write_bytes(target, payload)
    return notes


def _pdf_payload(
    jobs: Sequence[Job], *, generated_at: datetime, stop: Event | None = None
) -> tuple[bytes, tuple[str, ...]]:
    collected = _RendererNotes()
    logger = logging.getLogger(PDF_LOGGER)
    logger.addHandler(collected)
    propagates, logger.propagate = logger.propagate, False
    try:
        payload = _render_pdf(jobs, generated_at=generated_at, stop=stop or Event())
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(
            f"could not read the PDF font ({exc.strerror or exc}). Reinstall Stage, or export "
            "csv, json or md instead"
        ) from exc
    except Exception as exc:
        detail = plain_error(first_line(str(exc)))
        raise ExportError(
            f"the PDF renderer could not lay out these postings ({detail}). Export csv, json "
            "or md instead, or narrow the filters"
        ) from exc
    finally:
        logger.removeHandler(collected)
        logger.propagate = propagates
    return payload, tuple(_plain_note(message) for message in collected.messages)


def _plain_note(message: str) -> str:
    missing = re.findall(r"'(.)' \(", message) if "missing the following glyphs" in message else []
    if not missing:
        return truncate(first_line(message), 160)
    shown = " ".join(dict.fromkeys(missing[:8]))
    count = counted(len(set(missing)), "character")
    return (
        f"The PDF cannot show {count}, such as {shown}, and leaves them out. "
        "csv, json and md keep them."
    )


def _render_pdf(jobs: Sequence[Job], *, generated_at: datetime, stop: Event) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace, TTFFont

    class Pages(FPDF):
        def multi_cell(self, *args: Any, **kwargs: Any) -> Any:
            if stop.is_set():
                raise ExportError("the export was cancelled")
            return super().multi_cell(*args, **kwargs)

    reader = dict(COLUMNS)
    pdf = Pages(orientation="landscape", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_font("dejavu", style="", fname=str(font_path()))
    try:
        pdf.add_page()
        pdf.set_font("dejavu", size=14)
        pdf.cell(0, 8, _cell(f"Stage export, {generated_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}"))
        pdf.ln(10)
        pdf.set_font("dejavu", size=8)
        pdf.cell(0, 5, _cell(counted(len(jobs), "posting")))
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
        return bytes(pdf.output())
    finally:
        for font in pdf.fonts.values():
            if isinstance(font, TTFFont):
                font.close()


async def export_jobs(
    repository: AsyncRepository,
    filters: JobFilters,
    *,
    fmt: ExportFormat,
    destination: Path | None = None,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    force: bool = False,
    claimed: bool = False,
    now: datetime | None = None,
    query: str = "",
) -> ExportResult:
    moment = now or datetime.now(UTC)
    if claimed and destination is not None:
        target = destination
    else:
        target = resolve_destination(destination, fmt, moment, force)
    try:
        if query.strip():
            from stage.services.query import search_jobs

            listing = await search_jobs(
                repository, query, filters, window_days=window_days, now=moment
            )
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
                stop = Event()
                try:
                    payload, notes = await to_thread.run_sync(
                        partial(_pdf_payload, jobs, generated_at=moment, stop=stop),
                        abandon_on_cancel=True,
                    )
                finally:
                    stop.set()
                _write_bytes(staged, payload)
    except BaseException:
        if claimed:
            target.unlink(missing_ok=True)
        raise

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
    "csv_table",
    "default_filename",
    "downloads_root",
    "export_jobs",
    "free_destination",
    "markdown_table",
    "render_csv",
    "render_json",
    "render_markdown",
    "resolve_destination",
    "write_pdf",
]
