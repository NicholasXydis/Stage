from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

MAX_LOG_BYTES = 5 * 1024 * 1024
GENERATIONS = 3


def rotate(path: Path, *, max_bytes: int = MAX_LOG_BYTES, generations: int = GENERATIONS) -> bool:
    if generations < 1:
        raise ValueError("a rotated log needs at least one generation")
    if not path.exists() or path.stat().st_size < max_bytes:
        return False

    oldest = path.with_name(f"{path.name}.{generations}")
    oldest.unlink(missing_ok=True)
    for index in range(generations - 1, 0, -1):
        candidate = path.with_name(f"{path.name}.{index}")
        if candidate.exists():
            candidate.rename(path.with_name(f"{path.name}.{index + 1}"))
    path.rename(path.with_name(f"{path.name}.1"))
    return True


@contextmanager
def open_request_log(
    path: Path, *, max_bytes: int = MAX_LOG_BYTES, generations: int = GENERATIONS
) -> Iterator[TextIO]:
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    rotate(resolved, max_bytes=max_bytes, generations=generations)
    with resolved.open("a", encoding="utf-8") as stream:
        yield stream


def probe_journal_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / "probe-journal.jsonl"


@contextmanager
def open_probe_journal(path: Path | None = None) -> Iterator[Callable[[object], None]]:
    import json

    resolved = (path or probe_journal_path()).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    rotate(resolved)
    with resolved.open("a", encoding="utf-8") as stream:

        def record(entry: object) -> None:
            stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            stream.flush()

        yield record
