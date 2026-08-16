import asyncio
import random
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from stage.classify import (
    classify_role,
    resolve_eligibility,
    screen_degree_scope,
    screen_is_cs_role,
    screen_is_internship,
    to_quarantined,
)
from stage.dedup import resolve_duplicates
from stage.domain import (
    BucketPlan,
    Company,
    CompanyFailed,
    CompanyFinished,
    CompanyStarted,
    CompanyUnchanged,
    CompanyVisit,
    DetailFetch,
    HttpValidator,
    Job,
    PlannedRequest,
    Priority,
    QuarantinedJob,
    RateState,
    RequestLogged,
    Rotation,
    RotationMember,
    SourceBlocked,
    SourceCapped,
    SourceFailed,
    SourceFinished,
    SourceFresh,
    SourceRotated,
    SourceRunStats,
    SourceStarted,
    SyncEvent,
    SyncFinished,
    SyncOutcome,
    SyncRun,
    SyncStarted,
    UnroutableCompanies,
    WorkdayCrawl,
    WorkdayCrawlStep,
    WorkdayFacet,
    rotate,
)
from stage.http import (
    CEILING_BACKSTOP,
    HostBudget,
    HttpClient,
    RatePosture,
    ValidatorCache,
    resolve,
)
from stage.normalize import (
    canonical_apply_url,
    detect_language,
    resolve_location,
    resolve_term,
)
from stage.sources import (
    Adapter,
    FeedAdapter,
    FetchResult,
    adapter_for_platform,
    get_adapters,
    get_feeds,
)
from stage.sources.workday import WorkdayAdapter
from stage.storage import AsyncRepository, SourceBatch, SourceBatchResult


class NoSourcesSelectedError(Exception):
    pass


def _bucket_postures(
    grouped: Mapping[str, tuple[Adapter, list[Company]]], feeds: Mapping[str, FeedAdapter]
) -> dict[str, RatePosture]:
    claims: list[tuple[frozenset[str], str, RatePosture]] = [
        (
            adapter.hosts,
            adapter.bucket_key,
            resolve(
                adapter.rate_profile, [company.rate_profile for company in companies]
            ).sized_for(len(companies), _reserve_for(adapter)),
        )
        for adapter, companies in grouped.values()
    ]
    claims.extend(
        (feed.hosts, feed.bucket_key, resolve(feed.rate_profile, [])) for feed in feeds.values()
    )

    postures: dict[str, RatePosture] = {}
    for hosts, bucket_key, posture in claims:
        for bucket in _bucket_keys(hosts, bucket_key):
            stored = postures.get(bucket)
            postures[bucket] = posture if stored is None else stored.strictest(posture)
    return postures


def _bucket_keys(hosts: frozenset[str], bucket_key: str) -> tuple[str, ...]:
    return (bucket_key,) if bucket_key else tuple(sorted(hosts))


def _reserve_for(adapter: Adapter) -> int:
    return getattr(adapter, "retry_reserve", 0) + adapter.detail_budget


DAILY_RUNS = 4


async def _spent_today(repository: AsyncRepository, since: datetime) -> tuple[dict[str, int], bool]:
    runs = await repository.run_history(DAILY_RUNS * 4)
    spent: dict[str, int] = {}
    seen_any = False
    for run in runs:
        if run.started_at < since:
            continue
        seen_any = True
        for entry in run.sources:
            spent[entry.source] = spent.get(entry.source, 0) + entry.requests
    return spent, seen_any


def _daily_allowance(posture: RatePosture, spent: int, has_history: bool) -> int:
    if not has_history:
        return posture.max_requests_per_run
    cap = min(CEILING_BACKSTOP * 2, posture.max_requests_per_run * DAILY_RUNS)
    return max(0, min(posture.max_requests_per_run, cap - spent))


def _active_block(
    rate_state: Mapping[str, RateState], buckets: Sequence[str], now: datetime
) -> RateState | None:
    for bucket in buckets:
        state = rate_state.get(bucket)
        if state is not None and state.is_blocked(now):
            return state
    return None


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


