import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stage.domain import (
    CLOSED_RETENTION_DAYS,
    OPEN_RETENTION_DAYS,
    UNKNOWN_TERM,
    CoverageClassification,
    CoverageDisposition,
    DegreeRequirement,
    HttpValidator,
    IntegrityFinding,
    IntegrityRepair,
    Job,
    JobFilters,
    JobStatus,
    Language,
    LocationBucket,
    PurgeResult,
    QuarantinedJob,
    QuarantineFilters,
    RateState,
    RejectionReason,
    RemoteScope,
    RoleCategory,
    SourceRunStats,
    SourceSignals,
    SourceVisit,
    SyncOutcome,
    SyncRun,
    VolumePoint,
    WorkdayCrawl,
    WorkdayFacet,
    board_of,
    public_https_url,
    source_rank,
)
from stage.paths import restrict_permissions
from stage.storage.migrations import migrate
from stage.storage.repository import SourceBatch, SourceBatchResult
from stage.storage.search import FTS_COLUMN_WEIGHTS, match_expression, search_terms

_BM25_WEIGHTS = ", ".join(f"{weight:.1f}" for weight in FTS_COLUMN_WEIGHTS)

_COMPOSITION_COLUMNS = frozenset(
    {"source", "location", "role", "term", "language", "status", "degree_requirement"}
)
_LEGACY_LOCATION_BUCKETS = {"other": "international", "remote": "unknown"}
_STORED_LOCATION_VALUES = {
    LocationBucket.INTERNATIONAL: ("international", "other"),
    LocationBucket.UNKNOWN: ("unknown", "remote"),
}


def _location_bucket(value: str) -> LocationBucket:
    return LocationBucket(_LEGACY_LOCATION_BUCKETS.get(value, value))


def _stored_location_values(bucket: LocationBucket) -> tuple[str, ...]:
    return _STORED_LOCATION_VALUES.get(bucket, (bucket.value,))


def fold_company(value: str) -> str:
    from stage.lexicon import fold

    return fold(value)


_JOB_COLUMNS = (
    "id",
    "source",
    "company",
    "company_fold",
    "title_raw",
    "title_normalized",
    "title_canonical",
    "apply_url_raw",
    "apply_url_canonical",
    "description",
    "location_raw",
    "location",
    "remote_scope",
    "language",
    "term",
    "role",
    "work_auth_flag",
    "degree_requirement",
    "compensation",
    "employment_type",
    "source_category",
    "status",
    "first_seen",
    "last_seen",
    "source_posted_at",
    "duplicate_of",
)

_UPDATE_ON_CONFLICT = (
    "source = excluded.source",
    "company = excluded.company",
    "company_fold = excluded.company_fold",
    "title_raw = excluded.title_raw",
    "title_normalized = excluded.title_normalized",
    "title_canonical = excluded.title_canonical",
    "apply_url_raw = excluded.apply_url_raw",
    "apply_url_canonical = excluded.apply_url_canonical",
    "description = excluded.description",
    "location_raw = excluded.location_raw",
    "location = excluded.location",
    "remote_scope = excluded.remote_scope",
    "language = excluded.language",
    "term = excluded.term",
    "role = excluded.role",
    "work_auth_flag = excluded.work_auth_flag",
    "degree_requirement = excluded.degree_requirement",
    "compensation = excluded.compensation",
    "employment_type = excluded.employment_type",
    "source_category = excluded.source_category",
    "status = excluded.status",
    "last_seen = excluded.last_seen",
    "source_posted_at = excluded.source_posted_at",
)

_UPSERT_SQL = (
    f"INSERT INTO jobs ({', '.join(_JOB_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_JOB_COLUMNS))}) "
    f"ON CONFLICT(id) DO UPDATE SET {', '.join(_UPDATE_ON_CONFLICT)}"
)


_QUARANTINE_COLUMNS = (
    "id",
    "source",
    "company",
    "title_raw",
    "apply_url_raw",
    "location_raw",
    "location",
    "remote_scope",
    "reason",
    "matched_phrase",
    "first_seen",
    "last_seen",
)

_QUARANTINE_UPSERT_SQL = (
    f"INSERT INTO quarantine ({', '.join(_QUARANTINE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_QUARANTINE_COLUMNS))}) "
    "ON CONFLICT(id) DO UPDATE SET "
    "source = excluded.source, company = excluded.company, "
    "title_raw = excluded.title_raw, apply_url_raw = excluded.apply_url_raw, "
    "location_raw = excluded.location_raw, location = excluded.location, "
    "remote_scope = excluded.remote_scope, reason = excluded.reason, "
    "matched_phrase = excluded.matched_phrase, last_seen = excluded.last_seen"
)


_RATE_STATE_COLUMNS = (
    "bucket",
    "blocked_until",
    "min_interval_override",
    "consecutive_failures",
    "last_failure_at",
    "reason",
    "rotation_cursor",
    "updated_at",
)

_RATE_STATE_UPSERT_SQL = (
    f"INSERT INTO rate_state ({', '.join(_RATE_STATE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_RATE_STATE_COLUMNS))}) "
    "ON CONFLICT(bucket) DO UPDATE SET "
    "blocked_until = NULLIF(MAX(COALESCE(excluded.blocked_until, ''), "
    "COALESCE(rate_state.blocked_until, '')), ''), "
    "reason = CASE WHEN COALESCE(rate_state.blocked_until, '') > "
    "COALESCE(excluded.blocked_until, '') THEN rate_state.reason ELSE excluded.reason END, "
    "min_interval_override = excluded.min_interval_override, "
    "consecutive_failures = excluded.consecutive_failures, "
    "last_failure_at = excluded.last_failure_at, "
    "rotation_cursor = excluded.rotation_cursor, updated_at = excluded.updated_at"
)


_VISIT_UPSERT_SQL = (
    "INSERT INTO source_visits "
    "(source, board, label, last_attempt_at, last_success_at, consecutive_failures, last_error) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(source, board) DO UPDATE SET "
    "label = excluded.label, "
    "last_attempt_at = excluded.last_attempt_at, "
    "last_success_at = COALESCE(excluded.last_success_at, source_visits.last_success_at), "
    "consecutive_failures = CASE WHEN excluded.consecutive_failures = 0 THEN 0 "
    "ELSE source_visits.consecutive_failures + 1 END, "
    "last_error = excluded.last_error"
)


MAX_DETAIL_ATTEMPTS = 3
_ID_CHUNK = 500

_QUEUE_ELIGIBLE = "(d.id IS NULL OR (d.failed = 1 AND d.attempts < ?))"


def _board_glob(board: str) -> str:
    return f"{board}:*"


def _limit_clause(limit: int | None) -> tuple[str, tuple[int, ...]]:
    return ("", ()) if limit is None else (" LIMIT ?", (limit,))


