from dataclasses import dataclass
from datetime import UTC, datetime

from stage.domain import (
    STALE_AFTER_DAYS,
    IntegrityFinding,
    JobFilters,
    RateState,
    SourceRunStats,
    SourceVisit,
    SyncRun,
    VisitState,
    VolumeSignal,
    assess_volume,
    classify_visit,
)
from stage.storage import AsyncRepository

RUN_HISTORY = 20
COMPOSITION_COLUMNS = ("source", "location", "role", "term", "language", "degree_requirement")


@dataclass(frozen=True, slots=True)
class BoardHealth:
    source: str
    board: str
    label: str
    state: VisitState
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str


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
        succeeded = sum(1 for board in self.boards if board.state is not VisitState.FAILING)
        return succeeded / len(self.boards)

    @property
    def failing_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for board in self.boards if board.state is VisitState.FAILING)

    @property
    def stale_boards(self) -> tuple[BoardHealth, ...]:
        return tuple(board for board in self.boards if board.state is VisitState.STALE)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    schema_version: int
    last_sync_at: datetime | None
    integrity: tuple[IntegrityFinding, ...]
    sources: tuple[SourceHealth, ...]
    blocks: tuple[RateState, ...]
    never_synced: bool
    stale_after_days: int = STALE_AFTER_DAYS

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
    def warnings(self) -> int:
        return len(self.failing_boards) + len(self.stale_boards)

    @property
    def is_healthy(self) -> bool:
        return not (self.integrity_problems or self.volume_alerts or self.blocks)


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


async def _board_health(
    repository: AsyncRepository, now: datetime, stale_after_days: int
) -> dict[str, list[BoardHealth]]:
    visits: list[SourceVisit] = await repository.all_visits()
    grouped: dict[str, list[BoardHealth]] = {}
    for visit in visits:
        grouped.setdefault(visit.source, []).append(
            BoardHealth(
                source=visit.source,
                board=visit.board,
                label=visit.label or visit.board,
                state=classify_visit(
                    visit.last_success_at, visit.consecutive_failures, now, stale_after_days
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
) -> DoctorReport:
    moment = now or datetime.now(UTC)
    runs = await repository.run_history(history)
    volumes = await repository.volume_history(history)
    stored = await repository.stored_counts()
    boards = await _board_health(repository, moment, stale_after_days)
    rate_state = await repository.load_rate_state()

    latest: dict[str, SourceRunStats] = {}
    for run in runs:
        for stats in run.sources:
            latest.setdefault(stats.source, stats)
    names = sorted(set(volumes) | set(stored) | set(latest) | set(boards))

    sources = tuple(
        SourceHealth(
            source=name,
            stored=stored.get(name, 0),
            volume=assess_volume(name, list(volumes.get(name, ()))),
            requests=latest[name].requests if name in latest else 0,
            not_modified=latest[name].not_modified if name in latest else 0,
            latency_p50_ms=latest[name].latency_p50_ms if name in latest else 0.0,
            latency_p95_ms=latest[name].latency_p95_ms if name in latest else 0.0,
            errors=latest[name].errors if name in latest else 0,
            tightenings=latest[name].tightenings if name in latest else 0,
            deferred=latest[name].deferred if name in latest else 0,
            blocked=latest[name].blocked if name in latest else False,
            boards=tuple(boards.get(name, ())),
        )
        for name in names
    )

    return DoctorReport(
        schema_version=await repository.schema_version(),
        last_sync_at=await repository.last_sync_at(),
        integrity=tuple(await repository.integrity_findings()),
        sources=sources,
        blocks=tuple(
            state for state in rate_state.values() if state.is_blocked(moment)
        ),
        never_synced=not runs,
        stale_after_days=stale_after_days,
    )


async def statistics(
    repository: AsyncRepository, *, history: int = RUN_HISTORY
) -> StatsReport:
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
