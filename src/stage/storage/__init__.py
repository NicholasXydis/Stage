from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from stage.storage.repository import Repository, SourceBatch, SourceBatchResult
from stage.storage.sqlite_repo import SqliteRepository
from stage.storage.writer import AsyncRepository, DatabaseWriter, WriterNotStartedError


@asynccontextmanager
async def open_repository(db_path: Path) -> AsyncIterator[AsyncRepository]:
    writer = DatabaseWriter(db_path)
    await writer.start()
    try:
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
