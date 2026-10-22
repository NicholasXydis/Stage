import hashlib
from collections.abc import Collection, Sequence

from stage.storage import AsyncRepository

LONGEST = 80
MARK = "#"
DIGITS = 12


def short(posting_id: str) -> str:
    if len(posting_id) <= LONGEST:
        return posting_id
    parts = posting_id.split(":")
    head = ":".join(parts[:2]) if len(parts) > 2 else "posting"
    return f"{head}:{MARK}{hashlib.sha256(posting_id.encode()).hexdigest()[:DIGITS]}"


async def expand(
    repository: AsyncRepository, references: Sequence[str], tracked: Collection[str] = ()
) -> list[str]:
    found: list[str] = []
    for reference in references:
        head, marked, _ = reference.rpartition(f":{MARK}")
        if not marked or not head:
            found.append(reference)
            continue
        prefix = f"{head}:"
        candidates = [
            *await repository.ids_with_prefix(prefix),
            *(posting_id for posting_id in tracked if posting_id.startswith(prefix)),
        ]
        found.append(next((each for each in candidates if short(each) == reference), reference))
    return found


__all__ = ["LONGEST", "MARK", "expand", "short"]
