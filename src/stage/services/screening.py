import asyncio
import os
import signal
import sys
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from multiprocessing import get_context

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
    resolve_location,
    resolve_term,
)

POOL_THRESHOLD = 500
CHUNK_SIZE = 1_000
WORKERS = 2

Screened = tuple[tuple[Job, ...], tuple[QuarantinedJob, ...]]


def normalize_batch(jobs: Sequence[Job]) -> Screened:
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


def _screen_in(pool: ProcessPoolExecutor, chunks: list[list[Job]]) -> list[Screened]:
    return list(pool.map(normalize_batch, chunks))


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
        batch = list(jobs)
        if self.broken or len(batch) < self.threshold:
            return await asyncio.to_thread(normalize_batch, batch)
        chunks = [batch[start : start + CHUNK_SIZE] for start in range(0, len(batch), CHUNK_SIZE)]
        try:
            results = await asyncio.to_thread(_screen_in, self._started(), chunks)
        except Exception:
            self.broken = True
            self.close()
            return await asyncio.to_thread(normalize_batch, batch)
        return (
            tuple(job for kept, _ in results for job in kept),
            tuple(entry for _, rejected in results for entry in rejected),
        )

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
