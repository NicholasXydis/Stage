import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import NamedTuple

from stage.domain import OPEN_RETENTION_DAYS, Job, JobStatus, QuarantinedJob, board_of
from stage.domain.liveness import CHECK_PREFIX, CHECKED_SOURCES
from stage.domain.text import is_workday_opening
from stage.storage.copies import (
    copies_of_rejected,
    duplicate_candidates,
    followers_of_rejected,
    restore_copies,
)
from stage.storage.repository import Resolver, SourceBatch, SourceBatchResult
from stage.storage.rows import (
    CLOSED,
    OPEN,
    UPSERT_CHECK,
    UPSERT_CRAWL,
    UPSERT_DETAIL,
    UPSERT_FACET,
    UPSERT_JOB,
    UPSERT_QUARANTINE,
    UPSERT_RATE_STATE,
    UPSERT_VALIDATOR,
    UPSERT_VISIT,
    job_from,
    job_params,
    quarantined_params,
    rate_state_params,
)
from stage.storage.sql import EACH, Store, board_rows, count, from_text, listed, marks, to_text
from stage.storage.upkeep import (
    heirs_of,
    promote_open_copies,
    promote_orphaned_duplicates,
    remember_heirs,
)

_HEALED = timedelta(microseconds=1)


class _Seen(NamedTuple):
    listed: str | None = None
    rejected: str | None = None
    purged: str | None = None

    def expired(self, cutoff: str) -> bool:
        return (
            self.listed is None
            and self.rejected is None
            and self.purged is not None
            and self.purged < cutoff
        )


_UNSEEN = _Seen()


def _seen(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, _Seen]:
    rows = conn.execute(
        "SELECT w.value AS id, j.first_seen AS listed, q.first_seen AS rejected, "
        "t.first_seen AS purged FROM json_each(?) w "
        "LEFT JOIN jobs j ON j.id = w.value LEFT JOIN quarantine q ON q.id = w.value "
        "LEFT JOIN tombstones t ON t.id = w.value "
        "WHERE j.id IS NOT NULL OR q.id IS NOT NULL OR t.id IS NOT NULL",
        (listed(ids),),
    )
    return {str(row["id"]): _Seen(row["listed"], row["rejected"], row["purged"]) for row in rows}


def _first_seen(seen: dict[str, _Seen], started: datetime) -> dict[str, datetime]:
    known: dict[str, datetime] = {}
    for key, found in seen.items():
        moment = from_text(found.listed or found.rejected or found.purged)
        if moment is not None:
            known[key] = moment if moment <= started else started - _HEALED
    return known


def _held_closed(conn: sqlite3.Connection, batch: SourceBatch, jobs: tuple[Job, ...]) -> set[str]:
    rows = conn.execute(
        "SELECT j.id FROM json_each(?) w CROSS JOIN jobs j ON j.id = w.value "
        "JOIN detail_fetches d ON d.id = ? || j.id WHERE j.status = ? "
        "AND d.resolved = 0 AND d.failed = 0 AND (d.fetched_at >= ? OR ?)",
        (
            listed(job.id for job in jobs),
            CHECK_PREFIX,
            CLOSED,
            to_text(batch.run_started_at),
            int(batch.source not in CHECKED_SOURCES),
        ),
    )
    return {str(row["id"]) for row in rows}


def _link_duplicates(
    conn: sqlite3.Connection,
    resolve: Resolver | None,
    jobs: Sequence[Job],
    candidates: Sequence[Job] | None = None,
) -> int:
    if resolve is None or not jobs:
        return 0
    links = resolve(jobs, duplicate_candidates(conn, jobs) if candidates is None else candidates)
    pairs = [
        (link.duplicate_id, link.canonical_id)
        for link in links
        if board_of(link.duplicate_id, link.duplicate_id)
        != board_of(link.canonical_id, link.canonical_id)
        or is_workday_opening(link.evidence)
    ]
    duplicates = {duplicate for duplicate, _ in pairs}
    conn.execute(
        f"UPDATE jobs SET duplicate_of = NULL WHERE id IN {EACH} AND duplicate_of IS NOT NULL",
        (listed(job.id for job in jobs if job.id not in duplicates),),
    )
    if not pairs:
        return 0
    conn.executemany(
        "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
        [(canonical, duplicate) for duplicate, canonical in pairs],
    )
    conn.execute(
        f"UPDATE jobs SET duplicate_of = NULL WHERE id IN {EACH}",
        (listed(canonical for _, canonical in pairs),),
    )
    conn.executemany(
        "UPDATE jobs SET duplicate_of = ? WHERE duplicate_of = ? AND id != ?",
        [(canonical, duplicate, canonical) for duplicate, canonical in pairs],
    )
    return len(duplicates)


