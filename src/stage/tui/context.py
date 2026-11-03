from pathlib import Path

from stage.storage import AsyncRepository


def repository_of(app: object) -> AsyncRepository | None:
    found = getattr(app, "repository", None)
    return found if isinstance(found, AsyncRepository) else None


def database_of(app: object) -> Path | None:
    found = getattr(app, "database", None)
    return found if isinstance(found, Path) else None


__all__ = ["database_of", "repository_of"]
