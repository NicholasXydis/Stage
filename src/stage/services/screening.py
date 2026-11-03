import asyncio
import multiprocessing
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from typing import cast

from stage.classify import (
    classify_role,
    resolve_eligibility,
    screen_degree_scope,
    screen_is_cs_role,
    screen_is_internship,
    screen_location,
    to_quarantined,
)
from stage.domain import Job, QuarantinedJob
from stage.normalize import (
    canonical_apply_url,
    detect_language,
    listed_place,
    resolve_listing,
    resolve_term,
)

POOL_THRESHOLD = 500
CHUNK_SIZE = 1_000
WORKERS = 2

Screened = tuple[tuple[Job, ...], tuple[QuarantinedJob, ...]]
_PARSE_WAITERS = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="stage-parse")


EARLIEST_POSTED = datetime(1990, 1, 1, tzinfo=UTC)


def _plausible(posted: datetime | None, seen: datetime) -> datetime | None:
    if posted is None:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    latest = (seen if seen.tzinfo else seen.replace(tzinfo=UTC)) + timedelta(days=366)
    return posted if EARLIEST_POSTED <= posted <= latest else None


def normalize_batch(jobs: Sequence[Job]) -> Screened:
    kept: list[Job] = []
    rejected: list[QuarantinedJob] = []
    for job in jobs:
        job = replace(
            job,
            location_raw=listed_place(job.location_raw, job.apply_url_raw, job.title_raw),
            source_posted_at=_plausible(job.source_posted_at, job.first_seen),
        )
        location = resolve_listing(job.location_raw, job.title_raw)
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
            or screen_location(normalized)
            or screen_degree_scope(normalized)
            or screen_is_cs_role(normalized)
        )
        if rejection is None:
            kept.append(normalized)
        else:
            rejected.append(to_quarantined(normalized, rejection))
    return tuple(kept), tuple(rejected)


def _ignore_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    parent = multiprocessing.parent_process()
    if parent is not None:
        threading.Thread(target=_exit_with, args=(parent,), daemon=True).start()


def _exit_with(parent: multiprocessing.process.BaseProcess) -> None:
    parent.join()
    os._exit(0)


def _screen_in(pool: ProcessPoolExecutor, chunks: list[list[Job]]) -> list[Screened]:
    with _interrupts_held():
        results = pool.map(normalize_batch, chunks)
    return list(results)


def _outcome[R](call: Callable[[], R]) -> tuple[R | None, Exception | None]:
    try:
        return call(), None
    except Exception as exc:
        return None, exc


def _parse_in[R](
    pool: ProcessPoolExecutor, call: Callable[[], R]
) -> tuple[R | None, Exception | None]:
    with _interrupts_held():
        future = pool.submit(_outcome, call)
    return future.result()


if sys.platform == "win32":

    @contextmanager
    def _interrupts_held() -> Iterator[None]:
        yield

else:

    @contextmanager
    def _interrupts_held() -> Iterator[None]:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextmanager
def _terminal_stderr() -> Iterator[None]:
    captured = sys.stderr
    if sys.__stderr__ is not None:
        sys.stderr = sys.__stderr__
    try:
        yield
    finally:
        sys.stderr = captured


class Screener:
    def __init__(self, workers: int = WORKERS, threshold: int = POOL_THRESHOLD) -> None:
        self.workers = max(1, min(workers, os.cpu_count() or 1))
        self.threshold = threshold
        self.broken = False
        self._pool: ProcessPoolExecutor | None = None

    async def __call__(self, jobs: Sequence[Job]) -> Screened:
        from stage.sources.base import off_loop

        batch = list(jobs)
        if self.broken or len(batch) < self.threshold:
            return await off_loop(normalize_batch, batch)
        chunks = [batch[start : start + CHUNK_SIZE] for start in range(0, len(batch), CHUNK_SIZE)]
        try:
            results = await asyncio.to_thread(_screen_in, self._started(), chunks)
        except Exception:
            self._give_up()
            return await off_loop(normalize_batch, batch)
        return (
            tuple(job for kept, _ in results for job in kept),
            tuple(entry for _, rejected in results for entry in rejected),
        )

    async def parse[R](self, call: Callable[[], R]) -> R:
        from stage.sources.base import off_loop

        if not self.broken:
            loop = asyncio.get_running_loop()
            try:
                value, error = await loop.run_in_executor(
                    _PARSE_WAITERS, _parse_in, self._started(), call
                )
            except Exception:
                self._give_up()
            else:
                if error is not None:
                    raise error
                return cast(R, value)
        return await off_loop(call)

    def _give_up(self) -> None:
        self.broken = True
        self.close()

    def _started(self) -> ProcessPoolExecutor:
        if self._pool is None:
            with _terminal_stderr():
                self._pool = ProcessPoolExecutor(
                    max_workers=self.workers,
                    mp_context=get_context("spawn"),
                    initializer=_ignore_interrupts,
                )
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
