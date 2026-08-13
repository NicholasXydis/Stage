from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from stage.companies import board_label
from stage.domain import (
    STALE_AFTER_DAYS,
    Company,
    CoverageClassification,
    CoverageRow,
    CoverageState,
    SourceVisit,
    UnregisteredCompany,
    coverage_state,
)
from stage.services.discover import GENERIC_TOKENS, name_matches, name_tokens
from stage.storage import AsyncRepository


@dataclass(frozen=True, slots=True)
class CoverageReport:
    rows: tuple[CoverageRow, ...]
    unregistered: tuple[UnregisteredCompany, ...]
    classifications: tuple[CoverageClassification, ...]
    enabled: int
    disabled: int
    stale_after_days: int

    @property
    def gaps(self) -> tuple[CoverageRow, ...]:
        return tuple(row for row in self.rows if row.is_gap)

    @property
    def producing(self) -> tuple[CoverageRow, ...]:
        return tuple(row for row in self.rows if row.state is CoverageState.PRODUCING)


def _board_key(company: Company) -> str | None:
    from stage.sources import adapter_for_platform

    adapter = adapter_for_platform(company.platform)
    if adapter is None:
        return None
    try:
        return adapter.board_key(company)
    except Exception:
        return None


def _registry_rows(
    companies: Sequence[Company],
    postings: dict[str, int],
    visits: dict[tuple[str, str], SourceVisit],
    now: datetime,
    stale_after_days: int,
) -> tuple[CoverageRow, ...]:
    rows: list[CoverageRow] = []
    for company in companies:
        board = _board_key(company)
        if board is None:
            rows.append(
                CoverageRow(
                    company=company.name,
                    platform=company.platform.value,
                    board=board_label(company),
                    state=CoverageState.UNROUTABLE,
                    postings=0,
                    last_success_at=None,
                    consecutive_failures=0,
                    last_error="",
                )
            )
            continue
        source, _, _ = board.partition(":")
        visit = visits.get((source, board))
        count = postings.get(board, 0)
        rows.append(
            CoverageRow(
                company=company.name,
                platform=company.platform.value,
                board=board,
                state=coverage_state(
                    count,
                    visit is not None,
                    visit.last_success_at if visit else None,
                    visit.consecutive_failures if visit else 0,
                    now,
                    stale_after_days,
                ),
                postings=count,
                last_success_at=visit.last_success_at if visit else None,
                consecutive_failures=visit.consecutive_failures if visit else 0,
                last_error=visit.last_error if visit else "",
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.state.value, row.company.lower())))


def _unregistered(
    companies: Sequence[Company],
    seen: dict[str, dict[str, int]],
    classifications: Sequence[CoverageClassification],
) -> tuple[UnregisteredCompany, ...]:
    index: dict[str, list[str]] = {}
    for company in companies:
        for token in name_tokens(company.name) - GENERIC_TOKENS:
            index.setdefault(token, []).append(company.name)

    unknown: list[UnregisteredCompany] = []
    for name, sources in seen.items():
        tokens = name_tokens(name)
        nearby = {
            candidate for token in tokens - GENERIC_TOKENS for candidate in index.get(token, ())
        }
        if any(name_matches(candidate, name) for candidate in nearby):
            continue
        if any(name_matches(entry.company, name) for entry in classifications):
            continue
        unknown.append(
            UnregisteredCompany(
                company=name,
                sources=tuple(sorted(sources)),
                postings=sum(sources.values()),
            )
        )
    return tuple(sorted(unknown, key=lambda row: (-row.postings, row.company.lower())))


async def coverage(
    repository: AsyncRepository,
    companies: Sequence[Company],
    *,
    now: datetime | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
    unregistered: bool = False,
) -> CoverageReport:
    moment = now or datetime.now(UTC)
    enabled = [company for company in companies if company.enabled]
    postings = await repository.board_counts()
    visits = {(visit.source, visit.board): visit for visit in await repository.all_visits()}

    seen = await repository.company_counts() if unregistered else {}
    classifications = await repository.coverage_classifications()
    return CoverageReport(
        rows=_registry_rows(enabled, postings, visits, moment, stale_after_days),
        unregistered=_unregistered(companies, seen, classifications) if unregistered else (),
        classifications=tuple(classifications),
        enabled=len(enabled),
        disabled=len(companies) - len(enabled),
        stale_after_days=stale_after_days,
    )


__all__ = ["CoverageReport", "coverage"]