def _to_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not stored; supply an aware datetime")
    return value.astimezone(UTC).isoformat()


def _from_text(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _require_datetime(value: str | None, column: str) -> datetime:
    parsed = _from_text(value)
    if parsed is None:
        raise ValueError(f"column {column!r} is unexpectedly null")
    return parsed


def _classification_from_row(row: sqlite3.Row) -> CoverageClassification:
    return CoverageClassification(
        company=str(row["company"]),
        disposition=CoverageDisposition(str(row["disposition"])),
        note=str(row["note"]),
        checked_on=_require_datetime(row["checked_on"], "checked_on"),
        url=str(row["url"]) if row["url"] is not None else None,
    )


def _classification_params(
    entry: CoverageClassification,
) -> tuple[str, str, str, str, str, str | None]:
    company = entry.company.strip()
    company_fold = fold_company(company)
    if not company_fold:
        raise ValueError("classification company must contain letters or numbers")
    note = entry.note.strip()
    if not note:
        raise ValueError("classification note must be a non-empty string")
    if not isinstance(entry.disposition, CoverageDisposition):
        raise ValueError("classification disposition is invalid")
    url = entry.url
    if url is not None:
        url = public_https_url(url)
        if url is None:
            raise ValueError(
                "classification url must be a public https address without credentials"
            )
    return company, company_fold, entry.disposition.value, note, _to_text(entry.checked_on), url


def _row_to_job(row: sqlite3.Row) -> Job:
    remote_scope = row["remote_scope"]
    return Job(
        id=row["id"],
        source=row["source"],
        company=row["company"],
        title_raw=row["title_raw"],
        title_normalized=row["title_normalized"],
        title_canonical=row["title_canonical"],
        apply_url_raw=row["apply_url_raw"],
        apply_url_canonical=row["apply_url_canonical"],
        description=row["description"],
        location_raw=row["location_raw"],
        location=_location_bucket(row["location"]),
        remote_scope=RemoteScope(remote_scope) if remote_scope else None,
        language=Language(row["language"]),
        term=row["term"],
        role=RoleCategory(row["role"]),
        work_auth_flag=bool(row["work_auth_flag"]),
        degree_requirement=DegreeRequirement(row["degree_requirement"]),
        compensation=row["compensation"],
        signals=SourceSignals(
            employment_type=row["employment_type"],
            category=row["source_category"],
        ),
        status=JobStatus(row["status"]),
        first_seen=_require_datetime(row["first_seen"], "first_seen"),
        last_seen=_require_datetime(row["last_seen"], "last_seen"),
        source_posted_at=_from_text(row["source_posted_at"]),
        duplicate_of=row["duplicate_of"],
    )


def _job_to_params(job: Job) -> tuple[Any, ...]:
    return (
        job.id,
        job.source,
        job.company,
        fold_company(job.company),
        job.title_raw,
        job.title_normalized,
        job.title_canonical,
        job.apply_url_raw,
        job.apply_url_canonical,
        job.description,
        job.location_raw,
        job.location.value,
        job.remote_scope.value if job.remote_scope else None,
        job.language.value,
        job.term,
        job.role.value,
        int(job.work_auth_flag),
        job.degree_requirement.value,
        job.compensation,
        job.signals.employment_type,
        job.signals.category,
        job.status.value,
        _to_text(job.first_seen),
        _to_text(job.last_seen),
        _to_text(job.source_posted_at) if job.source_posted_at else None,
        job.duplicate_of,
    )


def _row_to_quarantined(row: sqlite3.Row) -> QuarantinedJob:
    remote_scope = row["remote_scope"]
    return QuarantinedJob(
        id=row["id"],
        source=row["source"],
        company=row["company"],
        title_raw=row["title_raw"],
        reason=RejectionReason(row["reason"]),
        first_seen=_require_datetime(row["first_seen"], "first_seen"),
        last_seen=_require_datetime(row["last_seen"], "last_seen"),
        apply_url_raw=row["apply_url_raw"],
        location_raw=row["location_raw"],
        location=_location_bucket(row["location"]),
        remote_scope=RemoteScope(remote_scope) if remote_scope else None,
        matched_phrase=row["matched_phrase"],
    )


def _row_to_rate_state(row: sqlite3.Row) -> RateState:
    return RateState(
        bucket=str(row["bucket"]),
        updated_at=_require_datetime(row["updated_at"], "updated_at"),
        blocked_until=_from_text(row["blocked_until"]),
        min_interval_override=(
            float(row["min_interval_override"])
            if row["min_interval_override"] is not None
            else None
        ),
        consecutive_failures=int(row["consecutive_failures"]),
        last_failure_at=_from_text(row["last_failure_at"]),
        reason=str(row["reason"]),
        rotation_cursor=str(row["rotation_cursor"]),
    )


def _rate_state_to_params(state: RateState) -> tuple[Any, ...]:
    return (
        state.bucket,
        _to_text(state.blocked_until) if state.blocked_until else None,
        state.min_interval_override,
        state.consecutive_failures,
        _to_text(state.last_failure_at) if state.last_failure_at else None,
        state.reason,
        state.rotation_cursor,
        _to_text(state.updated_at),
    )


def _quarantined_to_params(entry: QuarantinedJob) -> tuple[Any, ...]:
    return (
        entry.id,
        entry.source,
        entry.company,
        entry.title_raw,
        entry.apply_url_raw,
        entry.location_raw,
        entry.location.value,
        entry.remote_scope.value if entry.remote_scope else None,
        entry.reason.value,
        entry.matched_phrase,
        _to_text(entry.first_seen),
        _to_text(entry.last_seen),
    )


MAX_CHAIN_REPAIR_PASSES = 8


class SqliteRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        connection.create_function("stage_board_of", 2, board_of, deterministic=True)

    @classmethod
    def connect(cls, db_path: Path) -> "SqliteRepository":
        is_new = not db_path.exists()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            migrate(conn, db_path)
            if is_new:
                restrict_permissions(db_path)
        except Exception:
            conn.close()
            raise
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

    def _existing_ids(self, job_ids: Sequence[str]) -> set[str]:
        existing: set[str] = set()
        chunk_size = 400
        for start in range(0, len(job_ids), chunk_size):
            chunk = job_ids[start : start + chunk_size]
            placeholders = ", ".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT id FROM jobs WHERE id IN ({placeholders})", tuple(chunk)
            ).fetchall()
            existing.update(str(row["id"]) for row in rows)
        return existing

    def _chunked(self, values: Sequence[str]) -> Iterator[Sequence[str]]:
        chunk_size = 400
        for start in range(0, len(values), chunk_size):
            yield values[start : start + chunk_size]

    def _preserved_first_seen(self, job_ids: Sequence[str]) -> dict[str, datetime]:
        found: dict[str, datetime] = {}
        for table in ("jobs", "quarantine", "tombstones"):
            for chunk in self._chunked(job_ids):
                placeholders = ", ".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT id, first_seen FROM {table} WHERE id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    found.setdefault(
                        str(row["id"]), _require_datetime(row["first_seen"], "first_seen")
                    )
        return found

    def _duplicate_candidates(self, jobs: Sequence[Job]) -> list[Job]:
        companies = sorted({folded for job in jobs if (folded := fold_company(job.company))})
        urls = sorted({job.apply_url_canonical for job in jobs if job.apply_url_canonical})
        found: dict[str, Job] = {}
        for column, values in (("company_fold", companies), ("apply_url_canonical", urls)):
            for start in range(0, len(values), 400):
                chunk = values[start : start + 400]
                if not chunk:
                    continue
                placeholders = ", ".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT * FROM jobs WHERE {column} IN ({placeholders})", tuple(chunk)
                ).fetchall()
                for row in rows:
                    found[str(row["id"])] = _row_to_job(row)
        return list(found.values())

    def _apply_duplicate_links(self, batch: SourceBatch, jobs: Sequence[Job]) -> int:
        if batch.resolve_duplicates is None or not jobs:
            return 0
        candidates = self._duplicate_candidates(jobs)
        links = batch.resolve_duplicates(jobs, candidates)
        if not links:
            self._conn.execute(
                "UPDATE jobs SET duplicate_of = NULL WHERE id IN "
                f"({', '.join('?' * len(jobs))}) AND duplicate_of IS NOT NULL",
                tuple(job.id for job in jobs),
            )
            return 0
        pairs = [
            (duplicate, canonical)
            for duplicate, canonical in (
                (str(link.duplicate_id), str(link.canonical_id))  # type: ignore[attr-defined]
                for link in links
            )
            if board_of(duplicate, duplicate) != board_of(canonical, canonical)
        ]
        if not pairs:
            return 0
        duplicates = {duplicate for duplicate, _ in pairs}
        self._clear_stale_links(jobs, duplicates)
        canonicals = {canonical for _, canonical in pairs}
        self._conn.executemany(
            "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
            [(canonical, duplicate) for duplicate, canonical in pairs],
        )
        placeholders = ", ".join("?" * len(canonicals))
        self._conn.execute(
            f"UPDATE jobs SET duplicate_of = NULL WHERE id IN ({placeholders})",
            tuple(sorted(canonicals)),
        )
        for duplicate, canonical in pairs:
            self._conn.execute(
                "UPDATE jobs SET duplicate_of = ? WHERE duplicate_of = ? AND id != ?",
                (canonical, duplicate, canonical),
            )
        return len(duplicates)

    def _clear_stale_links(self, jobs: Sequence[Job], linked: set[str]) -> None:
        stale = [job.id for job in jobs if job.id not in linked]
        for chunk in self._chunked(stale):
            placeholders = ", ".join("?" * len(chunk))
            self._conn.execute(
                f"UPDATE jobs SET duplicate_of = NULL WHERE id IN ({placeholders}) "
                "AND duplicate_of IS NOT NULL",
                tuple(chunk),
            )

    def _reseat_inverted_links(self) -> int:
        rows = self._conn.execute(
            "SELECT d.id AS duplicate, d.source AS duplicate_source, c.id AS canonical, "
            "c.source AS canonical_source FROM jobs d JOIN jobs c ON d.duplicate_of = c.id"
        ).fetchall()
        swapped = 0
        for row in rows:
            duplicate, canonical = str(row["duplicate"]), str(row["canonical"])
            if source_rank(str(row["duplicate_source"]), duplicate) >= source_rank(
                str(row["canonical_source"]), canonical
            ):
                continue
            self._conn.execute(
                "UPDATE jobs SET duplicate_of = ? WHERE duplicate_of = ? AND id != ?",
                (duplicate, canonical, duplicate),
            )
            self._conn.execute("UPDATE jobs SET duplicate_of = NULL WHERE id = ?", (duplicate,))
            self._conn.execute(
                "UPDATE jobs SET duplicate_of = ? WHERE id = ?", (duplicate, canonical)
            )
            swapped += 1
        return swapped

    def _promote_orphaned_duplicates(self, purged: Sequence[str]) -> int:
        doomed = set(purged)
        clusters: dict[str, list[tuple[int, str]]] = {}
        for chunk in self._chunked(purged):
            placeholders = ", ".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT id, source, duplicate_of FROM jobs WHERE duplicate_of IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                if str(row["id"]) in doomed:
                    continue
                clusters.setdefault(str(row["duplicate_of"]), []).append(
                    source_rank(str(row["source"]), str(row["id"]))
                )
        for row in self._conn.execute(
            "SELECT a.id, a.source, a.duplicate_of FROM jobs a "
            "LEFT JOIN jobs b ON a.duplicate_of = b.id "
            "WHERE a.duplicate_of IS NOT NULL AND b.id IS NULL"
        ).fetchall():
            if str(row["id"]) in doomed:
                continue
            clusters.setdefault(str(row["duplicate_of"]), []).append(
                source_rank(str(row["source"]), str(row["id"]))
            )

        promoted = 0
        for members in clusters.values():
            unique = sorted(set(members))
            if not unique:
                continue
            winner = unique[0][1]
            self._conn.execute("UPDATE jobs SET duplicate_of = NULL WHERE id = ?", (winner,))
            for _, member in unique[1:]:
                self._conn.execute(
                    "UPDATE jobs SET duplicate_of = ? WHERE id = ?", (winner, member)
                )
            promoted += 1
        return promoted

    def purge(
        self,
        now: datetime,
        *,
        open_days: int = OPEN_RETENTION_DAYS,
        closed_days: int = CLOSED_RETENTION_DAYS,
    ) -> PurgeResult:
        open_cutoff = _to_text(now - timedelta(days=open_days))
        closed_cutoff = _to_text(now - timedelta(days=closed_days))
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, source, first_seen FROM jobs WHERE "
                "(status = ? AND first_seen < ?) OR (status = ? AND first_seen < ?)",
                (JobStatus.OPEN.value, open_cutoff, JobStatus.CLOSED.value, closed_cutoff),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            reseated = self._reseat_inverted_links()
            promoted = self._promote_orphaned_duplicates(ids) + reseated
            if not ids:
                return PurgeResult(promoted=promoted)
            conn.executemany(
                "INSERT INTO tombstones (id, source, first_seen, purged_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "purged_at = excluded.purged_at",
                [
                    (
                        str(row["id"]),
                        str(row["source"]),
                        str(row["first_seen"]),
                        _to_text(now),
                    )
                    for row in rows
                ],
            )
            for chunk in self._chunked(ids):
                placeholders = ", ".join("?" * len(chunk))
                conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(chunk))
        return PurgeResult(purged=len(ids), tombstoned=len(ids), promoted=promoted)

    def preview_purge(
        self,
        now: datetime,
        *,
        open_days: int = OPEN_RETENTION_DAYS,
        closed_days: int = CLOSED_RETENTION_DAYS,
    ) -> PurgeResult:
        open_cutoff = _to_text(now - timedelta(days=open_days))
        closed_cutoff = _to_text(now - timedelta(days=closed_days))
        row = self._conn.execute(
            "SELECT COUNT(*) AS total FROM jobs WHERE "
            "(status = ? AND first_seen < ?) OR (status = ? AND first_seen < ?)",
            (JobStatus.OPEN.value, open_cutoff, JobStatus.CLOSED.value, closed_cutoff),
        ).fetchone()
        count = int(row["total"])
        return PurgeResult(purged=count, tombstoned=count)

    def tombstone_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS total FROM tombstones").fetchone()
        return int(row["total"])

    def count_duplicates(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total FROM jobs WHERE duplicate_of IS NOT NULL"
        ).fetchone()
        return int(row["total"])

    def apply_source_batch(self, batch: SourceBatch) -> SourceBatchResult:
        stamp = _to_text(batch.run_started_at)
        quarantined_ids = [entry.id for entry in batch.quarantined]

        with self._transaction() as conn:
            known = self._preserved_first_seen([job.id for job in batch.jobs] + quarantined_ids)
            jobs = tuple(
                replace(job, first_seen=known[job.id]) if job.id in known else job
                for job in batch.jobs
            )
            quarantined = tuple(
                replace(entry, first_seen=known[entry.id]) if entry.id in known else entry
                for entry in batch.quarantined
            )
            existing = self._existing_ids([job.id for job in jobs])
            conn.executemany(_UPSERT_SQL, [_job_to_params(job) for job in jobs])

            if quarantined:
                conn.executemany(
                    _QUARANTINE_UPSERT_SQL,
                    [_quarantined_to_params(entry) for entry in quarantined],
                )
                self._promote_orphaned_duplicates(quarantined_ids)
                for chunk in self._chunked(quarantined_ids):
                    placeholders = ", ".join("?" * len(chunk))
                    conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(chunk))
            released = {job.id for job in jobs} & set(known)
            if released:
                for chunk in self._chunked(sorted(released)):
                    placeholders = ", ".join("?" * len(chunk))
                    conn.execute(
                        f"DELETE FROM quarantine WHERE id IN ({placeholders})", tuple(chunk)
                    )

            for crawl in batch.workday_crawls:
                if crawl.reset or crawl.discard:
                    conn.execute("DELETE FROM workday_crawls WHERE board = ?", (crawl.board,))
                if crawl.discard:
                    continue
                conn.execute(
                    "INSERT INTO workday_crawls "
                    "(board, next_offset, total, facet_parameter, facet_ids) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(board) DO UPDATE SET "
                    "next_offset = excluded.next_offset, total = excluded.total, "
                    "facet_parameter = excluded.facet_parameter, facet_ids = excluded.facet_ids",
                    (
                        crawl.board,
                        crawl.next_offset,
                        crawl.total,
                        crawl.facet_parameter,
                        json.dumps(crawl.facet_ids),
                    ),
                )
                if crawl.seen_ids:
                    conn.executemany(
                        "INSERT OR IGNORE INTO workday_crawl_seen (board, id) VALUES (?, ?)",
                        [(crawl.board, job_id) for job_id in crawl.seen_ids],
                    )
                if crawl.complete:
                    conn.execute(
                        "UPDATE jobs SET last_seen = ? WHERE source = 'workday' "
                        "AND status = ? AND id IN ("
                        "SELECT id FROM workday_crawl_seen WHERE board = ?)",
                        (stamp, JobStatus.OPEN.value, crawl.board),
                    )
                    conn.execute("DELETE FROM workday_crawls WHERE board = ?", (crawl.board,))

            touched = 0
            for board in batch.unchanged_boards:
                cursor = conn.execute(
                    "UPDATE jobs SET last_seen = ? WHERE source = ? AND id GLOB ? AND status = ?",
                    (stamp, batch.source, _board_glob(board), JobStatus.OPEN.value),
                )
                touched += cursor.rowcount

            closed = 0
            if batch.closes_whole_source:
                cursor = conn.execute(
                    "UPDATE jobs SET status = ? WHERE source = ? AND status = ? AND last_seen < ?",
                    (JobStatus.CLOSED.value, batch.source, JobStatus.OPEN.value, stamp),
                )
                closed = cursor.rowcount
            else:
                for board in batch.closable_boards:
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ? WHERE source = ? AND id GLOB ? "
                        "AND status = ? AND last_seen < ?",
                        (
                            JobStatus.CLOSED.value,
                            batch.source,
                            _board_glob(board),
                            JobStatus.OPEN.value,
                            stamp,
                        ),
                    )
                    closed += cursor.rowcount

            linked = self._apply_duplicate_links(batch, jobs)

            if batch.visits:
                conn.executemany(
                    _VISIT_UPSERT_SQL,
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

            if batch.detail_fetches:
                conn.executemany(
                    "INSERT INTO detail_fetches (id, source, fetched_at, resolved, "
                    "attempts, failed) VALUES (?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "fetched_at = excluded.fetched_at, resolved = excluded.resolved, "
                    "attempts = detail_fetches.attempts + 1, failed = excluded.failed",
                    [
                        (entry.id, batch.source, stamp, int(entry.resolved), int(entry.failed))
                        for entry in batch.detail_fetches
                    ],
                )

            if batch.forgotten_facets:
                conn.executemany(
                    "DELETE FROM workday_facets WHERE tenant = ? AND site = ?",
                    [(facet.tenant, facet.site) for facet in batch.forgotten_facets],
                )

            if batch.workday_facets:
                conn.executemany(
                    "INSERT INTO workday_facets "
                    "(tenant, site, parameter, facet_id, descriptor, resolved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tenant, site) DO UPDATE SET "
                    "parameter = excluded.parameter, facet_id = excluded.facet_id, "
                    "descriptor = excluded.descriptor, resolved_at = excluded.resolved_at",
                    [
                        (
                            facet.tenant,
                            facet.site,
                            facet.parameter,
                            " ".join(facet.facet_ids),
                            facet.descriptor,
                            _to_text(facet.resolved_at or batch.run_started_at),
                        )
                        for facet in batch.workday_facets
                        if not facet.pinned
                    ],
                )

            if batch.rate_state:
                conn.executemany(
                    _RATE_STATE_UPSERT_SQL,
                    [_rate_state_to_params(state) for state in batch.rate_state],
                )

            if batch.validators:
                conn.executemany(
                    "INSERT INTO http_cache (url, source, etag, last_modified, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                    "source = excluded.source, etag = excluded.etag, "
                    "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at",
                    [
                        (
                            validator.url,
                            batch.source,
                            validator.etag,
                            validator.last_modified,
                            _to_text(validator.fetched_at or batch.run_started_at),
                        )
                        for validator in batch.validators
                    ],
                )

        updated = sum(1 for job in jobs if job.id in existing)
        stored = conn.execute(
            "SELECT COUNT(*) AS total FROM jobs WHERE source = ? AND status = ?",
            (batch.source, JobStatus.OPEN.value),
        ).fetchone()
        return SourceBatchResult(
            source=batch.source,
            fetched=len(jobs) + len(batch.quarantined),
            added=len(jobs) - updated,
            updated=updated,
            closed=closed,
            touched=touched,
            quarantined=len(batch.quarantined),
            duplicates=linked,
            stored=int(stored["total"]),
        )

    def load_validators(self, source: str) -> Mapping[str, HttpValidator]:
        rows = self._conn.execute(
            "SELECT url, etag, last_modified, fetched_at FROM http_cache WHERE source = ?",
            (source,),
        ).fetchall()
        return {
            str(row["url"]): HttpValidator(
                url=str(row["url"]),
                etag=row["etag"],
                last_modified=row["last_modified"],
                fetched_at=_from_text(row["fetched_at"]),
            )
            for row in rows
        }

    def clear_validators(self, source: str | None = None) -> int:
        with self._transaction() as conn:
            if source is None:
                cursor = conn.execute("DELETE FROM http_cache")
            else:
                cursor = conn.execute("DELETE FROM http_cache WHERE source = ?", (source,))
        return cursor.rowcount if cursor.rowcount > 0 else 0

    def cached_url_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS total FROM http_cache").fetchone()
        return int(row["total"])

    def clear_rate_state(self, bucket: str | None = None) -> int:
        assignments = (
            "blocked_until = NULL, min_interval_override = NULL, "
            "consecutive_failures = 0, last_failure_at = NULL, reason = ''"
        )
        with self._transaction() as conn:
            if bucket is None:
                cursor = conn.execute(f"UPDATE rate_state SET {assignments}")
            else:
                cursor = conn.execute(
                    f"UPDATE rate_state SET {assignments} WHERE bucket = ?", (bucket,)
                )
            return int(cursor.rowcount)

    def load_rate_state(self) -> Mapping[str, RateState]:
        rows = self._conn.execute(
            "SELECT bucket, blocked_until, min_interval_override, consecutive_failures, "
            "last_failure_at, reason, rotation_cursor, updated_at FROM rate_state"
        ).fetchall()
        return {str(row["bucket"]): _row_to_rate_state(row) for row in rows}

    def stale_members(self, source: str, before: datetime) -> list[SourceVisit]:
        rows = self._conn.execute(
            "SELECT source, board, label, last_attempt_at, last_success_at, "
            "consecutive_failures, last_error FROM source_visits WHERE source = ? "
            "AND (last_success_at IS NULL OR last_success_at < ?) "
            "ORDER BY last_success_at IS NOT NULL, last_success_at, board",
            (source, _to_text(before)),
        ).fetchall()
        return [
            SourceVisit(
                source=str(row["source"]),
                board=str(row["board"]),
                label=str(row["label"]),
                last_attempt_at=_require_datetime(row["last_attempt_at"], "last_attempt_at"),
                last_success_at=_from_text(row["last_success_at"]),
                consecutive_failures=int(row["consecutive_failures"]),
                last_error=str(row["last_error"]),
            )
            for row in rows
        ]

    def detail_queue(self, source: str, limit: int) -> list[str]:
        rows = self._conn.execute(
            f"SELECT j.id FROM jobs j LEFT JOIN detail_fetches d ON d.id = j.id "
            f"WHERE j.source = ? AND j.description = '' AND {_QUEUE_ELIGIBLE} "
            f"AND (j.term = ? OR j.role = ?) "
            f"ORDER BY j.first_seen DESC LIMIT ?",
            (source, MAX_DETAIL_ATTEMPTS, UNKNOWN_TERM, RoleCategory.UNKNOWN.value, limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def detail_queue_size(self, source: str) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM jobs j LEFT JOIN detail_fetches d ON d.id = j.id "
            f"WHERE j.source = ? AND j.description = '' AND {_QUEUE_ELIGIBLE} "
            f"AND (j.term = ? OR j.role = ?)",
            (source, MAX_DETAIL_ATTEMPTS, UNKNOWN_TERM, RoleCategory.UNKNOWN.value),
        ).fetchone()
        return int(row["total"])

    def load_workday_facets(self) -> Mapping[tuple[str, str], WorkdayFacet]:
        rows = self._conn.execute(
            "SELECT tenant, site, parameter, facet_id, descriptor, resolved_at FROM workday_facets"
        ).fetchall()
        return {
            (str(row["tenant"]), str(row["site"])): WorkdayFacet(
                tenant=str(row["tenant"]),
                site=str(row["site"]),
                parameter=str(row["parameter"]),
                facet_ids=tuple(str(row["facet_id"]).split()),
                descriptor=str(row["descriptor"]),
                resolved_at=_from_text(row["resolved_at"]),
            )
            for row in rows
        }

    def load_workday_crawls(self) -> Mapping[str, WorkdayCrawl]:
        rows = self._conn.execute(
            "SELECT board, next_offset, total, facet_parameter, facet_ids FROM workday_crawls"
        ).fetchall()
        return {
            str(row["board"]): WorkdayCrawl(
                board=str(row["board"]),
                next_offset=int(row["next_offset"]),
                total=int(row["total"]) if row["total"] is not None else None,
                facet_parameter=str(row["facet_parameter"]),
                facet_ids=tuple(json.loads(str(row["facet_ids"]))),
            )
            for row in rows
        }

    def _quarantine_where(self, filters: QuarantineFilters) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.reason is not None:
            clauses.append("reason = ?")
            params.append(filters.reason.value)
        if filters.source is not None:
            clauses.append("source = ?")
            params.append(filters.source)
        if filters.company is not None:
            clauses.append("company = ?")
            params.append(filters.company)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_quarantined(self, filters: QuarantineFilters) -> list[QuarantinedJob]:
        where, params = self._quarantine_where(filters)
        limit, limit_params = _limit_clause(filters.limit)
        rows = self._conn.execute(
            f"SELECT * FROM quarantine{where} ORDER BY company COLLATE NOCASE, "
            f"first_seen DESC, title_raw COLLATE NOCASE{limit}",
            (*params, *limit_params),
        ).fetchall()
        return [_row_to_quarantined(row) for row in rows]

    def count_quarantined(self, filters: QuarantineFilters) -> int:
        where, params = self._quarantine_where(filters)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM quarantine{where}", tuple(params)
        ).fetchone()
        return int(row["total"])

    def relabel_quarantine(self, entries: Sequence[QuarantinedJob]) -> int:
        if not entries:
            return 0
        relabelled = 0
        with self._transaction() as conn:
            for entry in entries:
                cursor = conn.execute(
                    "UPDATE quarantine SET reason = ?, matched_phrase = ? "
                    "WHERE id = ? AND (reason <> ? OR matched_phrase <> ?)",
                    (
                        entry.reason.value,
                        entry.matched_phrase,
                        entry.id,
                        entry.reason.value,
                        entry.matched_phrase,
                    ),
                )
                relabelled += int(cursor.rowcount or 0)
        return relabelled

    def refresh_quarantine_locations(self, resolve: Callable[[str], tuple[str, str | None]]) -> int:
        rows = self._conn.execute(
            "SELECT id, location_raw, location, remote_scope FROM quarantine "
            "WHERE location_raw IS NOT NULL AND location_raw <> ''"
        ).fetchall()
        updates = []
        for row in rows:
            bucket, scope = resolve(str(row["location_raw"]))
            if bucket == str(row["location"]) and scope == row["remote_scope"]:
                continue
            updates.append((bucket, scope, str(row["id"])))
        if not updates:
            return 0
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE quarantine SET location = ?, remote_scope = ? WHERE id = ?", updates
            )
        return len(updates)

    def quarantine_reason_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT reason, COUNT(*) AS total FROM quarantine GROUP BY reason ORDER BY total DESC"
        ).fetchall()
        return {str(row["reason"]): int(row["total"]) for row in rows}

    def _where(self, filters: JobFilters) -> tuple[str, list[Any]]:
        clauses, params = self._clauses(filters)
        return f" WHERE {' AND '.join(clauses)}", params

    def _clauses(self, filters: JobFilters) -> tuple[list[str], list[Any]]:
        clauses: list[str] = ["jobs.duplicate_of IS NULL"]
        params: list[Any] = []
        if filters.status is not None:
            clauses.append("jobs.status = ?")
            params.append(filters.status.value)
        if filters.location is not None:
            locations = _stored_location_values(filters.location)
            clauses.append(f"jobs.location IN ({', '.join('?' * len(locations))})")
            params.extend(locations)
        if filters.term is not None:
            clauses.append("jobs.term = ?")
            params.append(filters.term)
        if filters.degree is not None:
            clauses.append("jobs.degree_requirement = ?")
            params.append(filters.degree.value)
        if filters.role is not None:
            clauses.append("jobs.role = ?")
            params.append(filters.role.value)
        if filters.language is not None:
            if filters.language in (Language.EN, Language.FR):
                clauses.append("jobs.language IN (?, ?)")
                params.extend((filters.language.value, Language.BILINGUAL.value))
            else:
                clauses.append("jobs.language = ?")
                params.append(filters.language.value)
        if filters.source is not None:
            clauses.append("jobs.source = ?")
            params.append(filters.source)
        if filters.company is not None:
            clauses.append("jobs.company = ?")
            params.append(filters.company)
        if filters.first_seen_after is not None:
            clauses.append("jobs.first_seen >= ?")
            params.append(_to_text(filters.first_seen_after))
        return clauses, params

    def list_jobs(self, filters: JobFilters) -> list[Job]:
        where, params = self._where(filters)
        limit, limit_params = _limit_clause(filters.limit)
        rows = self._conn.execute(
            f"SELECT * FROM jobs{where} ORDER BY first_seen DESC, id ASC{limit}",
            (*params, *limit_params),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_jobs(self, filters: JobFilters) -> int:
        where, params = self._where(filters)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM jobs{where}", tuple(params)
        ).fetchone()
        return int(row["total"])

    def company_names(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT company FROM jobs ORDER BY company COLLATE NOCASE"
        ).fetchall()
        return [str(row["company"]) for row in rows]

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def duplicates_of(self, job_id: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE duplicate_of = ? ORDER BY source, id",
            (job_id,),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def _match(self, query: str, filters: JobFilters) -> tuple[str, str, list[Any]]:
        clauses, params = self._clauses(filters)
        clauses.insert(0, "jobs_fts MATCH ?")
        return (
            match_expression(search_terms(query)),
            f"FROM jobs JOIN jobs_fts ON jobs_fts.rowid = jobs.rowid WHERE {' AND '.join(clauses)}",
            params,
        )

    def search_jobs(self, query: str, filters: JobFilters) -> list[Job]:
        expression, source, params = self._match(query, filters)
        if not expression:
            return []
        limit, limit_params = _limit_clause(filters.limit)
        rows = self._conn.execute(
            f"SELECT jobs.* {source} "
            f"ORDER BY bm25(jobs_fts, {_BM25_WEIGHTS}), jobs.first_seen DESC, jobs.id ASC"
            f"{limit}",
            (expression, *params, *limit_params),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_search(self, query: str, filters: JobFilters) -> int:
        expression, source, params = self._match(query, filters)
        if not expression:
            return 0
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total {source}", (expression, *params)
        ).fetchone()
        return int(row["total"])

    def record_sync_run(self, run: SyncRun) -> None:
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_runs (started_at, finished_at, outcome) VALUES (?, ?, ?)",
                (_to_text(run.started_at), _to_text(run.finished_at), run.outcome.value),
            )
            run_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO sync_run_sources "
                "(run_id, source, fetched, added, updated, closed, quarantined, errors, "
                "requests, not_modified, retries, tightenings, latency_p50_ms, "
                "latency_p95_ms, elapsed_ms, deferred, blocked, stored) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        stats.source,
                        stats.fetched,
                        stats.added,
                        stats.updated,
                        stats.closed,
                        stats.quarantined,
                        stats.errors,
                        stats.requests,
                        stats.not_modified,
                        stats.retries,
                        stats.tightenings,
                        stats.latency_p50_ms,
                        stats.latency_p95_ms,
                        stats.elapsed_ms,
                        stats.deferred,
                        int(stats.blocked),
                        stats.stored,
                    )
                    for stats in run.sources
                ],
            )

    def last_sync_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT finished_at FROM sync_runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return _from_text(row["finished_at"]) if row else None

    def closed_among(self, job_ids: Sequence[str]) -> int:
        total = 0
        for start in range(0, len(job_ids), _ID_CHUNK):
            chunk = job_ids[start : start + _ID_CHUNK]
            marks = ", ".join("?" * len(chunk))
            row = self._conn.execute(
                f"SELECT COUNT(*) AS total FROM jobs WHERE status = ? AND id IN ({marks})",
                (JobStatus.CLOSED.value, *chunk),
            ).fetchone()
            total += int(row["total"])
        return total

    def previous_sync_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT finished_at FROM sync_runs ORDER BY finished_at DESC LIMIT 1 OFFSET 1"
        ).fetchone()
        return _from_text(row["finished_at"]) if row else None

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"]) if row and row["version"] is not None else 0

    def stored_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) AS total FROM jobs WHERE status = ? GROUP BY source",
            (JobStatus.OPEN.value,),
        ).fetchall()
        return {str(row["source"]): int(row["total"]) for row in rows}

    def board_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT id, source FROM jobs WHERE status = ?",
            (JobStatus.OPEN.value,),
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            key = board_of(str(row["id"]), str(row["source"]))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def company_counts(self) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            "SELECT company, source, COUNT(*) AS total FROM jobs "
            "WHERE duplicate_of IS NULL AND status = ? "
            "GROUP BY company, source",
            (JobStatus.OPEN.value,),
        ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(str(row["company"]), {})[str(row["source"])] = int(row["total"])
        return counts

    def quarantine_company_counts(self) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            "SELECT company, source, COUNT(*) AS total FROM quarantine GROUP BY company, source"
        ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(str(row["company"]), {})[str(row["source"])] = int(row["total"])
        return counts

    def quarantine_company_reasons(self) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            "SELECT company, reason, COUNT(*) AS total FROM quarantine GROUP BY company, reason"
        ).fetchall()
        reasons: dict[str, dict[str, int]] = {}
        for row in rows:
            reasons.setdefault(str(row["company"]), {})[str(row["reason"])] = int(row["total"])
        return reasons

    def company_apply_urls(self, companies: Sequence[str]) -> dict[str, tuple[str, ...]]:
        wanted = {fold_company(company): company for company in companies}
        if not wanted:
            return {}
        collected: dict[str, list[str]] = {}
        folds = sorted(wanted)
        for start in range(0, len(folds), _ID_CHUNK):
            chunk = folds[start : start + _ID_CHUNK]
            marks = ", ".join("?" * len(chunk))
            kept = self._conn.execute(
                "SELECT company_fold, apply_url_raw FROM jobs "
                f"WHERE company_fold IN ({marks}) "
                "AND duplicate_of IS NULL AND status = ? AND apply_url_raw <> '' "
                "ORDER BY last_seen DESC, apply_url_raw",
                (*chunk, JobStatus.OPEN.value),
            ).fetchall()
            for row in kept:
                self._collect_apply_url(
                    collected, wanted, str(row["company_fold"]), str(row["apply_url_raw"])
                )

        names = sorted(set(wanted.values()))
        for start in range(0, len(names), _ID_CHUNK):
            chunk = names[start : start + _ID_CHUNK]
            marks = ", ".join("?" * len(chunk))
            rejected = self._conn.execute(
                "SELECT company, apply_url_raw FROM quarantine "
                f"WHERE company IN ({marks}) "
                "AND apply_url_raw IS NOT NULL AND apply_url_raw <> '' "
                "ORDER BY last_seen DESC, apply_url_raw",
                tuple(chunk),
            ).fetchall()
            for row in rejected:
                self._collect_apply_url(
                    collected, wanted, fold_company(str(row["company"])), str(row["apply_url_raw"])
                )
        return {company: tuple(urls) for company, urls in collected.items()}

    @staticmethod
    def _collect_apply_url(
        collected: dict[str, list[str]],
        wanted: dict[str, str],
        company_fold: str,
        apply_url: str,
    ) -> None:
        company = wanted.get(company_fold)
        url = apply_url.strip()
        if company is None or not url:
            return
        urls = collected.setdefault(company, [])
        if url not in urls and len(urls) < 5:
            urls.append(url)

    def coverage_classifications(self) -> list[CoverageClassification]:
        rows = self._conn.execute(
            "SELECT company, disposition, note, checked_on, url FROM coverage_classifications "
            "ORDER BY company COLLATE NOCASE"
        ).fetchall()
        return [_classification_from_row(row) for row in rows]

    def record_coverage_classification(self, entry: CoverageClassification) -> bool:
        company, company_fold, disposition, note, checked_on, url = _classification_params(entry)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM coverage_classifications WHERE company_fold = ?", (company_fold,)
            ).fetchone()
            conn.execute(
                "INSERT INTO coverage_classifications "
                "(company, company_fold, disposition, note, checked_on, url) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(company_fold) DO UPDATE SET company = excluded.company, "
                "disposition = excluded.disposition, note = excluded.note, "
                "checked_on = excluded.checked_on, url = excluded.url",
                (company, company_fold, disposition, note, checked_on, url),
            )
        return existing is not None

    def clear_coverage_classification(self, company: str) -> bool:
        company_fold = fold_company(company)
        if not company_fold:
            raise ValueError("classification company must contain letters or numbers")
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM coverage_classifications WHERE company_fold = ?", (company_fold,)
            )
        return cursor.rowcount > 0

    def composition(self, column: str) -> dict[str, int]:
        if column not in _COMPOSITION_COLUMNS:
            raise ValueError(
                f"{column!r} is not a composition column; "
                f"known: {', '.join(sorted(_COMPOSITION_COLUMNS))}"
            )
        rows = self._conn.execute(
            f"SELECT {column} AS bucket, COUNT(*) AS total FROM jobs "
            "WHERE duplicate_of IS NULL GROUP BY bucket ORDER BY total DESC"
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            bucket = str(row["bucket"])
            if column == "location":
                bucket = _location_bucket(bucket).value
            counts[bucket] = counts.get(bucket, 0) + int(row["total"])
        return counts

    def volume_history(self, limit: int) -> Mapping[str, list[VolumePoint]]:
        rows = self._conn.execute(
            "SELECT source, stored, deferred, blocked FROM sync_run_sources "
            "WHERE run_id IN (SELECT id FROM sync_runs ORDER BY id DESC LIMIT ?) "
            "ORDER BY run_id DESC",
            (limit,),
        ).fetchall()
        history: dict[str, list[VolumePoint]] = {}
        for row in rows:
            history.setdefault(str(row["source"]), []).append(
                VolumePoint(
                    stored=int(row["stored"]),
                    deferred=int(row["deferred"]),
                    blocked=bool(row["blocked"]),
                )
            )
        return history

    def requests_since(self, since: datetime) -> tuple[dict[str, int], bool]:
        rows = self._conn.execute(
            "SELECT s.source AS source, SUM(s.requests) AS spent "
            "FROM sync_run_sources s JOIN sync_runs r ON r.id = s.run_id "
            "WHERE r.started_at >= ? GROUP BY s.source",
            (_to_text(since),),
        ).fetchall()
        seen = self._conn.execute(
            "SELECT 1 FROM sync_runs WHERE started_at >= ? LIMIT 1", (_to_text(since),)
        ).fetchone()
        return {str(row["source"]): int(row["spent"] or 0) for row in rows}, seen is not None

    def run_history(self, limit: int) -> list[SyncRun]:
        runs = self._conn.execute(
            "SELECT id, started_at, finished_at, outcome FROM sync_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if not runs:
            return []
        placeholders = ", ".join("?" * len(runs))
        stats = self._conn.execute(
            "SELECT * FROM sync_run_sources "
            f"WHERE run_id IN ({placeholders}) ORDER BY run_id DESC, source",
            tuple(int(run["id"]) for run in runs),
        ).fetchall()
        by_run: dict[int, list[SourceRunStats]] = {}
        for row in stats:
            by_run.setdefault(int(row["run_id"]), []).append(
                SourceRunStats(
                    source=str(row["source"]),
                    fetched=int(row["fetched"]),
                    added=int(row["added"]),
                    updated=int(row["updated"]),
                    closed=int(row["closed"]),
                    quarantined=int(row["quarantined"]),
                    errors=int(row["errors"]),
                    requests=int(row["requests"]),
                    not_modified=int(row["not_modified"]),
                    retries=int(row["retries"]),
                    tightenings=int(row["tightenings"]),
                    latency_p50_ms=float(row["latency_p50_ms"]),
                    latency_p95_ms=float(row["latency_p95_ms"]),
                    elapsed_ms=float(row["elapsed_ms"]),
                    deferred=int(row["deferred"]),
                    blocked=bool(row["blocked"]),
                    stored=int(row["stored"]),
                )
            )
        return [
            SyncRun(
                started_at=_require_datetime(run["started_at"], "started_at"),
                finished_at=_require_datetime(run["finished_at"], "finished_at"),
                outcome=SyncOutcome(str(run["outcome"])),
                sources=tuple(by_run.get(int(run["id"]), ())),
            )
            for run in runs
        ]

    def all_visits(self) -> list[SourceVisit]:
        rows = self._conn.execute(
            "SELECT source, board, label, last_attempt_at, last_success_at, "
            "consecutive_failures, last_error FROM source_visits "
            "ORDER BY last_success_at IS NOT NULL, last_success_at, source, board"
        ).fetchall()
        return [
            SourceVisit(
                source=str(row["source"]),
                board=str(row["board"]),
                label=str(row["label"]),
                last_attempt_at=_require_datetime(row["last_attempt_at"], "last_attempt_at"),
                last_success_at=_from_text(row["last_success_at"]),
                consecutive_failures=int(row["consecutive_failures"]),
                last_error=str(row["last_error"]),
            )
            for row in rows
        ]

    def repair_integrity(self) -> list[IntegrityRepair]:
        repairs = [
            IntegrityRepair(
                check="dangling duplicate links",
                repaired=self._execute_repair(
                    "UPDATE jobs SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL "
                    "AND duplicate_of NOT IN (SELECT id FROM jobs)"
                ),
                detail="the posting is visible again on its own",
            ),
            IntegrityRepair(
                check="same-board merges",
                repaired=self._execute_repair(
                    "UPDATE jobs SET duplicate_of = NULL WHERE id IN ("
                    "SELECT a.id FROM jobs a JOIN jobs b ON a.duplicate_of = b.id "
                    "WHERE stage_board_of(a.id, a.source) = stage_board_of(b.id, b.source))"
                ),
                detail="two rows from one board are two requisitions",
            ),
            IntegrityRepair(
                check="duplicate chains",
                repaired=self._repair_chains(),
                detail="followers now point at the survivor",
            ),
            IntegrityRepair(
                check="tombstoned rows re-ingested as new",
                repaired=self._execute_repair(
                    "UPDATE jobs SET first_seen = ("
                    "SELECT t.first_seen FROM tombstones t WHERE t.id = jobs.id) "
                    "WHERE id IN (SELECT j.id FROM jobs j JOIN tombstones t ON t.id = j.id "
                    "WHERE j.first_seen > t.first_seen)"
                ),
                detail="the original first_seen is restored from the tombstone",
            ),
        ]
        self._conn.commit()
        return [repair for repair in repairs if repair.repaired]

    def close_orphan_boards(self, sources: Sequence[str], boards: Sequence[str]) -> int:
        if not sources or not boards:
            return 0
        source_slots = ", ".join("?" * len(sources))
        board_slots = ", ".join("?" * len(boards))
        with self._transaction() as conn:
            closed = conn.execute(
                f"UPDATE jobs SET status = ? WHERE status = ? AND source IN ({source_slots}) "
                "AND stage_board_of(id, source) <> source "
                f"AND stage_board_of(id, source) NOT IN ({board_slots})",
                (JobStatus.CLOSED.value, JobStatus.OPEN.value, *sources, *boards),
            ).rowcount
        return int(closed or 0)

    def _execute_repair(self, sql: str) -> int:
        return int(self._conn.execute(sql).rowcount or 0)

    def _repair_chains(self) -> int:
        repaired = 0
        for _ in range(MAX_CHAIN_REPAIR_PASSES):
            changed = self._execute_repair(
                "UPDATE jobs SET duplicate_of = ("
                "SELECT b.duplicate_of FROM jobs b WHERE b.id = jobs.duplicate_of) "
                "WHERE duplicate_of IN (SELECT id FROM jobs WHERE duplicate_of IS NOT NULL)"
            )
            repaired += changed
            if not changed:
                break
        return repaired

    def integrity_findings(self) -> list[IntegrityFinding]:
        checks = (
            (
                "dangling duplicate links",
                "SELECT COUNT(*) AS total FROM jobs a LEFT JOIN jobs b ON a.duplicate_of = b.id "
                "WHERE a.duplicate_of IS NOT NULL AND b.id IS NULL",
                "the survivor is gone, so the posting is invisible",
            ),
            (
                "duplicate chains",
                "SELECT COUNT(*) AS total FROM jobs a JOIN jobs b ON a.duplicate_of = b.id "
                "WHERE b.duplicate_of IS NOT NULL",
                "followers must repoint at the survivor",
            ),
            (
                "same-board merges",
                "SELECT COUNT(*) AS total FROM jobs a JOIN jobs b ON a.duplicate_of = b.id "
                "WHERE stage_board_of(a.id, a.source) = stage_board_of(b.id, b.source)",
                "two rows from one board are two requisitions",
            ),
            (
                "postings in both tables",
                "SELECT COUNT(*) AS total FROM jobs j JOIN quarantine q ON q.id = j.id",
                "quarantine is a move, not a copy",
            ),
            (
                "tombstoned rows re-ingested as new",
                "SELECT COUNT(*) AS total FROM jobs j JOIN tombstones t ON t.id = j.id "
                "WHERE j.first_seen > t.first_seen",
                "a purged posting came back with a fresh date",
            ),
            (
                "open postings with no first_seen",
                "SELECT COUNT(*) AS total FROM jobs WHERE first_seen IS NULL OR first_seen = ''",
                "first_seen is the sort key and is assigned locally",
            ),
        )
        findings: list[IntegrityFinding] = []
        for check, sql, detail in checks:
            row = self._conn.execute(sql).fetchone()
            findings.append(IntegrityFinding(check=check, count=int(row["total"]), detail=detail))
        return findings
