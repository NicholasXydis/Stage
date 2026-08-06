from stage.dedup.identity import (
    SOURCE_PRIORITY,
    MatchKind,
    MatchResult,
    title_canonical,
    would_merge,
)
from stage.dedup.resolve import DuplicateLink, rank, resolve_duplicates

__all__ = [
    "SOURCE_PRIORITY",
    "DuplicateLink",
    "MatchKind",
    "MatchResult",
    "rank",
    "resolve_duplicates",
    "title_canonical",
    "would_merge",
]
