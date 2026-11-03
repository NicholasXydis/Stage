import asyncio
import random
import sqlite3
import sys
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Container,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial

from stage.dedup import resolve_duplicates
from stage.domain import (
    FIRST_READ_PREFIX,
    FULL_READ_PREFIX,
    SEARCH_PREFIX,
    BucketPlan,
    Company,
    CompanyDeferred,
    CompanyFailed,
    CompanyFinished,
    CompanyStarted,
    CompanyUnchanged,
    CompanyVisit,
    DetailFetch,
    HttpValidator,
    Job,
    PlannedRequest,
    RateState,
    RequestLogged,
    Rotation,
    RotationMember,
    SourceBlocked,
    SourceCapped,
    SourceFailed,
    SourceFinished,
    SourceFresh,
    SourceResting,
    SourceRotated,
    SourceRunStats,
    SourceStarted,
    SourceVisit,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    SyncRun,
    SyncStarted,
    UnroutableCompanies,
    WorkdayCrawl,
    WorkdayCrawlStep,
    WorkdayFacet,
    is_resting,
    rotate,
)
from stage.domain.text import counted, did_you_mean, plain_error
from stage.http import (
    CEILING_BACKSTOP,
    REFRESH_SLACK_H,
    HostBudget,
    HttpClient,
    HttpError,
    RatePosture,
    RequestRefusedError,
    ValidatorCache,
    failure_text,
    is_disconnected,
    resolve,
    skipped_by_breaker,
)
from stage.http.profiles import NORMAL_REFRESH_H
from stage.services.screening import Screener
from stage.sources import (
    Adapter,
    FeedAdapter,
    FetchResult,
    adapter_for_platform,
    get_adapters,
    get_feeds,
    platform_bucket,
    platform_front,
    sentences,
)
from stage.sources.base import parse_pages_with
from stage.sources.workday import WHOLE_READ_BOARDS, WorkdayAdapter, full_read_day
from stage.storage import AsyncRepository, SourceBatch, SourceBatchResult
from stage.storage.problems import not_saved


class NoSourcesSelectedError(Exception):
    pass


def _bucket_postures(
    grouped: Mapping[str, tuple[Adapter, list[Company]]], feeds: Mapping[str, FeedAdapter]
) -> dict[str, RatePosture]:
    claims: list[tuple[tuple[str, ...], RatePosture]] = []
    for adapter, companies in grouped.values():
        members: dict[str, list[Company]] = {}
        for company in companies:
            for bucket in _adapter_buckets(adapter, [company]):
                members.setdefault(bucket, []).append(company)
        claims.extend(
            (
                (bucket,),
                resolve(adapter.rate_profile, [company.rate_profile for company in rows]).sized_for(
                    len(rows), _reserve_for(adapter)
                ),
            )
            for bucket, rows in members.items()
        )
    claims.extend(
        (_bucket_keys(feed.hosts, feed.bucket_key), resolve(feed.rate_profile, []))
        for feed in feeds.values()
    )

    postures: dict[str, RatePosture] = {}
    for keys, posture in claims:
        for bucket in keys:
            stored = postures.get(bucket)
            postures[bucket] = posture if stored is None else _shared(stored, posture)
    return postures


def _shared(stored: RatePosture, claim: RatePosture) -> RatePosture:
    ceiling = max(stored.max_requests_per_run, claim.max_requests_per_run)
    return replace(stored.strictest(claim), max_requests_per_run=ceiling)


def _bucket_keys(hosts: frozenset[str], bucket_key: str) -> tuple[str, ...]:
    return (bucket_key,) if bucket_key else tuple(sorted(hosts))


def _host_buckets(adapter: Adapter, companies: Sequence[Company]) -> dict[str, str]:
    hosts = adapter.hosts_for(companies) or adapter.hosts
    if adapter.bucket_key:
        return dict.fromkeys(hosts or (adapter.bucket_key,), adapter.bucket_key)
    return {host: platform_bucket(host) for host in hosts}


def _adapter_buckets(adapter: Adapter, companies: Sequence[Company]) -> tuple[str, ...]:
    if adapter.bucket_key:
        return (adapter.bucket_key,)
    return tuple(sorted(set(_host_buckets(adapter, companies).values())))


def _plan_bounds(
    adapter: Adapter,
    source: str,
    companies: Sequence[Company],
    page_budgets: Mapping[str, int],
    detail_budget: int,
) -> list[tuple[str, str, int, int]]:
    members: dict[str, int] = {}
    worst: dict[str, int] = {}
    for company in companies:
        for bucket in _adapter_buckets(adapter, [company]):
            members[bucket] = members.get(bucket, 0) + 1
            worst[bucket] = worst.get(bucket, 0) + page_budgets.get(company.registry_key, 0)
    bounds = [(bucket, source, members[bucket], worst[bucket]) for bucket in sorted(members)]
    if detail_budget and bounds:
        bucket, name, count, total = bounds[0]
        bounds[0] = (bucket, name, count, total + detail_budget)
    return bounds


def _reserve_for(adapter: Adapter) -> int:
    return getattr(adapter, "retry_reserve", 0) + adapter.detail_budget


DAILY_RUNS = 4
DAILY_HEADROOM = 2.5
CLEAN_DAY_PACE = 2
ACTIVITY_DAYS = 30
BUSY_DAYS = 7
TIP_HOURS = 24
TIP_LIMIT = 40
GIL_SWITCH_S = 0.001
CRAWL_RESUME_H = 1.0
FULL_READ_GAP_H = 20.0
CRAWL_GIVEN_UP = (
    "Stage resumed this crawl once and it is still incomplete, so its postings stay open "
    "and the next sync starts over"
)


