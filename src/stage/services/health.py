from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from stage.domain import (
    STALE_AFTER_DAYS,
    Company,
    IntegrityFinding,
    JobFilters,
    RateState,
    SourceRunStats,
    SourceVisit,
    SyncRun,
    VisitState,
    VolumeSignal,
    WorkdayCrawl,
    assess_volume,
    classify_visit,
)
from stage.domain.text import plain_error
from stage.domain.visits import refused
from stage.storage import AsyncRepository

RUN_HISTORY = 20
COMPOSITION_COLUMNS = ("source", "location", "role", "term", "language")


@dataclass(frozen=True, slots=True)
class BoardHealth:
    source: str
    board: str
    label: str
    state: VisitState
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str

    @property
    def error(self) -> str:
        return plain_error(self.last_error) if self.last_error else ""

    @property
    def breaker_open(self) -> bool:
        from stage.http import skipped_by_breaker

        return skipped_by_breaker(self.last_error)


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    stored: int
    volume: VolumeSignal
    requests: int
    not_modified: int
    latency_p50_ms: float
    latency_p95_ms: float
    errors: int
    tightenings: int
    deferred: int
    blocked: bool
    boards: tuple[BoardHealth, ...] = ()

    @property
    def cache_hit_ratio(self) -> float | None:
        if self.requests <= 0:
            return None
        return self.not_modified / self.requests

    @property
    def success_rate(self) -> float | None:
        if not self.boards:
            return None
        failed = (VisitState.FAILING, VisitState.REFUSING, VisitState.GONE)
        succeeded = sum(1 for board in self.boards if board.state not in failed)
        return succeeded / len(self.boards)

    @property
    def failing_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for board in self.boards if board.state is VisitState.FAILING)

    @property
    def stale_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for board in self.boards if board.state is VisitState.STALE)

    @property
    def refusing_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for board in self.boards if board.state is VisitState.REFUSING)

    @property
    def gone_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for board in self.boards if board.state is VisitState.GONE)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    schema_version: int
    last_sync_at: datetime | None
    integrity: tuple[IntegrityFinding, ...]
    sources: tuple[SourceHealth, ...]
    blocks: tuple[RateState, ...]
    never_synced: bool
    stale_after_days: int = STALE_AFTER_DAYS
    due_for_recheck: tuple[str, ...] = ()
    workday_crawls: tuple[WorkdayCrawl, ...] = ()

    @property
    def integrity_problems(self) -> tuple[IntegrityFinding, ...]:
        return tuple(finding for finding in self.integrity if not finding.is_clean)

    @property
    def volume_alerts(self) -> tuple[SourceHealth, ...]:
        return tuple(source for source in self.sources if source.volume.is_alert)

    @property
    def failing_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for source in self.sources for board in source.failing_boards)

    @property
    def stale_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for source in self.sources for board in source.stale_boards)

    @property
    def refusing_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for source in self.sources for board in source.refusing_boards)

    @property
    def gone_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for source in self.sources for board in source.gone_boards)

    @property
    def resting_blocks(self) -> tuple[RateState, ...]:
        errors = [board.last_error for board in self.refusing_boards]
        return tuple(
            state
            for state in self.blocks
            if refused(state.reason) and any(state.bucket in error for error in errors)
        )

    @property
    def warning_blocks(self) -> tuple[RateState, ...]:
        resting = {state.bucket for state in self.resting_blocks}
        return tuple(state for state in self.blocks if state.bucket not in resting)

    @property
    def warnings(self) -> int:
        return (
            len(self.failing_boards)
            + len(self.stale_boards)
            + len(self.due_for_recheck)
            + len(self.volume_alerts)
            + len(self.warning_blocks)
        )

    @property
    def is_healthy(self) -> bool:
        return not self.integrity_problems


@dataclass(frozen=True, slots=True)
class StatsReport:
    runs: tuple[SyncRun, ...]
    composition: dict[str, dict[str, int]]
    total_jobs: int
    duplicates: int
    quarantined: dict[str, int]
    tombstones: int
    cached_urls: int
    schema_version: int


def _live_boards(companies: Sequence[Company]) -> frozenset[str]:
    from stage.services.sync import _registry_boards

    return frozenset(_registry_boards([row for row in companies if row.enabled])[1])