def _reject(
    conn: sqlite3.Connection,
    batch: SourceBatch,
    entries: tuple[QuarantinedJob, ...],
    known: dict[str, datetime],
    stamp: str,
) -> tuple[tuple[QuarantinedJob, ...], dict[str, str]]:
    if not entries:
        return entries, {}
    entries += followers_of_rejected(conn, batch, entries)
    leaving = {entry.id for entry in entries}
    conn.executemany(
        UPSERT_QUARANTINE,
        [quarantined_params(entry, known.get(entry.id, entry.first_seen)) for entry in entries],
    )
    heirs = heirs_of(conn, leaving, promote_orphaned_duplicates(conn, sorted(leaving)))
    remember_heirs(conn, sorted(heirs.items()), stamp)
    conn.execute(f"DELETE FROM jobs WHERE id IN {EACH}", (listed(leaving),))
    return entries, heirs


def _crawl(conn: sqlite3.Connection, batch: SourceBatch, stamp: str) -> None:
    for crawl in batch.workday_crawls:
        if crawl.reset or crawl.discard:
            conn.execute("DELETE FROM workday_crawls WHERE board = ?", (crawl.board,))
        if crawl.discard:
            continue
        conn.execute(
            UPSERT_CRAWL,
            (
                crawl.board,
                crawl.next_offset,
                crawl.total,
                crawl.facet_parameter,
                json.dumps(crawl.facet_ids),
            ),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO workday_crawl_seen (board, id) VALUES (?, ?)",
            [(crawl.board, job_id) for job_id in crawl.seen_ids],
        )
        if crawl.complete:
            conn.execute(
                "UPDATE jobs SET last_seen = ? WHERE source = 'workday' AND status = ? AND id IN ("
                "SELECT id FROM workday_crawl_seen WHERE board = ?)",
                (stamp, OPEN, crawl.board),
            )
            conn.execute("DELETE FROM workday_crawls WHERE board = ?", (crawl.board,))


def _touch(conn: sqlite3.Connection, batch: SourceBatch, stamp: str) -> int:
    touched = 0
    for board in batch.unchanged_boards:
        touched += conn.execute(
            "UPDATE jobs SET last_seen = ? WHERE source = ? AND id GLOB ? AND status = ?",
            (stamp, batch.source, board_rows(board), OPEN),
        ).rowcount
        conn.execute(
            "UPDATE quarantine SET last_seen = ? WHERE source = ? AND id GLOB ?",
            (stamp, batch.source, board_rows(board)),
        )
    return touched


def _close(conn: sqlite3.Connection, batch: SourceBatch, stamp: str) -> tuple[int, list[str]]:
    if batch.closes_whole_source:
        gone = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM jobs WHERE source = ? AND status = ? AND last_seen <> ?",
                (batch.source, OPEN, stamp),
            )
        ]
        conn.execute(f"UPDATE jobs SET status = ? WHERE id IN {EACH}", (CLOSED, listed(gone)))
        return len(gone), gone
    gone = [
        str(row["id"])
        for board in batch.closable_boards
        for row in conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND id GLOB ? AND status = ? AND last_seen <> ?",
            (batch.source, board_rows(board), OPEN, stamp),
        )
    ]
    conn.execute(f"UPDATE jobs SET status = ? WHERE id IN {EACH}", (CLOSED, listed(gone)))
    return len(gone), gone + _close_list_copies(conn, gone, batch.list_sources, stamp)


def _close_list_copies(
    conn: sqlite3.Connection, gone: Sequence[str], lists: Collection[str], stamp: str
) -> list[str]:
    if not gone or not lists:
        return []
    copies = [
        str(row["id"])
        for row in conn.execute(
            f"SELECT id FROM jobs WHERE duplicate_of IN {EACH} "
            f"AND source IN ({marks(len(lists))}) AND status = ?",
            (listed(gone), *sorted(lists), OPEN),
        )
    ]
    conn.execute(f"UPDATE jobs SET status = ? WHERE id IN {EACH}", (CLOSED, listed(copies)))
    conn.executemany(UPSERT_CHECK, [(CHECK_PREFIX + ident, stamp, 0, 0, ident) for ident in copies])
    return copies