def _select(
    companies: Sequence[Company],
    sources: Sequence[str] | None,
    excluded: Sequence[str] | None = None,
) -> tuple[dict[str, tuple[Adapter, list[Company]]], dict[str, FeedAdapter], list[Company]]:
    grouped, unroutable = _group_by_adapter(companies)
    feeds = dict(get_feeds())
    known = set(grouped) | set(feeds) | set(get_adapters())
    if sources is not None:
        selected = set(sources)
        unknown = selected - known
        if unknown:
            raise NoSourcesSelectedError(
                f"no enabled companies for source(s): {', '.join(sorted(unknown))}"
            )
        grouped = {name: value for name, value in grouped.items() if name in selected}
        feeds = {name: value for name, value in feeds.items() if name in selected}
    if excluded:
        dropped = set(excluded)
        unknown = dropped - known
        if unknown:
            raise NoSourcesSelectedError(
                f"unknown source(s) to exclude: {', '.join(sorted(unknown))}"
            )
        grouped = {name: value for name, value in grouped.items() if name not in dropped}
        feeds = {name: value for name, value in feeds.items() if name not in dropped}
    if not grouped and not feeds:
        detail = ""
        if unroutable:
            platforms = ", ".join(sorted({company.platform.value for company in unroutable}))
            detail = f" ({len(unroutable)} enabled row(s) sit on unrouted platform(s): {platforms})"
        raise NoSourcesSelectedError(
            f"the registry has no enabled companies with a matching adapter{detail}"
        )
    return grouped, feeds, unroutable


def normalize_batch(jobs: Sequence[Job]) -> tuple[tuple[Job, ...], tuple[QuarantinedJob, ...]]:
    kept: list[Job] = []
    rejected: list[QuarantinedJob] = []
    for job in jobs:
        location = resolve_location(job.location_raw)
        term = resolve_term(
            title=job.title_raw,
            description=job.description,
            structured_terms=job.signals.terms,
            structured_season=job.signals.season,
        )
        role = classify_role(job.title_raw, job.description, job.signals.category)
        language = detect_language(job.title_raw, job.description)
        eligibility = resolve_eligibility(job)
        normalized = replace(
            job,
            apply_url_canonical=canonical_apply_url(job.apply_url_raw),
            location=location.bucket,
            remote_scope=location.remote_scope,
            term=term.term,
            role=role.role,
            language=language.language,
            degree_requirement=eligibility.degree_requirement,
            work_auth_flag=eligibility.work_auth_flag,
        )
        rejection = (
            screen_is_internship(normalized)
            or screen_degree_scope(normalized)
            or screen_is_cs_role(normalized)
        )
        if rejection is None:
            kept.append(normalized)
        else:
            rejected.append(to_quarantined(normalized, rejection))
    return tuple(kept), tuple(rejected)


async def _fetch_company(
    adapter: Adapter,
    company: Company,
    client: HttpClient,
    now: datetime,
    facets: Mapping[tuple[str, str], WorkdayFacet],
    details: Sequence[str],
    crawls: Mapping[str, WorkdayCrawl],
    page_budget: int,
    final_workday_pass: bool,
) -> tuple[Company, FetchResult | None, str, float]:
    started = time.perf_counter()
    try:
        if isinstance(adapter, WorkdayAdapter):
            result = await adapter.fetch(
                company,
                client,
                now,
                facets,
                details,
                crawl=crawls.get(_safe_board_key(adapter, company)),
                page_budget=page_budget,
            )
            crawl = result.workday_crawl
            if (
                final_workday_pass
                and crawl is not None
                and not crawl.complete
                and not crawl.discard
            ):
                message = (
                    "resumed once and still incomplete; postings stay open, crawl state discarded"
                )
                result = replace(
                    result,
                    degraded=f"{result.degraded}; {message}" if result.degraded else message,
                    workday_crawl=replace(crawl, discard=True),
                )
        else:
            result = await adapter.fetch(company, client, now, facets, details)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return company, None, f"{type(exc).__name__}: {exc}", elapsed
    return company, result, "", (time.perf_counter() - started) * 1000


def _keepable_validators(cache: ValidatorCache, skip_urls: set[str]) -> tuple[HttpValidator, ...]:
    return tuple(validator for url, validator in cache.pending.items() if url not in skip_urls)


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
            ceiling=postures[bucket].max_requests_per_run
            if bucket in postures
            else RatePosture().max_requests_per_run,
        )
        for bucket in sorted(planned)
    )


def _safe_plan(adapter: Adapter, company: Company) -> tuple[tuple[str, ...], str]:
    try:
        return adapter.plan(company), ""
    except Exception as exc:
        return (), f"{type(exc).__name__}: {exc}"


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


