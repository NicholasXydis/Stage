from stage.storage import AsyncRepository


def repository_of(app: object) -> AsyncRepository | None:
    found = getattr(app, "repository", None)
    return found if isinstance(found, AsyncRepository) else None


__all__ = ["repository_of"]
