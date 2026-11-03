import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from stage.companies import PERSONAL_LIMIT, PersonalBoards, personal_kept
from stage.domain import Company, ProbeResult, ProbeVerdict
from stage.services.discover import candidate_for, gone_reason, is_gone
from stage.storage import AsyncRepository

STRIKE_GAP_DAYS = 6
BARREN_AFTER_DAYS = 365
RECENT_DAYS = 7
ANSWERED = frozenset({ProbeVerdict.MATCH, ProbeVerdict.UNVERIFIED, ProbeVerdict.EMPTY})
_STRIKE = re.compile(r"^\d{4}-\d{2}-\d{2}: switched off, ")


@dataclass(frozen=True, slots=True)
class PruneReport:
    verified: int = 0
    switched_off: tuple[str, ...] = ()
    back_on: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    barren: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardListSummary:
    path: Path
    boards: int = 0
    enabled: int = 0
    disabled: int = 0
    recent: int = 0
    shipped: int = 0
    problem: str = ""


def _lines(company: Company) -> list[str]:
    return (company.notes or "").splitlines()


def added_on(company: Company) -> date | None:
    lines = _lines(company)
    if not lines or _STRIKE.match(lines[0]):
        return None
    try:
        return date.fromisoformat(lines[0][:10])
    except ValueError:
        return None


def struck_on(company: Company) -> date | None:
    lines = _lines(company)
    if company.enabled or not lines or not _STRIKE.match(lines[-1]):
        return None
    return date.fromisoformat(lines[-1][:10])


def _struck(company: Company, today: date, result: ProbeResult) -> Company:
    line = (
        f"{today.isoformat()}: switched off, {gone_reason(result)}. Removed if still gone next week"
    )
    return replace(company, enabled=False, notes="\n".join([*_lines(company), line]))


def _revived(company: Company, today: date) -> Company:
    kept = [line for line in _lines(company) if not _STRIKE.match(line)]
    return replace(company, enabled=True, last_verified=today, notes="\n".join(kept) or None)


def room_left(rows: Sequence[Company], packaged: Sequence[Company]) -> int:
    live = sum(1 for row in personal_kept(packaged, rows) if row.enabled)
    return max(0, PERSONAL_LIMIT - live)


def prune_personal(
    rows: Sequence[Company],
    results: Sequence[ProbeResult],
    *,
    today: date,
    productive: Collection[str],
) -> tuple[tuple[Company, ...], PruneReport]:
    answers = {(result.company, result.candidate.label): result for result in results}
    barren_before = today - timedelta(days=BARREN_AFTER_DAYS)
    strike_before = today - timedelta(days=STRIKE_GAP_DAYS)
    kept: list[Company] = []
    verified = 0
    switched_off: list[str] = []
    back_on: list[str] = []
    removed: list[str] = []
    barren: list[str] = []
    for row in rows:
        added = added_on(row)
        if added is not None and added <= barren_before and row.registry_key not in productive:
            barren.append(row.name)
            continue
        result = answers.get((row.name, candidate_for(row).label))
        struck = struck_on(row)
        if result is not None and is_gone(result):
            if struck is None:
                if row.enabled:
                    switched_off.append(row.name)
                kept.append(_struck(row, today, result))
            elif struck <= strike_before:
                removed.append(row.name)
            else:
                kept.append(row)
        elif result is None or result.verdict not in ANSWERED:
            kept.append(row)
        elif struck is not None:
            back_on.append(row.name)
            kept.append(_revived(row, today))
        else:
            if row.last_verified != today:
                verified += 1
            kept.append(replace(row, last_verified=today))
    return tuple(kept), PruneReport(
        verified=verified,
        switched_off=tuple(switched_off),
        back_on=tuple(back_on),
        removed=tuple(removed),
        barren=tuple(barren),
    )


async def productive_boards(repository: AsyncRepository, rows: Sequence[Company]) -> frozenset[str]:
    from stage.services.coverage import board_of

    boards = {row.registry_key: board_of(row) for row in rows}
    found = await repository.boards_with_postings(sorted(set(boards.values())))
    return frozenset(key for key, board in boards.items() if board in found)


def summarize(
    personal: PersonalBoards, packaged: Sequence[Company], today: date
) -> BoardListSummary:
    if personal.problem:
        return BoardListSummary(path=personal.path, problem=personal.problem)
    recent = today - timedelta(days=RECENT_DAYS)
    kept = personal_kept(packaged, personal.rows)
    return BoardListSummary(
        path=personal.path,
        boards=len(personal.rows),
        enabled=sum(1 for row in kept if row.enabled),
        disabled=sum(1 for row in personal.rows if not row.enabled),
        recent=sum(1 for row in personal.rows if (added_on(row) or date.min) > recent),
        shipped=len(personal.rows) - len(kept),
    )


__all__ = [
    "BARREN_AFTER_DAYS",
    "STRIKE_GAP_DAYS",
    "BoardListSummary",
    "PruneReport",
    "added_on",
    "productive_boards",
    "prune_personal",
    "room_left",
    "struck_on",
    "summarize",
]