def _drain(client: HttpClient, source: str) -> list[RequestLogged]:
    return [
        RequestLogged(
            source=source,
            method=record.method,
            url=record.url,
            status=record.status,
            elapsed_ms=record.elapsed_ms,
            attempt=record.attempt,
            error=record.error,
        )
        for record in client.drain_log()
    ]


async def _merge(
    streams: Sequence[tuple[str, AsyncIterator[SyncEvent]]],
    failed_sources: list[str],
    stats: list[SourceRunStats],
) -> AsyncIterator[SyncEvent]:
    if len(streams) == 1:
        source, stream = streams[0]
        try:
            async for event in stream:
                yield event
        except asyncio.CancelledError:
            raise
        except Exception as stream_exc:
            error = f"{type(stream_exc).__name__}: {stream_exc}"
            failed_sources.append(source)
            stats.append(SourceRunStats(source=source, errors=1))
            yield SourceFailed(source=source, error=error)
        return

    queue: asyncio.Queue[SyncEvent | tuple[str, Exception] | None] = asyncio.Queue()

    async def pump(source: str, stream: AsyncIterator[SyncEvent]) -> None:
        try:
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
                source, queued_exc = item
                error = f"{type(queued_exc).__name__}: {queued_exc}"
                failed_sources.append(source)
                stats.append(SourceRunStats(source=source, errors=1))
                yield SourceFailed(source=source, error=error)
            else:
                yield item
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _plan_company_source(
    adapter: Adapter, companies: Sequence[Company], validators: dict[str, HttpValidator]
) -> AsyncIterator[SyncEvent]:
    for company in companies:
        urls, error = _safe_plan(adapter, company)
        if error:
            yield CompanyFailed(
                source=adapter.name, company=company.name, error=error, elapsed_ms=0.0
            )
            continue
        for url in urls:
            cached = validators.get(url)
            has_validator = cached is not None and cached.usable
            yield PlannedRequest(
                source=adapter.name,
                company=company.name,
                url=url,
                has_validator=has_validator,
                expectation="likely 304 (validator on file)" if has_validator else "full response",
            )


