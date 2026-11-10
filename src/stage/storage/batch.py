import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import NamedTuple

from stage.domain import (
    HELD_CLOSE_PREFIX,
    MASS_CLOSE_MIN,
    MASS_CLOSE_SHARE,
    OPEN_RETENTION_DAYS,
    REPOST,
    UNKNOWN_TERM,
    Job,
    JobStatus,
    LocationBucket,
    QuarantinedJob,
    board_of,
)
from stage.domain.liveness import BOARD_SOURCES, CHECK_PREFIX
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
    stored_locations,
)
from stage.storage.sql import (
    EACH,
    Store,
    board_rows,
    count,
    from_text,
    link_of,
    listed,
    marks,
    to_text,
)
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


def _copies_of_retired(conn: sqlite3.Connection, jobs: Iterable[Job], cutoff: str) -> set[str]:
    from stage.normalize import named_posting, names_posting

    retired: set[str] = set()
    for job in jobs:
        board, tail = named_posting(job.apply_url_canonical or job.apply_url_raw)
        if not tail:
            continue
        rows = conn.execute(
            "SELECT id FROM tombstones WHERE id GLOB ? AND first_seen < ? AND id <> ?",
            (f"{board}{tail}", cutoff, job.id),
        )
        if any(names_posting(str(row["id"]), tail) for row in rows):
            retired.add(job.id)
    return retired


_REJECTED_TOUCH = timedelta(days=1)


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
            int(batch.source not in BOARD_SOURCES),
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
        or link.kind == REPOST
    ]
    duplicates = {duplicate for duplicate, _ in pairs}
    conn.execute(
        f"UPDATE jobs SET duplicate_of = NULL WHERE id IN {EACH} AND duplicate_of IS NOT NULL",
        (listed(job.id for job in jobs if job.id not in duplicates),),
    )
    if not pairs:
        _inherit_from_copies(conn, [job.id for job in jobs])
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
    _inherit_from_copies(conn, [*(job.id for job in jobs), *(canonical for _, canonical in pairs)])
    return len(duplicates)


_COPIES = "FROM jobs c WHERE c.duplicate_of = jobs.id AND c.status = ?"
_CURRENT = (
    "NOT stage_term_ended(c.term, coalesce(jobs.source_posted_at, jobs.first_seen)) "
    "AND stage_term_fits(c.term, jobs.title_raw)"
)
_PLACED = f"c.location NOT IN {tuple(stored_locations(LocationBucket.UNKNOWN))}"
_UNPLACED = "TRIM(location_raw) = ''"


def _inherit_from_copies(conn: sqlite3.Connection, ids: Sequence[str]) -> None:
    mains = listed(ids)
    conn.execute(
        f"UPDATE jobs SET term = (SELECT MIN(c.term) {_COPIES} AND c.term <> ? AND {_CURRENT}) "
        f"WHERE id IN {EACH} AND duplicate_of IS NULL AND term = ? "
        f"AND (SELECT COUNT(DISTINCT c.term) {_COPIES} AND c.term <> ? AND {_CURRENT}) = 1",
        (OPEN, UNKNOWN_TERM, mains, UNKNOWN_TERM, OPEN, UNKNOWN_TERM),
    )
    conn.execute(
        "UPDATE jobs SET (location_raw, location, remote_scope) = (SELECT c.location_raw, "
        f"c.location, c.remote_scope {_COPIES} AND {_PLACED} ORDER BY c.id LIMIT 1) "
        f"WHERE id IN {EACH} AND duplicate_of IS NULL AND {_UNPLACED} "
        f"AND (SELECT COUNT(DISTINCT c.location) {_COPIES} AND {_PLACED}) = 1",
        (OPEN, mains, OPEN),
    )


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
            "UPDATE jobs SET last_seen = ? WHERE source = ? AND id GLOB ? AND status = ? "
            "AND last_seen >= coalesce((SELECT fetched_at FROM detail_fetches WHERE id = ?), '')",
            (
                stamp,
                batch.source,
                board_rows(board),
                OPEN,
                f"{HELD_CLOSE_PREFIX}{batch.source}:{board}",
            ),
        ).rowcount
        conn.execute(
            "UPDATE quarantine SET last_seen = ? WHERE source = ? AND id GLOB ? AND last_seen < ?",
            (
                stamp,
                batch.source,
                board_rows(board),
                to_text(batch.run_started_at - _REJECTED_TOUCH),
            ),
        )
    return touched