def _record_reads(conn: sqlite3.Connection, batch: SourceBatch, stamp: str) -> None:
    conn.executemany(
        UPSERT_VISIT,
        [
            (
                batch.source,
                visit.board,
                visit.label,
                stamp,
                stamp if visit.succeeded else None,
                0 if visit.succeeded else 1,
                "" if visit.succeeded else visit.error,
            )
            for visit in batch.visits
        ],
    )
    conn.executemany(
        UPSERT_DETAIL,
        [
            (entry.id, batch.source, stamp, int(entry.resolved), 1, int(entry.failed))
            for entry in batch.detail_fetches
        ],
    )
    conn.executemany(
        "DELETE FROM workday_facets WHERE tenant = ? AND site = ?",
        [(facet.tenant, facet.site) for facet in batch.forgotten_facets],
    )
    conn.executemany(
        UPSERT_FACET,
        [
            (
                facet.tenant,
                facet.site,
                facet.parameter,
                " ".join(facet.facet_ids),
                facet.descriptor,
                to_text(facet.resolved_at or batch.run_started_at),
            )
            for facet in batch.workday_facets
            if not facet.pinned
        ],
    )
    conn.executemany(UPSERT_RATE_STATE, [rate_state_params(state) for state in batch.rate_state])
    conn.executemany(
        UPSERT_VALIDATOR,
        [
            (
                validator.url,
                batch.source,
                validator.etag,
                validator.last_modified,
                to_text(validator.fetched_at or batch.run_started_at),
            )
            for validator in batch.validators
        ],
    )


class BatchWriter(Store):
    def relink_duplicates(self, resolve: Resolver, canonical: Callable[[str], str]) -> int:
        with self._transaction() as conn:
            before = dict(
                conn.execute("SELECT id, duplicate_of FROM jobs WHERE status = ?", (OPEN,))
            )
            stored = [
                job_from(row)
                for row in conn.execute("SELECT * FROM jobs WHERE status = ?", (OPEN,))
            ]
            jobs = [
                replace(job, duplicate_of=None, apply_url_canonical=canonical(job.apply_url_raw))
                for job in stored
            ]
            conn.executemany(
                "UPDATE jobs SET apply_url_canonical = ? WHERE id = ?",
                [
                    (job.apply_url_canonical, job.id)
                    for job, old in zip(jobs, stored, strict=True)
                    if job.apply_url_canonical != old.apply_url_canonical
                ],
            )
            _link_duplicates(conn, resolve, jobs, ())
            after = dict(
                conn.execute("SELECT id, duplicate_of FROM jobs WHERE status = ?", (OPEN,))
            )
        return sum(1 for ident, linked in after.items() if before.get(ident) != linked)

    def apply_source_batch(self, batch: SourceBatch) -> SourceBatchResult:
        started = batch.run_started_at
        stamp = to_text(started)
        with self._transaction() as conn:
            seen = _seen(conn, [job.id for job in batch.jobs] + [e.id for e in batch.quarantined])
            cutoff = to_text(started - timedelta(days=OPEN_RETENTION_DAYS))
            known = _first_seen(seen, started)
            jobs = tuple(
                replace(job, first_seen=known[job.id]) if job.id in known else job
                for job in batch.jobs
                if not seen.get(job.id, _UNSEEN).expired(cutoff)
            )
            held = _held_closed(conn, batch, jobs)
            jobs = tuple(
                replace(job, status=JobStatus.CLOSED) if job.id in held else job for job in jobs
            )
            jobs, diverted = copies_of_rejected(conn, batch, jobs)
            conn.executemany(UPSERT_JOB, [job_params(job) for job in jobs])
            quarantined, heirs = _reject(conn, batch, batch.quarantined + diverted, known, stamp)
            conn.execute(
                f"DELETE FROM quarantine WHERE id IN {EACH}",
                (listed(job.id for job in jobs if seen.get(job.id, _UNSEEN).rejected is not None),),
            )
            restore_copies(conn, {job.id for job in jobs})
            _crawl(conn, batch, stamp)
            touched = _touch(conn, batch, stamp)
            closed, shut = _close(conn, batch, stamp)
            promoted = promote_open_copies(conn, shut)
            remember_heirs(conn, sorted(promoted.items()), stamp)
            heirs = {**heirs, **promoted}
            linked = _link_duplicates(conn, batch.resolve_duplicates, jobs)
            _record_reads(conn, batch, stamp)
        updated = sum(1 for job in jobs if seen.get(job.id, _UNSEEN).listed is not None)
        return SourceBatchResult(
            source=batch.source,
            fetched=len(batch.jobs) + len(batch.quarantined),
            added=len(jobs) - updated,
            updated=updated,
            closed=closed,
            touched=touched,
            quarantined=len(quarantined),
            duplicates=linked,
            stored=count(
                self._conn,
                "SELECT COUNT(*) FROM jobs WHERE source = ? AND status = ?",
                (batch.source, OPEN),
            ),
            successors=tuple(sorted(heirs.items())),
        )