async def _run_company_source(
    repository: AsyncRepository,
    adapter: Adapter,
    companies: Sequence[Company],
    run_started_at: datetime,
    shuffler: random.Random,
    dry_run: bool,
    stats: list[SourceRunStats],
    failed: list[str],
    succeeded_any: list[bool],
    rate_state: Mapping[str, RateState],
    blocked_sources: list[str],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    plan_bounds: list[tuple[str, str, int, int]],
    force_refresh: bool,
    spent_today: Mapping[str, int],
    has_history: bool,
) -> AsyncIterator[SyncEvent]:
    source_name = adapter.name
    buckets = _bucket_keys(adapter.hosts, adapter.bucket_key)
    block = _active_block(rate_state, buckets, run_started_at)
    if block is not None:
        blocked_sources.append(source_name)
        if not dry_run:
            stats.append(SourceRunStats(source=source_name, blocked=True))
        yield SourceBlocked(
            source=source_name,
            bucket=block.bucket,
            blocked_until=block.blocked_until or run_started_at,
            remaining_s=block.blocks_remaining_s(run_started_at),
            reason=block.reason,
            consecutive_failures=block.consecutive_failures,
        )
        return

    rotation_bucket = buckets[0]
    stored = rate_state.get(rotation_bucket)
    workday_adapter = adapter if isinstance(adapter, WorkdayAdapter) else None
    is_workday = workday_adapter is not None
    window_h = postures.get(rotation_bucket, RatePosture()).refresh_interval_h
    last_success = {
        visit.board: visit.last_success_at
        for visit in await repository.all_visits()
        if visit.source == source_name
    }
    rotation = rotate(
        [
            RotationMember(
                key=company.registry_key,
                always=company.priority is Priority.HIGH and not is_workday,
            )
            for company in companies
        ],
        cursor=stored.rotation_cursor if stored is not None else "",
        budget=adapter.rotation_slice,
    )
    covered = set(rotation.selected)
    ordered = [company for company in companies if company.registry_key in covered]
    source_clock = time.perf_counter()
    fetch_clock = time.perf_counter()

    open_crawls = (
        frozenset(await repository.load_workday_crawls())
        if workday_adapter is not None
        else frozenset()
    )
    refreshed_recently = 0
    if window_h > 0 and not force_refresh and not dry_run:
        since = run_started_at - timedelta(hours=window_h)
        kept_boards = [
            company
            for company in ordered
            if _safe_board_key(adapter, company) in open_crawls
            or (seen := last_success.get(_safe_board_key(adapter, company))) is None
            or seen < since
        ]
        refreshed_recently = len(ordered) - len(kept_boards)
        ordered = kept_boards

    seed = dict(await repository.load_validators(source_name))
    resuming_boards: frozenset[str] = frozenset()
    base = postures.get(rotation_bucket) or resolve(
        adapter.rate_profile, [company.rate_profile for company in ordered]
    )
    allowance = _daily_allowance(base, spent_today.get(source_name, 0), has_history)
    posture = replace(base, max_requests_per_run=allowance)
    if workday_adapter is not None:
        facets = await repository.load_workday_facets()
        crawls = await repository.load_workday_crawls()
        resuming_boards = frozenset(crawls)
        page_budgets, detail_budget = (
            workday_adapter.crawl_budgets(ordered, crawls, facets, posture.max_requests_per_run)
            if ordered
            else ({}, 0)
        )
    else:
        page_budgets = {
            company.registry_key: adapter.max_requests_per_company for company in ordered
        }
        detail_budget = adapter.detail_budget
        facets = {}
        crawls = {}
    shuffler.shuffle(ordered)
    ordered = _stalest_first(adapter, ordered, last_success)
    yield SourceStarted(source=source_name, companies=len(ordered))
    if allowance < base.max_requests_per_run:
        yield SourceCapped(
            source=source_name,
            bucket=rotation_bucket,
            spent=spent_today.get(source_name, 0),
            allowance=allowance,
            ceiling=base.max_requests_per_run,
        )
    if refreshed_recently:
        yield SourceFresh(
            source=source_name,
            skipped=refreshed_recently,
            remaining=len(ordered),
            refresh_interval_h=window_h,
        )
    if rotation.rotating:
        yield SourceRotated(
            source=source_name,
            bucket=rotation_bucket,
            selected=len(rotation.selected),
            deferred=len(rotation.deferred),
            cursor=rotation.cursor,
            wrapped=rotation.wrapped,
        )
    detail_queue = await repository.detail_queue(source_name, detail_budget)

    if dry_run:
        worst_case = sum(page_budgets.values()) + (detail_budget if is_workday else 0)
        plan_bounds.append((rotation_bucket, source_name, len(ordered), worst_case))
        async for event in _plan_company_source(adapter, ordered, seed):
            yield event
        yield SourceFinished(
            source=source_name,
            fetched=0,
            added=0,
            updated=0,
            closed=0,
            failed_companies=0,
            elapsed_ms=(time.perf_counter() - source_clock) * 1000,
        )
        return

    cache = ValidatorCache(seed)
    collected: list[Job] = []
    closable: list[str] = []
    unchanged: list[str] = []
    skip_urls: set[str] = set()
    visits: list[CompanyVisit] = []
    resolved_facets: list[WorkdayFacet] = []
    forgotten_facets: list[WorkdayFacet] = []
    workday_crawls: list[WorkdayCrawlStep] = []
    detail_outcomes: list[DetailFetch] = []
    errors = 0
    successful_results = False

    async with HttpClient(
        allowed_hosts=adapter.hosts_for(ordered),
        posture=posture,
        cache=cache,
        rng=shuffler,
        bucket_key=adapter.bucket_key,
        rate_state=rate_state,
        now=run_started_at,
        budgets=budgets,
        postures={**postures, rotation_bucket: posture},
    ) as client:
        pending = [
            _fetch_company(
                adapter,
                company,
                client,
                run_started_at,
                facets,
                detail_queue,
                crawls,
                page_budgets[company.registry_key],
                _safe_board_key(adapter, company) in resuming_boards,
            )
            for company in ordered
        ]
        for company in ordered:
            yield CompanyStarted(source=source_name, company=company.name)

        for company, result, error, elapsed in await asyncio.gather(*pending):
            for record in _drain(client, source_name):
                yield record
            board = _safe_board_key(adapter, company)
            if result is None:
                errors += 1
                skip_urls.update(_safe_plan(adapter, company)[0])
                visits.append(
                    CompanyVisit(board=board, succeeded=False, error=error, label=company.name)
                )
                yield CompanyFailed(
                    source=source_name, company=company.name, error=error, elapsed_ms=elapsed
                )
            elif result.not_modified:
                visits.append(CompanyVisit(board=board, succeeded=True, label=company.name))
                unchanged.append(board)
                yield CompanyUnchanged(source=source_name, company=company.name, elapsed_ms=elapsed)
            else:
                successful_results = True
                visits.append(CompanyVisit(board=board, succeeded=True, label=company.name))
                collected.extend(result.jobs)
                resolved_facets.extend(
                    entry for entry in result.facets if isinstance(entry, WorkdayFacet)
                )
                forgotten_facets.extend(
                    entry for entry in result.forgotten_facets if isinstance(entry, WorkdayFacet)
                )
                detail_outcomes.extend(
                    entry for entry in result.detail_fetches if isinstance(entry, DetailFetch)
                )
                if isinstance(result.workday_crawl, WorkdayCrawlStep):
                    workday_crawls.append(result.workday_crawl)
                if result.authoritative:
                    closable.append(board)
                if result.degraded:
                    skip_urls.update(_safe_plan(adapter, company)[0])
                    skip_urls.update(result.stale_urls)
                yield CompanyFinished(
                    source=source_name,
                    company=company.name,
                    fetched=len(result.jobs),
                    elapsed_ms=elapsed,
                    degraded=result.degraded,
                )

        metrics = _client_metrics(client)
        validators = _keepable_validators(cache, skip_urls)
        settled = _advance_cursor(
            client.rate_state(run_started_at), rotation, rotation_bucket, run_started_at
        )

    fetch_ms = (time.perf_counter() - fetch_clock) * 1000
    normalize_clock = time.perf_counter()
    kept, rejected = await asyncio.to_thread(normalize_batch, collected)
    normalize_ms = (time.perf_counter() - normalize_clock) * 1000
    write_clock = time.perf_counter()
    counts = await repository.apply_source_batch(
        SourceBatch(
            source=source_name,
            run_started_at=run_started_at,
            jobs=kept,
            closable_boards=tuple(closable),
            unchanged_boards=tuple(unchanged),
            validators=validators,
            rate_state=settled,
            workday_facets=tuple(resolved_facets),
            forgotten_facets=tuple(forgotten_facets),
            workday_crawls=tuple(workday_crawls),
            detail_fetches=tuple(detail_outcomes),
            visits=tuple(visits),
            quarantined=rejected,
            resolve_duplicates=resolve_duplicates,
        )
    )
    write_ms = (time.perf_counter() - write_clock) * 1000
    if successful_results or unchanged:
        succeeded_any.append(True)
    if errors:
        failed.append(source_name)

    elapsed_ms = (time.perf_counter() - source_clock) * 1000
    stats.append(
        replace(
            _stats(source_name, counts, errors, metrics, elapsed_ms),
            deferred=len(rotation.deferred),
        )
    )
    yield _finished(
        source_name,
        counts,
        errors,
        metrics,
        elapsed_ms,
        fetch_ms=fetch_ms,
        normalize_ms=normalize_ms,
        write_ms=write_ms,
    )


