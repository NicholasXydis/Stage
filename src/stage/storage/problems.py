from pathlib import Path

from stage.domain.text import plain_error

UPGRADE = "Upgrade Stage (uv tool upgrade stage-cli), then try again."


def problem_kind(exc: BaseException) -> str:
    said = str(exc).lower()
    if "locked" in said or "busy" in said:
        return "busy"
    if "full" in said:
        return "full"
    if any(word in said for word in ("readonly", "read-only", "unable to open", "permission")):
        return "read-only"
    return "damaged"


def no_database(path: Path) -> str:
    return (
        f"There is no Stage database at {path}. Check the path, or run "
        f"stage sync --db {path} to start one there."
    )


def newer_database(path: Path) -> str:
    return f"The database at {path} was written by a newer version of Stage. {UPGRADE}"


def database_problem(exc: BaseException, path: Path) -> str:
    match problem_kind(exc):
        case "busy":
            return (
                f"The database at {path} is busy with another Stage process, such as a sync. "
                "Try again when it finishes."
            )
        case "full":
            return (
                f"The disk holding the database at {path} is full. Free some space and try "
                "again (the database itself is fine)."
            )
        case "read-only":
            return (
                f"Stage cannot write to the database at {path}. Check that the file and its "
                "folder are yours and writable."
            )
    return (
        f"The database at {path} is damaged or is not a Stage database. Move it aside and "
        "run stage sync to start a new one."
    )


def not_saved(exc: BaseException) -> str:
    match problem_kind(exc):
        case "busy":
            return (
                "Another Stage process held the database, so these postings were not saved. "
                "The next sync saves them."
            )
        case "full":
            return (
                "The disk holding the database is full, so these postings were not saved. "
                "Free some space and run stage sync again."
            )
        case "read-only":
            return (
                "Stage cannot write to the database, so these postings were not saved. Check "
                "that the file and its folder are yours and writable."
            )
    return (
        f"Stage could not save these postings ({plain_error(str(exc))}). Run stage doctor "
        "to check the database."
    )
