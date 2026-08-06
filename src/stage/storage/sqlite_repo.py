import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stage.domain import (
    CLOSED_RETENTION_DAYS,
    OPEN_RETENTION_DAYS,
    UNKNOWN_TERM,
    DegreeRequirement,
    HttpValidator,
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
    SourceVisit,
    SyncRun,
    WorkdayFacet,
    source_rank,
)
from stage.paths import restrict_permissions
from stage.storage.migrations import migrate
from stage.storage.repository import SourceBatch, SourceBatchResult

_JOB_COLUMNS = (
    "id",
    "source",
    "company",
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
    "status",
    "first_seen",
    "last_seen",
    "source_posted_at",
    "duplicate_of",
)

_UPDATE_ON_CONFLICT = (
    "source = excluded.source",
    "company = excluded.company",
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

_QUEUE_ELIGIBLE = "(d.id IS NULL OR (d.failed = 1 AND d.attempts < ?))"


def _board_glob(board: str) -> str:
    return f"{board}:*"


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
        location=LocationBucket(row["location"]),
        remote_scope=RemoteScope(remote_scope) if remote_scope else None,
        language=Language(row["language"]),
        term=row["term"],
        role=RoleCategory(row["role"]),
        work_auth_flag=bool(row["work_auth_flag"]),
        degree_requirement=DegreeRequirement(row["degree_requirement"]),
        compensation=row["compensation"],
        status=JobStatus(row["status"]),
        first_seen=_require_datetime(row["first_seen"], "first_seen"),
        last_seen=_require_datetime(row["last_seen"], "last_seen"),
        source_posted_at=_from_text(row["source_posted_at"]),
    )


def _job_to_params(job: Job) -> tuple[Any, ...]:
    return (
        job.id,
        job.source,
        job.company,
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
        job.status.value,
        _to_text(job.first_seen),
        _to_text(job.last_seen),
        _to_text(job.source_posted_at) if job.source_posted_at else None,
        None,
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
        location=LocationBucket(row["location"]),
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


class SqliteRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    @classmethod
    def connect(cls, db_path: Path) -> "SqliteRepository":
        is_new = not db_path.exists()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        migrate(conn, db_path)
        if is_new:
            restrict_permissions(db_path)
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
        companies = sorted({job.company for job in jobs})
        urls = sorted({job.apply_url_canonical for job in jobs if job.apply_url_canonical})
        found: dict[str, Job] = {}
        for column, values in (("company", companies), ("apply_url_canonical", urls)):
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
            (str(link.duplicate_id), str(link.canonical_id))  # type: ignore[attr-defined]
            for link in links
        ]
        duplicates = {duplicate for duplicate, _ in pairs}
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

    def _promote_orphaned_duplicates(self, purged: Sequence[str]) -> int:
        promoted = 0
        for chunk in self._chunked(purged):
            placeholders = ", ".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT id, source, duplicate_of FROM jobs "
                f"WHERE duplicate_of IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            clusters: dict[str, list[tuple[int, str]]] = {}
            for row in rows:
                clusters.setdefault(str(row["duplicate_of"]), []).append(
                    source_rank(str(row["source"]), str(row["id"]))
                )
            for members in clusters.values():
                members.sort()
                winner = members[0][1]
                self._conn.execute(
                    "UPDATE jobs SET duplicate_of = NULL WHERE id = ?", (winner,)
                )
                for _, member in members[1:]:
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
            if not rows:
                return PurgeResult()
            ids = [str(row["id"]) for row in rows]
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
            promoted = self._promote_orphaned_duplicates(ids)
            for chunk in self._chunked(ids):
                placeholders = ", ".join("?" * len(chunk))
                conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(chunk))
        return PurgeResult(purged=len(ids), tombstoned=len(ids), promoted=promoted)

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
            known = self._preserved_first_seen(
                [job.id for job in batch.jobs] + quarantined_ids
            )
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
                    conn.execute(
                        f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(chunk)
                    )
            released = {job.id for job in jobs} & set(known)
            if released:
                for chunk in self._chunked(sorted(released)):
                    placeholders = ", ".join("?" * len(chunk))
                    conn.execute(
                        f"DELETE FROM quarantine WHERE id IN ({placeholders})", tuple(chunk)
                    )

            touched = 0
            for board in batch.unchanged_boards:
                cursor = conn.execute(
                    "UPDATE jobs SET last_seen = ? WHERE source = ? AND id GLOB ? "
                    "AND status = ?",
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
        return SourceBatchResult(
            source=batch.source,
            fetched=len(jobs) + len(batch.quarantined),
            added=len(jobs) - updated,
            updated=updated,
            closed=closed,
            touched=touched,
            quarantined=len(batch.quarantined),
            duplicates=linked,
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
            "SELECT tenant, site, parameter, facet_id, descriptor, resolved_at "
            "FROM workday_facets"
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
        rows = self._conn.execute(
            f"SELECT * FROM quarantine{where} ORDER BY first_seen DESC, id ASC LIMIT ?",
            (*params, filters.limit),
        ).fetchall()
        return [_row_to_quarantined(row) for row in rows]

    def count_quarantined(self, filters: QuarantineFilters) -> int:
        where, params = self._quarantine_where(filters)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM quarantine{where}", tuple(params)
        ).fetchone()
        return int(row["total"])

    def quarantine_reason_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT reason, COUNT(*) AS total FROM quarantine GROUP BY reason ORDER BY total DESC"
        ).fetchall()
        return {str(row["reason"]): int(row["total"]) for row in rows}

    def _where(self, filters: JobFilters) -> tuple[str, list[Any]]:
        clauses: list[str] = ["duplicate_of IS NULL"]
        params: list[Any] = []
        if filters.status is not None:
            clauses.append("status = ?")
            params.append(filters.status.value)
        if filters.location is not None:
            clauses.append("location = ?")
            params.append(filters.location.value)
        if filters.term is not None:
            clauses.append("term = ?")
            params.append(filters.term)
        if filters.degree is not None:
            clauses.append("degree_requirement = ?")
            params.append(filters.degree.value)
        if filters.role is not None:
            clauses.append("role = ?")
            params.append(filters.role.value)
        if filters.language is not None:
            clauses.append("language = ?")
            params.append(filters.language.value)
        if filters.source is not None:
            clauses.append("source = ?")
            params.append(filters.source)
        if filters.company is not None:
            clauses.append("company = ?")
            params.append(filters.company)
        if filters.first_seen_after is not None:
            clauses.append("first_seen >= ?")
            params.append(_to_text(filters.first_seen_after))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_jobs(self, filters: JobFilters) -> list[Job]:
        where, params = self._where(filters)
        rows = self._conn.execute(
            f"SELECT * FROM jobs{where} ORDER BY first_seen DESC, id ASC LIMIT ?",
            (*params, filters.limit),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_jobs(self, filters: JobFilters) -> int:
        where, params = self._where(filters)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM jobs{where}", tuple(params)
        ).fetchone()
        return int(row["total"])

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

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
                "latency_p95_ms, elapsed_ms, deferred, blocked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    )
                    for stats in run.sources
                ],
            )

    def last_sync_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT finished_at FROM sync_runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return _from_text(row["finished_at"]) if row else None