async def _run_feed_source(
    repository: AsyncRepository,
    feed: FeedAdapter,
    run_started_at: datetime,
    shuffler: random.Random,
    dry_run: bool,
    stats: list[SourceRunStats],
    failed: list[str],
    succeeded_any: list[bool],
    rate_state: Mapping[str, RateState],
    blocked_sources: list[str],
    budgets: dict[str, HostBudget],
    postures: Mapping[str, RatePosture],
    plan_bounds: list[tuple[str, str, int, int]],
) -> AsyncIterator[SyncEvent]:
    source_name = feed.name
    block = _active_block(rate_state, _bucket_keys(feed.hosts, feed.bucket_key), run_started_at)
    if block is not None:
        blocked_sources.append(source_name)
        if not dry_run:
            stats.append(SourceRunStats(source=source_name, blocked=True))
        yield SourceBlocked(
            source=source_name,
            bucket=block.bucket,
            blocked_until=block.blocked_until or run_started_at,
            remaining_s=block.blocks_remaining_s(run_started_at),
            reason=block.reason,
            consecutive_failures=block.consecutive_failures,
        )
        return

    source_clock = time.perf_counter()
    yield SourceStarted(source=source_name, companies=0)

    fetch_clock = time.perf_counter()
    seed = dict(await repository.load_validators(source_name))

    if dry_run:
        planned_urls = feed.plan(run_started_at)
        for bucket in _bucket_keys(feed.hosts, feed.bucket_key):
            plan_bounds.append((bucket, source_name, len(planned_urls), len(planned_urls)))
        for url in planned_urls:
            cached = seed.get(url)
            has_validator = cached is not None and cached.usable
            yield PlannedRequest(
                source=source_name,
                company=f"season {feed.season_year(run_started_at)}",
                url=url,
                has_validator=has_validator,
                expectation="likely 304 (validator on file)" if has_validator else "full response",
            )
        yield SourceFinished(
            source=source_name,
            fetched=0,
            added=0,
            updated=0,
            closed=0,
            failed_companies=0,
            elapsed_ms=(time.perf_counter() - source_clock) * 1000,
        )
        return

    cache = ValidatorCache(seed)
    collected: list[Job] = []
    errors = 0
    unchanged = False
    degraded = ""
    authoritative = True
    stale_urls: tuple[str, ...] = ()
    label = f"{source_name} {feed.season_year(run_started_at)}"

    async with HttpClient(
        allowed_hosts=feed.hosts,
        posture=resolve(feed.rate_profile, []),
        cache=cache,
        rng=shuffler,
        bucket_key=feed.bucket_key,
        rate_state=rate_state,
        now=run_started_at,
        budgets=budgets,
        postures=postures,
    ) as client:
        yield CompanyStarted(source=source_name, company=label)
        started = time.perf_counter()
        try:
            result = await feed.fetch(client, run_started_at)
        except Exception as exc:
            errors = 1
            result = None
            error_text = f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000

        for record in _drain(client, source_name):
            yield record

        if result is None:
            yield CompanyFailed(
                source=source_name, company=label, error=error_text, elapsed_ms=elapsed
            )
        elif result.not_modified:
            unchanged = True
            yield CompanyUnchanged(source=source_name, company=label, elapsed_ms=elapsed)
        else:
            collected.extend(result.jobs)
            degraded = result.degraded
            authoritative = result.authoritative
            stale_urls = result.stale_urls
            yield CompanyFinished(
                source=source_name,
                company=label,
                fetched=len(result.jobs),
                elapsed_ms=elapsed,
                degraded=result.degraded,
            )

        metrics = _client_metrics(client)
        incomplete = bool(errors) or bool(degraded)
        skip_urls = set(feed.plan(run_started_at)) | set(stale_urls) if incomplete else set()
        validators = _keepable_validators(cache, skip_urls)
        settled = client.rate_state(run_started_at)

    fetch_ms = (time.perf_counter() - fetch_clock) * 1000
    normalize_clock = time.perf_counter()
    kept, rejected = await asyncio.to_thread(normalize_batch, collected)
    normalize_ms = (time.perf_counter() - normalize_clock) * 1000
    write_clock = time.perf_counter()
    counts = await repository.apply_source_batch(
        SourceBatch(
            source=source_name,
            run_started_at=run_started_at,
            jobs=kept,
            validators=validators,
            rate_state=settled,
            quarantined=rejected,
            resolve_duplicates=resolve_duplicates,
            closes_whole_source=authoritative and not incomplete and not unchanged,
        )
    )
    write_ms = (time.perf_counter() - write_clock) * 1000
    if not errors:
        succeeded_any.append(True)
    else:
        failed.append(source_name)

    elapsed_ms = (time.perf_counter() - source_clock) * 1000
    stats.append(_stats(source_name, counts, errors, metrics, elapsed_ms))
    yield _finished(
        source_name,
        counts,
        errors,
        metrics,
        elapsed_ms,
        fetch_ms=fetch_ms,
        normalize_ms=normalize_ms,
        write_ms=write_ms,
    )


