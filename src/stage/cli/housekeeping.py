from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CAPTURE_KEEP = 50
CAPTURE_KEEP_DAYS = 30
JOURNAL_MAX_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Housekeeping:
    captures_removed: int = 0
    journal_rotated: bool = False


def _prunable(paths: list[Path], now: datetime) -> list[Path]:
    if len(paths) <= CAPTURE_KEEP:
        return []
    cutoff = now - timedelta(days=CAPTURE_KEEP_DAYS)
    ordered = sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        path
        for path in ordered[CAPTURE_KEEP:]
        if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff
    ]


def tidy(*, dry_run: bool = False, now: datetime | None = None) -> Housekeeping:
    from stage.cli.logfile import probe_journal_path
    from stage.paths import capture_dir

    moment = now or datetime.now(UTC)
    removed = 0
    captures = capture_dir()
    if captures.is_dir():
        stale = _prunable([path for path in captures.iterdir() if path.is_file()], moment)
        removed = len(stale)
        if not dry_run:
            for path in stale:
                path.unlink(missing_ok=True)

    rotated = False
    journal = probe_journal_path()
    try:
        oversized = journal.is_file() and journal.stat().st_size > JOURNAL_MAX_BYTES
    except OSError:
        oversized = False
    if oversized:
        rotated = True
        if not dry_run:
            previous = journal.with_suffix(".jsonl.1")
            previous.unlink(missing_ok=True)
            journal.replace(previous)

    return Housekeeping(captures_removed=removed, journal_rotated=rotated)


__all__ = [
    "CAPTURE_KEEP",
    "CAPTURE_KEEP_DAYS",
    "JOURNAL_MAX_BYTES",
    "Housekeeping",
    "tidy",
]