@dataclass(frozen=True, slots=True)
class Pacing:
    spent: Mapping[str, int] = field(default_factory=dict)
    has_history: bool = False
    pushback: frozenset[str] = frozenset()
    activity: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    tips: Mapping[str, datetime] = field(default_factory=dict)
    followed: frozenset[str] = frozenset()
    avoided: frozenset[str] = frozenset()

    def pace(self, source: str) -> int:
        return 1 if source in self.pushback else CLEAN_DAY_PACE

    def window(
        self, adapter: Adapter, company: Company, postures: Mapping[str, RatePosture]
    ) -> float:
        from stage.lexicon import fold

        claimed = [
            postures.get(bucket, RatePosture()) for bucket in _adapter_buckets(adapter, [company])
        ]
        posture = replace(
            RatePosture(),
            refresh_interval_h=max(claim.refresh_interval_h for claim in claimed),
            busy_refresh_h=max(claim.busy_refresh_h for claim in claimed),
        )
        folded = fold(company.name)
        if folded in self.avoided:
            return posture.window(0, 0)
        month, week = self.activity.get(_safe_board_key(adapter, company), (0, 0))
        return posture.window(month, week, folded in self.followed)

    def tipped(self, company: Company) -> datetime | None:
        from stage.lexicon import fold

        folded = fold(company.name)
        return None if folded in self.avoided else self.tips.get(folded)


async def _pacing(
    repository: AsyncRepository, run_started_at: datetime, feeds: Sequence[str] = ()
) -> Pacing:
    day = run_started_at - timedelta(hours=24)
    spent, has_history = await repository.requests_since(day, run_started_at)
    followed, avoided = _chosen()
    tips = await repository.feed_tips(
        feeds, run_started_at - timedelta(hours=TIP_HOURS), TIP_LIMIT + len(avoided)
    )
    return Pacing(
        spent=spent,
        has_history=has_history,
        pushback=await repository.pushback_since(day, run_started_at),
        activity=await repository.board_activity(
            run_started_at - timedelta(days=ACTIVITY_DAYS),
            run_started_at - timedelta(days=BUSY_DAYS),
        ),
        tips=dict(
            [(folded, at) for folded, at in tips.items() if folded not in avoided][:TIP_LIMIT]
        ),
        followed=followed,
        avoided=avoided,
    )


def _chosen() -> tuple[frozenset[str], frozenset[str]]:
    from stage.services.preferences import load_preferences

    try:
        loaded = load_preferences()
    except OSError:
        return frozenset(), frozenset()
    chosen = loaded.preferences
    return (
        frozenset(company.folded for company in chosen.follow),
        frozenset(company.folded for company in chosen.avoid),
    )


def _daily_allowance(
    posture: RatePosture,
    spent: int,
    has_history: bool,
    buckets: int = 1,
    pace: int = 1,
    boards: int = 0,
) -> int:
    if not has_history:
        return posture.max_requests_per_run
    span = max(1, buckets)
    runs = min(CEILING_BACKSTOP * 2, posture.max_requests_per_run * DAILY_RUNS) * span
    cap = max(runs, _quiet_day(posture, boards)) * pace
    return max(0, min(posture.max_requests_per_run, cap - spent))


def _quiet_day(posture: RatePosture, boards: int) -> int:
    if posture.refresh_interval_h <= 0:
        return 0
    reads = 24 / posture.refresh_interval_h
    return round(boards * posture.requests_per_board * reads * DAILY_HEADROOM)


def _active_block(
    rate_state: Mapping[str, RateState], hosts: Mapping[str, str], now: datetime
) -> RateState | None:
    found: list[RateState] = []
    for host, bucket in sorted(hosts.items()):
        states = (
            rate_state.get(bucket),
            rate_state.get(platform_front(host)),
            rate_state.get(host),
        )
        state = next((item for item in states if item is not None and item.is_blocked(now)), None)
        if state is None:
            return None
        found.append(state)
    return found[0] if found else None


def _feed_hosts(feed: FeedAdapter) -> dict[str, str]:
    return {host: feed.bucket_key or host for host in feed.hosts}


def _group_by_adapter(
    companies: Sequence[Company],
) -> tuple[dict[str, tuple[Adapter, list[Company]]], list[Company]]:
    grouped: dict[str, tuple[Adapter, list[Company]]] = {}
    unroutable: list[Company] = []
    for company in companies:
        if not company.enabled:
            continue
        adapter = adapter_for_platform(company.platform)
        if adapter is None:
            unroutable.append(company)
            continue
        entry = grouped.setdefault(adapter.name, (adapter, []))
        entry[1].append(company)
    return grouped, unroutable