def _client_metrics(client: HttpClient) -> tuple[int, int, int, int, float, float]:
    p50, p95 = client.latency_percentiles()
    return (
        client.request_count,
        client.not_modified_count,
        client.retry_count,
        client.tightening_count,
        p50,
        p95,
    )


def _stats(
    source: str,
    counts: SourceBatchResult,
    errors: int,
    metrics: tuple[int, int, int, int, float, float],
    elapsed_ms: float,
) -> SourceRunStats:
    requests, not_modified, retries, tightenings, p50, p95 = metrics
    return SourceRunStats(
        source=source,
        fetched=counts.fetched,
        added=counts.added,
        updated=counts.updated,
        closed=counts.closed,
        quarantined=counts.quarantined,
        errors=errors,
        requests=requests,
        not_modified=not_modified,
        retries=retries,
        tightenings=tightenings,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        elapsed_ms=elapsed_ms,
        stored=counts.stored,
    )


def _finished(
    source: str,
    counts: SourceBatchResult,
    errors: int,
    metrics: tuple[int, int, int, int, float, float],
    elapsed_ms: float,
    fetch_ms: float = 0.0,
    normalize_ms: float = 0.0,
    write_ms: float = 0.0,
) -> SourceFinished:
    requests, not_modified, retries, tightenings, p50, p95 = metrics
    return SourceFinished(
        source=source,
        fetched=counts.fetched,
        added=counts.added,
        updated=counts.updated,
        closed=counts.closed,
        quarantined=counts.quarantined,
        failed_companies=errors,
        elapsed_ms=elapsed_ms,
        fetch_ms=fetch_ms,
        normalize_ms=normalize_ms,
        write_ms=write_ms,
        requests=requests,
        not_modified=not_modified,
        retries=retries,
        tightenings=tightenings,
        latency_p50_ms=p50,
        latency_p95_ms=p95,
    )


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
) -> AsyncIterator[SyncEvent]:
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

    stats: list[SourceRunStats] = []
    failed_sources: list[str] = []
    succeeded_any: list[bool] = []
    blocked_sources: list[str] = []

    rate_state = await repository.load_rate_state()
    spent_today, has_history = await _spent_today(repository, run_started_at - timedelta(hours=24))
    budgets: dict[str, HostBudget] = {}
    plan_bounds: list[tuple[str, str, int, int]] = []
    postures = _bucket_postures(grouped, feeds)

    streams: list[tuple[str, AsyncIterator[SyncEvent]]] = [
        (
            name,
            _run_company_source(
                repository,
                grouped[name][0],
                grouped[name][1],
                run_started_at,
                shuffler,
                dry_run,
                stats,
                failed_sources,
                succeeded_any,
                rate_state,
                blocked_sources,
                budgets,
                postures,
                plan_bounds,
                force_refresh,
                spent_today,
                has_history,
            ),
        )
        for name in sorted(grouped)
    ]
    streams.extend(
        (
            name,
            _run_feed_source(
                repository,
                feeds[name],
                run_started_at,
                shuffler,
                dry_run,
                stats,
                failed_sources,
                succeeded_any,
                rate_state,
                blocked_sources,
                budgets,
                postures,
                plan_bounds,
            ),
        )
        for name in sorted(feeds)
    )

    async for event in _merge(streams, failed_sources, stats):
        yield event

    if dry_run:
        for event in _bucket_plans(plan_bounds, postures):
            yield event
        yield SyncFinished(
            outcome=(
                SyncOutcome.PARTIAL
                if failed_sources or unroutable or blocked_sources
                else SyncOutcome.SUCCESS
            ),
            added=0,
            updated=0,
            closed=0,
            failed_sources=tuple(failed_sources),
            elapsed_ms=(time.perf_counter() - run_clock) * 1000,
            dry_run=True,
        )
        return

    if not succeeded_any and (failed_sources or blocked_sources):
        outcome_status = SyncOutcome.FAILURE
    elif failed_sources or unroutable or blocked_sources:
        outcome_status = SyncOutcome.PARTIAL
    else:
        outcome_status = SyncOutcome.SUCCESS

    purged = await repository.purge(now_fn())

    await repository.record_sync_run(
        SyncRun(
            started_at=run_started_at,
            finished_at=now_fn(),
            outcome=outcome_status,
            sources=tuple(stats),
        )
    )
    yield SyncFinished(
        outcome=outcome_status,
        added=sum(item.added for item in stats),
        updated=sum(item.updated for item in stats),
        closed=sum(item.closed for item in stats),
        quarantined=sum(item.quarantined for item in stats),
        purged=purged.purged,
        failed_sources=tuple(failed_sources),
        elapsed_ms=(time.perf_counter() - run_clock) * 1000,
        requests=sum(item.requests for item in stats),
        not_modified=sum(item.not_modified for item in stats),
    )
