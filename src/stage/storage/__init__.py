from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from stage.storage.repository import Repository, SourceBatch, SourceBatchResult
from stage.storage.sqlite_repo import SqliteRepository
from stage.storage.writer import AsyncRepository, DatabaseWriter, WriterNotStartedError


@asynccontextmanager
async def open_repository(
    db_path: Path, *, read_only: bool = False
) -> AsyncIterator[AsyncRepository]:
    writer = DatabaseWriter(db_path, read_only=read_only)
    try:
        await writer.start()
        yield AsyncRepository(writer)
    finally:
        await writer.aclose()


__all__ = [
    "AsyncRepository",
    "DatabaseWriter",
    "Repository",
    "SourceBatch",
    "SourceBatchResult",
    "SqliteRepository",
    "WriterNotStartedError",
    "open_repository",
]