def _registry_boards(
    companies: Sequence[Company],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    boards: set[str] = set()
    unreadable: set[str] = set()
    covered: set[str] = set()
    for company in companies:
        adapter = adapter_for_platform(company.platform)
        if adapter is None:
            continue
        covered.add(adapter.name)
        try:
            boards.add(adapter.board_key(company))
        except Exception:
            unreadable.add(adapter.name)
    return tuple(sorted(covered - unreadable)), tuple(sorted(boards))


def _unknown_sources(unknown: set[str], known: set[str], purpose: str = "") -> str:
    from difflib import get_close_matches

    names = sorted(unknown)
    quoted = ", ".join(f'"{name}"' for name in names)
    near = get_close_matches(names[0], sorted(known), n=1) if len(names) == 1 else []
    hint = did_you_mean(near[0]) if near else f"Choose from: {', '.join(sorted(known))}."
    noun = "source" if len(names) == 1 else "sources"
    return f"Stage has no {noun} named {quoted}{purpose}. {hint}"


def _select(
    companies: Sequence[Company],
    sources: Sequence[str] | None,
    excluded: Sequence[str] | None = None,
) -> tuple[dict[str, tuple[Adapter, list[Company]]], dict[str, FeedAdapter], list[Company]]:
    grouped, unroutable = _group_by_adapter(companies)
    feeds = dict(get_feeds())
    known = set(grouped) | set(feeds) | set(get_adapters())
    if sources is None:
        feeds = {name: feed for name, feed in feeds.items() if not getattr(feed, "paused", "")}
    else:
        selected = {name.strip().lower() for name in sources}
        unknown = selected - known
        if unknown:
            raise NoSourcesSelectedError(_unknown_sources(unknown, known))
        grouped = {name: value for name, value in grouped.items() if name in selected}
        feeds = {name: value for name, value in feeds.items() if name in selected}
    if excluded:
        dropped = {name.strip().lower() for name in excluded}
        unknown = dropped - known
        if unknown:
            raise NoSourcesSelectedError(_unknown_sources(unknown, known, " to exclude"))
        grouped = {name: value for name, value in grouped.items() if name not in dropped}
        feeds = {name: value for name, value in feeds.items() if name not in dropped}
    if not grouped and not feeds:
        detail = ""
        if unroutable:
            platforms = ", ".join(sorted({company.platform.value for company in unroutable}))
            detail = f" ({counted(len(unroutable), 'enabled row')} use {platforms})"
        raise NoSourcesSelectedError(
            f"The registry has no enabled boards on a platform Stage can read{detail}."
        )
    return grouped, feeds, unroutable


def _since(clock: float) -> float:
    return (time.perf_counter() - clock) * 1000


@dataclass(frozen=True, slots=True)
class Attempt:
    board: str
    label: str
    planned: tuple[str, ...]
    result: FetchResult | None = None
    error: str = ""
    elapsed_ms: float = 0.0
    sent: bool = True
    unreachable: bool = False
    cut_off: bool = False

    @property
    def fresh(self) -> FetchResult | None:
        return None if self.result is None or self.result.not_modified else self.result


async def _attempt(
    fetch: Callable[[], Awaitable[FetchResult]], board: str, label: str, planned: tuple[str, ...]
) -> Attempt:
    clock = time.perf_counter()
    try:
        result = await fetch()
    except Exception as exc:
        refused = isinstance(exc, RequestRefusedError)
        return Attempt(
            board,
            label,
            planned,
            error=failure_text(exc),
            elapsed_ms=_since(clock),
            sent=not refused,
            unreachable=isinstance(exc, HttpError),
            cut_off=not refused and is_disconnected(exc),
        )
    return Attempt(board, label, planned, result=result, elapsed_ms=_since(clock))


def _keepable_validators(cache: ValidatorCache, skip_urls: set[str]) -> tuple[HttpValidator, ...]:
    return tuple(
        validator
        for url, validator in cache.pending.items()
        if url not in skip_urls or not validator.usable
    )


def _advance_cursor(
    settled: Sequence[RateState], rotation: Rotation, bucket: str, now: datetime
) -> tuple[RateState, ...]:
    if not rotation.rotating:
        return tuple(
            state.with_cursor("", now)
            if state.bucket == bucket and state.rotation_cursor
            else state
            for state in settled
        )
    updated = [
        state.with_cursor(rotation.cursor, now) if state.bucket == bucket else state
        for state in settled
    ]
    if not any(state.bucket == bucket for state in updated):
        updated.append(RateState(bucket=bucket, updated_at=now, rotation_cursor=rotation.cursor))
    return tuple(updated)


def _bucket_plans(
    bounds: Sequence[tuple[str, str, int, int]], postures: Mapping[str, RatePosture]
) -> tuple[BucketPlan, ...]:
    planned: dict[str, int] = {}
    worst: dict[str, int] = {}
    sources: dict[str, list[str]] = {}
    for bucket, source, companies, worst_case in bounds:
        planned[bucket] = planned.get(bucket, 0) + companies
        worst[bucket] = worst.get(bucket, 0) + worst_case
        sources.setdefault(bucket, []).append(source)
    return tuple(
        BucketPlan(
            bucket=bucket,
            sources=tuple(sorted(sources[bucket])),
            planned=planned[bucket],
            worst_case=worst[bucket],
            ceiling=postures.get(bucket, RatePosture()).max_requests_per_run,
        )
        for bucket in sorted(planned)
    )


def _safe_plan(adapter: Adapter, company: Company) -> tuple[tuple[str, ...], str]:
    try:
        return adapter.plan(company), ""
    except Exception as exc:
        return (), failure_text(exc)


def _partial_reason(
    failed_sources: Sequence[str],
    unroutable: Sequence[Company],
    blocked_sources: Sequence[str],
    deferred_sources: Sequence[str],
) -> str:
    reasons = []
    if failed_sources:
        reasons.append(f"{counted(len(failed_sources), 'source')} failed")
    if blocked_sources:
        reasons.append(f"{counted(len(blocked_sources), 'source')} blocked")
    if deferred_sources:
        reasons.append(f"{counted(len(deferred_sources), 'source')} left boards for the next sync")
    if unroutable:
        reasons.append(f"{counted(len(unroutable), 'row')} on a platform Stage cannot read")
    return ", ".join(reasons)


def _stalest_first(
    adapter: Adapter, companies: Sequence[Company], last_success: Mapping[str, datetime | None]
) -> list[Company]:
    def age(company: Company) -> tuple[int, datetime]:
        seen = last_success.get(_safe_board_key(adapter, company))
        return (1, seen) if seen is not None else (0, datetime.min.replace(tzinfo=UTC))

    return sorted(companies, key=age)


def _safe_board_key(adapter: Adapter, company: Company) -> str:
    try:
        return adapter.board_key(company)
    except Exception:
        return f"<unresolved>:{company.registry_key}"


def _planned(
    source: str, label: str, url: str, validators: Mapping[str, HttpValidator]
) -> PlannedRequest:
    cached = validators.get(url)
    has_validator = cached is not None and cached.usable
    return PlannedRequest(
        source=source,
        company=label,
        url=url,
        has_validator=has_validator,
        expectation="likely 304 (validator on file)" if has_validator else "full response",
    )


def _idle_finish(source: str, clock: float) -> SourceFinished:
    return SourceFinished(
        source=source,
        fetched=0,
        added=0,
        updated=0,
        closed=0,
        failed_companies=0,
        elapsed_ms=_since(clock),
    )


@dataclass(slots=True)
class FetchHarvest:
    collected: list[Job] = field(default_factory=list)
    closable: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skip_urls: set[str] = field(default_factory=set)
    visits: list[CompanyVisit] = field(default_factory=list)
    resolved_facets: list[WorkdayFacet] = field(default_factory=list)
    forgotten_facets: list[WorkdayFacet] = field(default_factory=list)
    workday_crawls: list[WorkdayCrawlStep] = field(default_factory=list)
    detail_outcomes: list[DetailFetch] = field(default_factory=list)
    errors: int = 0
    cut_off: int = 0
    deferred_boards: int = 0
    succeeded: bool = False

    @property
    def offline(self) -> bool:
        connected = self.succeeded or bool(self.unchanged)
        reached = self.errors + self.deferred_boards
        stray = self.errors - self.cut_off
        nearly_all = self.cut_off > stray and stray <= max(1, self.errors // 10)
        return not connected and reached >= 2 and nearly_all

    @property
    def whole(self) -> bool:
        return bool(self.closable) and not self.skip_urls

    def take(self, source: str, attempt: Attempt, *, first_read: bool = False) -> SyncEvent:
        board, label, result = attempt.board, attempt.label, attempt.result
        if result is None and not attempt.sent:
            self.skip_urls.update(attempt.planned)
            self.deferred_boards += 1
            return CompanyDeferred(source=source, company=label, reason=attempt.error)
        if result is None:
            self.errors += 1
            self.cut_off += int(attempt.cut_off)
            self.skip_urls.update(attempt.planned)
            self.visits.append(
                CompanyVisit(board=board, succeeded=False, error=attempt.error, label=label)
            )
            return CompanyFailed(
                source=source,
                company=label,
                error=attempt.error,
                elapsed_ms=attempt.elapsed_ms,
                unreachable=attempt.unreachable,
            )
        self.visits.append(CompanyVisit(board=board, succeeded=True, label=label))
        if result.not_modified:
            self.unchanged.append(board)
            return CompanyUnchanged(source=source, company=label, elapsed_ms=attempt.elapsed_ms)
        self._keep(board, result, first_read)
        if result.degraded:
            self.skip_urls.update(attempt.planned)
            self.skip_urls.update(result.stale_urls)
        return CompanyFinished(
            source=source,
            company=label,
            fetched=len(result.jobs),
            elapsed_ms=attempt.elapsed_ms,
            degraded=result.degraded,
            declared_empty=result.declared_empty,
        )

    def _keep(self, board: str, result: FetchResult, first_read: bool) -> None:
        self.succeeded = True
        self.collected.extend(result.jobs)
        if first_read:
            self.detail_outcomes.append(
                DetailFetch(id=f"{FIRST_READ_PREFIX}{board}", resolved=True)
            )
        self.resolved_facets.extend(
            item for item in result.facets if isinstance(item, WorkdayFacet)
        )
        self.forgotten_facets.extend(
            item for item in result.forgotten_facets if isinstance(item, WorkdayFacet)
        )
        self.detail_outcomes.extend(
            item for item in result.detail_fetches if isinstance(item, DetailFetch)
        )
        if isinstance(result.workday_crawl, WorkdayCrawlStep):
            self.workday_crawls.append(result.workday_crawl)
        if result.authoritative:
            self.closable.append(board)


@dataclass(frozen=True, slots=True)
class ClientMetrics:
    requests: int = 0
    not_modified: int = 0
    retries: int = 0
    tightenings: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0

    @classmethod
    def of(cls, client: HttpClient) -> "ClientMetrics":
        p50, p95 = client.latency_percentiles()
        return cls(
            client.request_count,
            client.not_modified_count,
            client.retry_count,
            client.tightening_count,
            p50,
            p95,
        )


@dataclass(frozen=True, slots=True)
class Timings:
    elapsed_ms: float
    fetch_ms: float
    normalize_ms: float
    write_ms: float


@dataclass(slots=True)
class RunLedger:
    stats: list[SourceRunStats] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    succeeded: list[bool] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    plan_bounds: list[tuple[str, str, int, int]] = field(default_factory=list)
    screener: Screener = field(default_factory=Screener)
    first_reads: list[str] = field(default_factory=list)

    def block(self, source: str, state: RateState, now: datetime, dry_run: bool) -> SourceBlocked:
        self.blocked.append(source)
        if not dry_run:
            self.stats.append(SourceRunStats(source=source, blocked=True))
        return SourceBlocked(
            source=source,
            bucket=state.bucket,
            blocked_until=state.blocked_until or now,
            remaining_s=state.blocks_remaining_s(now),
            reason=plain_error(state.reason) if state.reason else "",
            consecutive_failures=state.consecutive_failures,
        )

    def broke(self, source: str, exc: Exception) -> SourceFailed:
        self.failed.append(source)
        self.stats.append(SourceRunStats(source=source, errors=1))
        said = not_saved(exc) if isinstance(exc, sqlite3.Error) else failure_text(exc)
        return SourceFailed(source=source, error=said)

    def settle(
        self,
        source: str,
        harvest: FetchHarvest,
        counts: SourceBatchResult,
        metrics: ClientMetrics,
        timings: Timings,
        deferred: int,
    ) -> SourceFinished:
        if harvest.succeeded or harvest.unchanged:
            self.succeeded.append(True)
        if harvest.errors:
            self.failed.append(source)
        if harvest.deferred_boards:
            self.deferred.append(source)
        self.stats.append(
            SourceRunStats(
                source=source,
                fetched=counts.fetched,
                added=counts.added,
                updated=counts.updated,
                closed=counts.closed,
                quarantined=counts.quarantined,
                errors=harvest.errors,
                requests=metrics.requests,
                not_modified=metrics.not_modified,
                retries=metrics.retries,
                tightenings=metrics.tightenings,
                latency_p50_ms=metrics.latency_p50_ms,
                latency_p95_ms=metrics.latency_p95_ms,
                elapsed_ms=timings.elapsed_ms,
                deferred=deferred,
                stored=counts.stored,
            )
        )
        return SourceFinished(
            source=source,
            fetched=counts.fetched,
            added=counts.added,
            updated=counts.updated,
            closed=counts.closed,
            quarantined=counts.quarantined,
            failed_companies=harvest.errors,
            elapsed_ms=timings.elapsed_ms,
            fetch_ms=timings.fetch_ms,
            normalize_ms=timings.normalize_ms,
            write_ms=timings.write_ms,
            requests=metrics.requests,
            not_modified=metrics.not_modified,
            retries=metrics.retries,
            tightenings=metrics.tightenings,
            latency_p50_ms=metrics.latency_p50_ms,
            latency_p95_ms=metrics.latency_p95_ms,
        )

    def outcome(self, unroutable: Sequence[Company]) -> SyncOutcome:
        if not self.succeeded and (self.failed or self.blocked):
            return SyncOutcome.FAILURE
        if self.failed or unroutable or self.blocked or self.deferred:
            return SyncOutcome.PARTIAL
        return SyncOutcome.SUCCESS


async def _merge(
    streams: Sequence[tuple[str, AsyncGenerator[SyncEvent]]], ledger: RunLedger
) -> AsyncGenerator[SyncEvent]:
    if len(streams) == 1:
        source, stream = streams[0]
        try:
            async with aclosing(stream):
                async for event in stream:
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield ledger.broke(source, exc)
        return

    queue: asyncio.Queue[SyncEvent | tuple[str, Exception] | None] = asyncio.Queue()

    async def pump(source: str, stream: AsyncGenerator[SyncEvent]) -> None:
        try:
            async with aclosing(stream):
                async for event in stream:
                    await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put((source, exc))
        finally:
            queue.put_nowait(None)

    tasks = [asyncio.create_task(pump(source, stream)) for source, stream in streams]
    try:
        done = 0
        while done < len(tasks):
            item = await queue.get()
            if item is None:
                done += 1
            elif isinstance(item, tuple):
                yield ledger.broke(*item)
            else:
                yield item
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _drop_recently_refreshed(
    adapter: Adapter,
    companies: Sequence[Company],
    last_success: Mapping[str, datetime | None],
    open_crawls: frozenset[str],
    run_started_at: datetime,
    window: Callable[[Company], float],
    skip: bool,
    tipped: Callable[[Company], datetime | None] = lambda _company: None,
) -> tuple[list[Company], int]:
    if skip:
        return list(companies), 0

    def due(company: Company) -> bool:
        hours = window(company)
        if hours <= 0:
            return True
        board = _safe_board_key(adapter, company)
        seen = last_success.get(board)
        if seen is None or seen > run_started_at:
            return True
        tip = tipped(company)
        if tip is not None and seen < tip:
            return True
        if board in open_crawls:
            hours = min(hours, CRAWL_RESUME_H)
        return seen < run_started_at - timedelta(hours=max(0.0, hours - REFRESH_SLACK_H))

    kept = [company for company in companies if due(company)]
    return kept, len(companies) - len(kept)


def _drop_resting(
    adapter: Adapter,
    companies: Sequence[Company],
    board_visits: Mapping[str, SourceVisit],
    run_started_at: datetime,
    skip: bool,
) -> tuple[list[Company], int]:
    if skip:
        return list(companies), 0

    def awake(company: Company) -> bool:
        visit = board_visits.get(_safe_board_key(adapter, company))
        return (
            visit is None
            or skipped_by_breaker(visit.last_error)
            or not is_resting(visit, run_started_at)
        )

    kept = [company for company in companies if awake(company)]
    return kept, len(companies) - len(kept)


def _still_first_read(
    board: str,
    result: FetchResult,
    last_success: Mapping[str, datetime | None],
    marked: Mapping[str, datetime],
    resuming: Container[str],
    full_states: Mapping[str, tuple[datetime, bool, bool]],
) -> bool:
    seen = last_success.get(board)
    if seen is None:
        return True
    if board in resuming and marked.get(board) == seen:
        return True
    whole = f"{FULL_READ_PREFIX}{board}"
    finished = any(
        isinstance(entry, DetailFetch) and entry.id == whole and entry.resolved
        for entry in result.detail_fetches
    )
    return finished and not full_states.get(board, (seen, False, False))[1]


def _full_reads_due(
    adapter: Adapter,
    companies: Sequence[Company],
    states: Mapping[str, tuple[datetime, bool, bool]],
    now: datetime,
) -> list[str]:
    wanted: list[tuple[datetime, str]] = []
    for company in companies:
        board = _safe_board_key(adapter, company)
        state = states.get(board)
        if board in WHOLE_READ_BOARDS:
            wanted.append((state[0] if state is not None else now, company.registry_key))
            continue
        if state is None:
            continue
        attempted, _, failed = state
        if not attempted <= now - timedelta(hours=FULL_READ_GAP_H):
            continue
        if failed or full_read_day(board) == now.weekday():
            wanted.append((attempted, company.registry_key))
    return [key for _, key in sorted(wanted)]


def _searches_due(
    adapter: Adapter,
    companies: Sequence[Company],
    searched: Mapping[str, tuple[datetime, bool, bool]],
    now: datetime,
) -> list[str]:
    since = now - timedelta(hours=NORMAL_REFRESH_H - REFRESH_SLACK_H)
    return [
        company.registry_key
        for company in companies
        if not (state := searched.get(_safe_board_key(adapter, company))) or state[0] <= since
    ]


async def _persist_source_batch(
    repository: AsyncRepository, screener: Screener, batch: SourceBatch, collected: Sequence[Job]
) -> tuple[SourceBatchResult, float, float]:
    normalize_clock = time.perf_counter()
    kept, rejected = await screener(collected)
    normalize_ms = _since(normalize_clock)
    write_clock = time.perf_counter()
    counts = await repository.apply_source_batch(replace(batch, jobs=kept, quarantined=rejected))
    if counts.successors:
        from stage.services.tracker import follow_recorded

        await follow_recorded(repository)
    return counts, normalize_ms, _since(write_clock)


async def _visits(repository: AsyncRepository, source: str) -> dict[str, SourceVisit]:
    return {visit.board: visit for visit in await repository.all_visits() if visit.source == source}


@dataclass(frozen=True, slots=True)
class BoardPlan:
    ordered: list[Company]
    rotation: Rotation
    rotation_bucket: str
    window_h: float
    last_success: Mapping[str, datetime | None]
    marked: Mapping[str, datetime]
    seed: dict[str, HttpValidator]
    base: RatePosture
    posture: RatePosture
    spent: int
    refreshed: int
    resting: int
    page_budgets: Mapping[str, int]
    detail_budget: int
    detail_queue: Sequence[str]
    facets: Mapping[tuple[str, str], WorkdayFacet] = field(default_factory=dict)
    crawls: Mapping[str, WorkdayCrawl] = field(default_factory=dict)
    full_states: Mapping[str, tuple[datetime, bool, bool]] = field(default_factory=dict)
    full_reads: frozenset[str] = frozenset()
    searches: frozenset[str] = frozenset()

    def opening(self, source: str) -> Iterator[SyncEvent]:
        yield SourceStarted(source=source, companies=len(self.ordered))
        if self.posture.max_requests_per_run < self.base.max_requests_per_run:
            yield SourceCapped(
                source=source,
                bucket=self.rotation_bucket,
                spent=self.spent,
                allowance=self.posture.max_requests_per_run,
                ceiling=self.base.max_requests_per_run,
            )
        if self.refreshed:
            yield SourceFresh(
                source=source,
                skipped=self.refreshed,
                remaining=len(self.ordered),
                refresh_interval_h=self.window_h,
            )
        if self.resting:
            yield SourceResting(source=source, skipped=self.resting, remaining=len(self.ordered))
        if self.rotation.rotating:
            yield SourceRotated(
                source=source,
                bucket=self.rotation_bucket,
                selected=len(self.rotation.selected),
                deferred=len(self.rotation.deferred),
                cursor=self.rotation.cursor,
                wrapped=self.rotation.wrapped,
            )

    def first_read(self, attempt: Attempt) -> bool:
        result = attempt.fresh
        return result is not None and _still_first_read(
            attempt.board,
            result,
            self.last_success,
            self.marked,
            self.crawls.keys(),
            self.full_states,
        )


async def _plan_boards(
    repository: AsyncRepository,
    adapter: Adapter,
    companies: Sequence[Company],
    run_started_at: datetime,
    shuffler: random.Random,
    rate_state: Mapping[str, RateState],
    postures: Mapping[str, RatePosture],
    pacing: Pacing,
    skip_refresh: bool,
) -> BoardPlan:
    source = adapter.name
    buckets = _adapter_buckets(adapter, companies)
    rotation_bucket = buckets[0]
    stored = rate_state.get(rotation_bucket)
    board_visits = await _visits(repository, source)
    last_success = {board: visit.last_success_at for board, visit in board_visits.items()}
    marked = await repository.first_reads()
    rotation = rotate(
        [RotationMember(key=company.registry_key) for company in companies],
        cursor=stored.rotation_cursor if stored is not None else "",
        budget=adapter.rotation_slice,
    )
    covered = set(rotation.selected)
    workday = adapter if isinstance(adapter, WorkdayAdapter) else None
    crawls = await repository.load_workday_crawls() if workday is not None else {}
    ordered, refreshed = _drop_recently_refreshed(
        adapter,
        [company for company in companies if company.registry_key in covered],
        last_success,
        frozenset(crawls),
        run_started_at,
        lambda company: pacing.window(adapter, company, postures),
        skip_refresh,
        pacing.tipped,
    )
    ordered, resting = _drop_resting(adapter, ordered, board_visits, run_started_at, skip_refresh)
    seed = dict(await repository.load_validators(source))
    base = postures.get(rotation_bucket) or resolve(
        adapter.rate_profile, [company.rate_profile for company in ordered]
    )
    spent = pacing.spent.get(source, 0)
    allowance = _daily_allowance(
        base,
        spent,
        pacing.has_history,
        buckets=len(buckets),
        pace=pacing.pace(source),
        boards=len(companies),
    )
    posture = replace(base, max_requests_per_run=allowance)
    facets: Mapping[tuple[str, str], WorkdayFacet] = {}
    full_states: Mapping[str, tuple[datetime, bool, bool]] = {}
    wanted: list[str] = []
    searches: list[str] = []
    page_budgets: Mapping[str, int] = {
        company.registry_key: adapter.max_requests_per_company for company in ordered
    }
    detail_budget = adapter.detail_budget
    if workday is not None:
        facets = await repository.load_workday_facets()
        full_states = await repository.full_reads(source, FULL_READ_PREFIX)
        wanted = _full_reads_due(adapter, ordered, full_states, run_started_at)
        searched = await repository.full_reads(source, SEARCH_PREFIX)
        searches = _searches_due(adapter, ordered, searched, run_started_at)
        glances = [
            company.registry_key
            for company in ordered
            if _safe_board_key(adapter, company) in full_states and company.registry_key in searches
        ]
        page_budgets, detail_budget = (
            workday.crawl_budgets(
                ordered,
                crawls,
                facets,
                posture.max_requests_per_run,
                full_reads=wanted,
                glances=glances,
            )
            if ordered
            else ({}, 0)
        )
    shuffler.shuffle(ordered)
    return BoardPlan(
        ordered=_stalest_first(adapter, ordered, last_success),
        rotation=rotation,
        rotation_bucket=rotation_bucket,
        window_h=postures.get(rotation_bucket, RatePosture()).refresh_interval_h,
        last_success=last_success,
        marked=marked,
        seed=seed,
        base=base,
        posture=posture,
        spent=spent,
        refreshed=refreshed,
        resting=resting,
        page_budgets=page_budgets,
        detail_budget=detail_budget,
        detail_queue=await repository.detail_queue(source, detail_budget),
        facets=facets,
        crawls=crawls,
        full_states=full_states,
        full_reads=frozenset(wanted),
        searches=frozenset(searches),
    )


def _rehearse_company_source(
    adapter: Adapter, plan: BoardPlan, ledger: RunLedger, clock: float
) -> Iterator[SyncEvent]:
    source = adapter.name
    detail_budget = plan.detail_budget if isinstance(adapter, WorkdayAdapter) else 0
    ledger.plan_bounds.extend(
        _plan_bounds(adapter, source, plan.ordered, plan.page_budgets, detail_budget)
    )
    for company in plan.ordered:
        urls, error = _safe_plan(adapter, company)
        if error:
            yield CompanyFailed(source=source, company=company.name, error=error, elapsed_ms=0.0)
        for url in urls:
            yield _planned(source, company.name, url, plan.seed)
    yield _idle_finish(source, clock)


def _crawl_given_up(result: FetchResult) -> FetchResult:
    crawl = result.workday_crawl
    if crawl is None or crawl.complete or crawl.discard:
        return result
    return replace(
        result,
        degraded=sentences(result.degraded, CRAWL_GIVEN_UP),
        workday_crawl=replace(crawl, discard=True),
    )


async def _fetch_company(
    adapter: Adapter, company: Company, client: HttpClient, now: datetime, plan: BoardPlan
) -> FetchResult:
    if not isinstance(adapter, WorkdayAdapter):
        return await adapter.fetch(company, client, now, plan.facets, plan.detail_queue)
    board = _safe_board_key(adapter, company)
    result = await adapter.fetch(
        company,
        client,
        now,
        plan.facets,
        plan.detail_queue,
        crawl=plan.crawls.get(board),
        page_budget=plan.page_budgets[company.registry_key],
        full_read=company.registry_key in plan.full_reads,
        size_known=board in plan.full_states,
        search=company.registry_key in plan.searches,
    )
    return _crawl_given_up(result) if board in plan.crawls else result


async def _read_boards(
    adapter: Adapter,
    plan: BoardPlan,
    client: HttpClient,
    now: datetime,
    harvest: FetchHarvest,
    ledger: RunLedger,
) -> AsyncGenerator[SyncEvent]:
    source = adapter.name
    pending = [
        asyncio.create_task(
            _attempt(
                partial(_fetch_company, adapter, company, client, now, plan),
                _safe_board_key(adapter, company),
                company.name,
                _safe_plan(adapter, company)[0],
            )
        )
        for company in plan.ordered
    ]
    try:
        for company in plan.ordered:
            yield CompanyStarted(source=source, company=company.name)
        for finished in asyncio.as_completed(pending):
            attempt = await finished
            for record in client.drain_requests(source):
                yield record
            if attempt.fresh is not None and plan.last_success.get(attempt.board) is None:
                ledger.first_reads.append(attempt.board)
            yield harvest.take(source, attempt, first_read=plan.first_read(attempt))
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_company_source(
    repository: AsyncRepository,
    adapter: Adapter,
    companies: Sequence[Company],
    run_started_at: datetime,
    shuffler: random.Random,
    dry_run: bool,
    ledger: RunLedger,
    rate_state: Mapping[str, RateState],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    force_refresh: bool,
    pacing: Pacing,
) -> AsyncGenerator[SyncEvent]:
    source = adapter.name
    block = _active_block(rate_state, _host_buckets(adapter, companies), run_started_at)
    if block is not None:
        yield ledger.block(source, block, run_started_at, dry_run)
        return

    clock = time.perf_counter()
    plan = await _plan_boards(
        repository,
        adapter,
        companies,
        run_started_at,
        shuffler,
        rate_state,
        postures,
        pacing,
        skip_refresh=force_refresh or dry_run,
    )
    for event in plan.opening(source):
        yield event
    if dry_run:
        for event in _rehearse_company_source(adapter, plan, ledger, clock):
            yield event
        return

    cache = ValidatorCache(plan.seed)
    harvest = FetchHarvest()
    hosts = adapter.hosts_for(plan.ordered)
    async with HttpClient(
        allowed_hosts=hosts,
        posture=plan.posture,
        cache=cache,
        rng=shuffler,
        bucket_key=adapter.bucket_key,
        rate_state=rate_state,
        now=run_started_at,
        budgets=budgets,
        postures={**postures, plan.rotation_bucket: plan.posture},
        host_buckets=_host_buckets(adapter, plan.ordered),
        host_fronts={host: platform_front(host) for host in hosts},
    ) as client:
        async with aclosing(
            _read_boards(adapter, plan, client, run_started_at, harvest, ledger)
        ) as events:
            async for event in events:
                yield event
        metrics = ClientMetrics.of(client)
        validators = _keepable_validators(cache, harvest.skip_urls)
        settled = _advance_cursor(
            client.rate_state(run_started_at), plan.rotation, plan.rotation_bucket, run_started_at
        )
        if harvest.offline:
            settled = ()
            harvest.visits = [visit for visit in harvest.visits if visit.succeeded]

    fetch_ms = _since(clock)
    counts, normalize_ms, write_ms = await _persist_source_batch(
        repository,
        ledger.screener,
        SourceBatch(
            source=source,
            run_started_at=run_started_at,
            closable_boards=tuple(harvest.closable),
            unchanged_boards=tuple(harvest.unchanged),
            validators=validators,
            rate_state=settled,
            workday_facets=tuple(harvest.resolved_facets),
            forgotten_facets=tuple(harvest.forgotten_facets),
            workday_crawls=tuple(harvest.workday_crawls),
            detail_fetches=tuple(harvest.detail_outcomes),
            visits=tuple(harvest.visits),
            resolve_duplicates=resolve_duplicates,
            list_sources=frozenset(get_feeds()),
        ),
        harvest.collected,
    )
    yield ledger.settle(
        source,
        harvest,
        counts,
        metrics,
        Timings(_since(clock), fetch_ms, normalize_ms, write_ms),
        deferred=len(plan.rotation.deferred) + harvest.deferred_boards,
    )


def _read_recently(
    last_success: datetime | None, window_h: float, run_started_at: datetime
) -> bool:
    if last_success is None:
        return False
    since = run_started_at - timedelta(hours=max(0.0, window_h - REFRESH_SLACK_H))
    return since <= last_success <= run_started_at


async def _run_feed_source(
    repository: AsyncRepository,
    feed: FeedAdapter,
    run_started_at: datetime,
    shuffler: random.Random,
    dry_run: bool,
    ledger: RunLedger,
    rate_state: Mapping[str, RateState],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    force_refresh: bool = False,
) -> AsyncGenerator[SyncEvent]:
    source = feed.name
    block = _active_block(rate_state, _feed_hosts(feed), run_started_at)
    if block is not None:
        yield ledger.block(source, block, run_started_at, dry_run)
        return

    clock = time.perf_counter()
    yield SourceStarted(source=source, companies=0)
    posture = resolve(feed.rate_profile, [])
    seen = (await _visits(repository, source)).get(source)
    last_read = seen.last_success_at if seen is not None else None
    if (
        posture.refresh_interval_h > 0
        and not (force_refresh or dry_run)
        and _read_recently(last_read, posture.refresh_interval_h, run_started_at)
    ):
        yield SourceFresh(
            source=source, skipped=1, remaining=0, refresh_interval_h=posture.refresh_interval_h
        )
        yield _idle_finish(source, clock)
        return

    fetch_clock = time.perf_counter()
    seed = dict(await repository.load_validators(source))
    planned = feed.plan(run_started_at)
    if dry_run:
        for bucket in _bucket_keys(feed.hosts, feed.bucket_key):
            ledger.plan_bounds.append((bucket, source, len(planned), len(planned)))
        for url in planned:
            yield _planned(source, f"season {feed.season_year(run_started_at)}", url, seed)
        yield _idle_finish(source, clock)
        return

    cache = ValidatorCache(seed)
    harvest = FetchHarvest()
    label = f"{source} {feed.season_year(run_started_at)}"
    async with HttpClient(
        allowed_hosts=feed.hosts,
        posture=posture,
        cache=cache,
        rng=shuffler,
        bucket_key=feed.bucket_key,
        rate_state=rate_state,
        now=run_started_at,
        budgets=budgets,
        postures=postures,
    ) as client:
        yield CompanyStarted(source=source, company=label)
        attempt = await _attempt(
            partial(feed.fetch, client, run_started_at), source, label, planned
        )
        for record in client.drain_requests(source):
            yield record
        if attempt.fresh is not None and last_read is None:
            ledger.first_reads.append(source)
        yield harvest.take(source, attempt, first_read=last_read is None)
        metrics = ClientMetrics.of(client)
        validators = _keepable_validators(cache, harvest.skip_urls)
        settled = client.rate_state(run_started_at)

    fetch_ms = _since(fetch_clock)
    counts, normalize_ms, write_ms = await _persist_source_batch(
        repository,
        ledger.screener,
        SourceBatch(
            source=source,
            run_started_at=run_started_at,
            validators=validators,
            rate_state=settled,
            resolve_duplicates=resolve_duplicates,
            closes_whole_source=harvest.whole,
            detail_fetches=tuple(harvest.detail_outcomes),
            visits=tuple(harvest.visits),
        ),
        harvest.collected,
    )
    yield ledger.settle(
        source,
        harvest,
        counts,
        metrics,
        Timings(_since(clock), fetch_ms, normalize_ms, write_ms),
        deferred=harvest.deferred_boards,
    )


def _count_check_requests(ledger: RunLedger, records: Sequence[RequestLogged]) -> None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.source] = counts.get(record.source, 0) + 1
    for source, requests in counts.items():
        index = next(
            (index for index, stat in enumerate(ledger.stats) if stat.source == source), None
        )
        if index is None:
            ledger.stats.append(SourceRunStats(source=source, requests=requests))
        else:
            stat = ledger.stats[index]
            ledger.stats[index] = replace(stat, requests=stat.requests + requests)


async def sync(
    repository: AsyncRepository,
    companies: Sequence[Company],
    *,
    sources: Sequence[str] | None = None,
    excluded: Sequence[str] | None = None,
    dry_run: bool = False,
    force_refresh: bool = False,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    rng: random.Random | None = None,
    close_orphans: bool = True,
    still_open: bool = True,
) -> AsyncGenerator[SyncEvent]:
    grouped, feeds, unroutable = _select(companies, sources, excluded)
    shuffler = rng or random.Random()

    run_started_at = now_fn()
    run_clock = time.perf_counter()
    yield SyncStarted(
        sources=tuple(sorted(set(grouped) | set(feeds))),
        companies=sum(len(entry[1]) for entry in grouped.values()),
        started_at=run_started_at,
    )
    if unroutable:
        yield UnroutableCompanies(
            companies=tuple(company.name for company in unroutable),
            platforms=tuple(sorted({company.platform.value for company in unroutable})),
        )

    ledger = RunLedger()
    rate_state = await repository.load_rate_state()
    pacing = await _pacing(repository, run_started_at, tuple(get_feeds()))
    budgets: dict[str, HostBudget] = {}
    postures = _bucket_postures(grouped, feeds)
    shared = (shuffler, dry_run, ledger, rate_state, budgets, postures)
    streams: list[tuple[str, AsyncGenerator[SyncEvent]]] = [
        (
            name,
            _run_company_source(
                repository,
                adapter,
                members,
                run_started_at,
                *shared,
                force_refresh,
                pacing,
            ),
        )
        for name, (adapter, members) in sorted(grouped.items())
    ]
    streams.extend(
        (
            name,
            _run_feed_source(repository, feeds[name], run_started_at, *shared, force_refresh),
        )
        for name in sorted(feeds)
    )

    switch = sys.getswitchinterval()
    sys.setswitchinterval(min(switch, GIL_SWITCH_S))
    parse_pages_with(ledger.screener.parse)
    try:
        async with aclosing(_merge(streams, ledger)) as merged:
            async for event in merged:
                yield event
    finally:
        sys.setswitchinterval(switch)
        parse_pages_with(None)
        ledger.screener.close()

    if dry_run:
        for event in _bucket_plans(ledger.plan_bounds, postures):
            yield event
        yield SyncFinished(
            outcome=(SyncOutcome.PARTIAL if ledger.failed or unroutable else SyncOutcome.SUCCESS),
            added=0,
            updated=0,
            closed=0,
            failed_sources=tuple(ledger.failed),
            elapsed_ms=_since(run_clock),
            dry_run=True,
        )
        return

    checked_count = checked_gone = 0
    if still_open and ledger.succeeded and sources is None and not excluded:
        from stage.services.liveness import check_still_open

        checks = await check_still_open(
            repository,
            rate_state=rate_state,
            budgets=budgets,
            postures=postures,
            now=now_fn(),
            rng=shuffler,
            clock=now_fn,
        )
        for record in checks.requests:
            yield record
        _count_check_requests(ledger, checks.requests)
        checked_count, checked_gone = checks.checked, checks.closed

    outcome = ledger.outcome(unroutable)
    partial_reason = (
        _partial_reason(ledger.failed, unroutable, ledger.blocked, ledger.deferred)
        if outcome is not SyncOutcome.SUCCESS
        else ""
    )

    orphaned = 0
    if close_orphans:
        sources_with_boards, known_boards = _registry_boards(companies)
        orphaned = await repository.close_orphan_boards(sources_with_boards, known_boards)

    from stage.services.tracker import follow_recorded

    purged = await repository.purge(now_fn())
    await follow_recorded(repository)

    await repository.record_sync_run(
        SyncRun(
            started_at=run_started_at,
            finished_at=now_fn(),
            outcome=outcome,
            sources=tuple(ledger.stats),
        )
    )
    yield SyncFinished(
        outcome=outcome,
        added=sum(item.added for item in ledger.stats),
        updated=sum(item.updated for item in ledger.stats),
        closed=sum(item.closed for item in ledger.stats) + checked_gone + orphaned,
        quarantined=sum(item.quarantined for item in ledger.stats),
        purged=purged.purged,
        failed_sources=tuple(ledger.failed),
        elapsed_ms=_since(run_clock),
        requests=sum(item.requests for item in ledger.stats),
        not_modified=sum(item.not_modified for item in ledger.stats),
        partial_reason=partial_reason,
        first_reads=tuple(sorted(set(ledger.first_reads))),
        checked=checked_count,
        checked_gone=checked_gone,
    )
