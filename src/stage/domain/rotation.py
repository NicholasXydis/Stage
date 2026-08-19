from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RotationMember:
    key: str


@dataclass(frozen=True, slots=True)
class Rotation:
    selected: tuple[str, ...]
    deferred: tuple[str, ...]
    cursor: str
    wrapped: bool = False

    @property
    def rotating(self) -> bool:
        return bool(self.deferred)


def rotate(members: Sequence[RotationMember], *, cursor: str = "", budget: int = 0) -> Rotation:
    if budget <= 0:
        keys = tuple(member.key for member in members)
        return Rotation(selected=keys, deferred=(), cursor=cursor)

    ring = sorted(member.key for member in members)

    if budget >= len(ring):
        return Rotation(selected=tuple(ring), deferred=(), cursor="", wrapped=True)

    start = bisect_right(ring, cursor) if cursor else 0
    wrapped = start + budget > len(ring)
    taken = [ring[(start + offset) % len(ring)] for offset in range(budget)]
    remaining = [key for key in ring if key not in set(taken)]

    return Rotation(
        selected=tuple(taken),
        deferred=tuple(remaining),
        cursor=taken[-1] if taken else cursor,
        wrapped=wrapped or start >= len(ring),
    )


__all__ = ["Rotation", "RotationMember", "rotate"]