async def _board_health(
    repository: AsyncRepository,
    now: datetime,
    stale_after_days: int,
    companies: Sequence[Company] = (),
) -> dict[str, list[BoardHealth]]:
    visits: list[SourceVisit] = await repository.all_visits()
    live = _live_boards(companies)
    grouped: dict[str, list[BoardHealth]] = {}
    for visit in visits:
        if live and visit.board not in live:
            continue
        grouped.setdefault(visit.source, []).append(
            BoardHealth(
                source=visit.source,
                board=visit.board,
                label=visit.label or visit.board,
                state=classify_visit(
                    visit.last_success_at,
                    visit.consecutive_failures,
                    now,
                    stale_after_days,
                    refusing=visit.refusing,
                    gone=visit.gone,
                ),
                last_success_at=visit.last_success_at,
                consecutive_failures=visit.consecutive_failures,
                last_error=visit.last_error,
            )
        )
    return grouped


async def doctor(
    repository: AsyncRepository,
    *,
    now: datetime | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
    history: int = RUN_HISTORY,
    companies: Sequence[Company] = (),
    whole_file: bool = False,
) -> DoctorReport:
    moment = now or datetime.now(UTC)
    runs = await repository.run_history(history)
    volumes = await repository.volume_history(history, await repository.rules_changed_at())
    stored = await repository.stored_counts()
    boards = await _board_health(repository, moment, stale_after_days, companies)
    rate_state = await repository.load_rate_state()
    workday_crawls = await repository.load_workday_crawls()

    latest: dict[str, SourceRunStats] = {}
    for run in runs:
        for stats in run.sources:
            latest.setdefault(stats.source, stats)
    paused = _paused_feeds()
    names = sorted(
        name
        for name in set(volumes) | set(stored) | set(latest) | set(boards)
        if name not in paused or stored.get(name, 0)
    )

    sources: list[SourceHealth] = []
    for name in names:
        last = latest.get(name) or SourceRunStats(source=name)
        sources.append(
            SourceHealth(
                source=name,
                stored=stored.get(name, 0),
                volume=assess_volume(name, list(volumes.get(name, ()))),
                requests=last.requests,
                not_modified=last.not_modified,
                latency_p50_ms=last.latency_p50_ms,
                latency_p95_ms=last.latency_p95_ms,
                errors=last.errors,
                tightenings=last.tightenings,
                deferred=last.deferred,
                blocked=last.blocked,
                boards=tuple(boards.get(name, ())),
            )
        )

    return DoctorReport(
        schema_version=await repository.schema_version(),
        last_sync_at=await repository.last_sync_at(),
        integrity=tuple(await repository.integrity_findings(whole_file=whole_file)),
        sources=tuple(sources),
        blocks=tuple(
            state
            for state in rate_state.values()
            if state.is_blocked(moment) and state.bucket not in paused
        ),
        never_synced=not runs,
        stale_after_days=stale_after_days,
        due_for_recheck=tuple(
            sorted(
                f"{company.name} ({company.recheck_after})"
                for company in companies
                if company.due_for_recheck(moment.date())
            )
        ),
        workday_crawls=tuple(crawl for _, crawl in sorted(workday_crawls.items())),
    )


async def statistics(repository: AsyncRepository, *, history: int = RUN_HISTORY) -> StatsReport:
    composition = {column: await repository.composition(column) for column in COMPOSITION_COLUMNS}
    return StatsReport(
        runs=tuple(await repository.run_history(history)),
        composition=composition,
        total_jobs=await repository.count_jobs(JobFilters(status=None, limit=0)),
        duplicates=await repository.count_duplicates(),
        quarantined=await repository.quarantine_reason_counts(),
        tombstones=await repository.tombstone_count(),
        cached_urls=await repository.cached_url_count(),
        schema_version=await repository.schema_version(),
    )


__all__ = [
    "BoardHealth",
    "DoctorReport",
    "SourceHealth",
    "StatsReport",
    "doctor",
    "statistics",
]


def _paused_feeds() -> frozenset[str]:
    from stage.sources import get_feeds

    paused = [feed for feed in get_feeds().values() if getattr(feed, "paused", "")]
    return frozenset(feed.name for feed in paused) | frozenset(
        getattr(feed, "bucket_key", "") or feed.name for feed in paused
    )