def _close(
    conn: sqlite3.Connection, batch: SourceBatch, stamp: str
) -> tuple[int, list[str], frozenset[str]]:
    urls = dict(batch.board_urls)
    boards = ("",) if batch.closes_whole_source else batch.closable_boards
    gone: list[str] = []
    held: set[str] = set()
    for board in boards:
        rows = board_rows(board) if board else "*"
        found = _closing(conn, batch.source, board, rows, stamp)
        if found is None:
            held.update(
                url for key, planned in urls.items() if board in ("", key) for url in planned
            )
        gone += found or []
    conn.execute(f"UPDATE jobs SET status = ? WHERE id IN {EACH}", (CLOSED, listed(gone)))
    if batch.closes_whole_source:
        return len(gone), gone, frozenset(held)
    copies = _close_list_copies(conn, gone, batch.list_sources, stamp)
    return len(gone), gone + copies, frozenset(held)


def _closing(
    conn: sqlite3.Connection, source: str, board: str, rows: str, stamp: str
) -> list[str] | None:
    found = conn.execute(
        "SELECT id, last_seen <> ? AS unseen FROM jobs WHERE source = ? AND id GLOB ? "
        "AND status = ?",
        (stamp, source, rows, OPEN),
    ).fetchall()
    unseen = [str(row["id"]) for row in found if row["unseen"]]
    marker = f"{HELD_CLOSE_PREFIX}{source}:{board}"
    if len(unseen) < MASS_CLOSE_MIN or len(unseen) < len(found) * MASS_CLOSE_SHARE:
        conn.execute("DELETE FROM detail_fetches WHERE id = ?", (marker,))
        return unseen
    held = conn.execute("SELECT fetched_at FROM detail_fetches WHERE id = ?", (marker,)).fetchone()
    if held is not None and str(held["fetched_at"]) < stamp:
        conn.execute("DELETE FROM detail_fetches WHERE id = ?", (marker,))
        return unseen
    conn.execute(UPSERT_DETAIL, (marker, source, stamp, 0, 1, 0))
    return None


def _close_list_copies(
    conn: sqlite3.Connection, gone: Sequence[str], lists: Collection[str], stamp: str
) -> list[str]:
    if not gone or not lists:
        return []
    copies = [
        str(row["id"])
        for row in conn.execute(
            "SELECT c.id FROM jobs c JOIN jobs m ON m.id = c.duplicate_of "
            f"WHERE c.duplicate_of IN {EACH} AND c.source IN ({marks(len(lists))}) "
            f"AND c.status = ? AND NOT stage_other_opening({link_of('m')}, {link_of('c')})",
            (listed(gone), *sorted(lists), OPEN),
        )
    ]
    conn.execute(f"UPDATE jobs SET status = ? WHERE id IN {EACH}", (CLOSED, listed(copies)))
    conn.executemany(UPSERT_CHECK, [(CHECK_PREFIX + ident, stamp, 0, 0, ident) for ident in copies])
    return copies


def _record_reads(
    conn: sqlite3.Connection, batch: SourceBatch, stamp: str, held: frozenset[str]
) -> None:
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
    conn.execute(f"DELETE FROM http_cache WHERE url IN {EACH}", (listed(held),))
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
            if validator.url not in held
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
            arriving = [job for job in batch.jobs if job.id not in seen]
            retired = _copies_of_retired(conn, arriving, cutoff)
            jobs = tuple(
                replace(job, first_seen=known[job.id]) if job.id in known else job
                for job in batch.jobs
                if not seen.get(job.id, _UNSEEN).expired(cutoff) and job.id not in retired
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
            closed, shut, held_urls = _close(conn, batch, stamp)
            promoted = promote_open_copies(conn, shut)
            remember_heirs(conn, sorted(promoted.items()), stamp)
            heirs = {**heirs, **promoted}
            linked = _link_duplicates(conn, batch.resolve_duplicates, jobs)
            _record_reads(conn, batch, stamp, held_urls)
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
