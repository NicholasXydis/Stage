import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from stage.domain import (
    CoverageClassification,
    CoverageDisposition,
    DegreeRequirement,
    Job,
    JobStatus,
    Language,
    LocationBucket,
    QuarantinedJob,
    RateState,
    RejectionReason,
    RemoteScope,
    RoleCategory,
    SourceRunStats,
    SourceSignals,
    SourceVisit,
    public_https_url,
)
from stage.storage.sql import fold_company, from_text, marks, required_time, to_text

OPEN = JobStatus.OPEN.value
CLOSED = JobStatus.CLOSED.value

_LEGACY_LOCATIONS = {"other": "international", "remote": "unknown"}
_STORED_LOCATIONS = {
    LocationBucket.INTERNATIONAL: ("international", "other"),
    LocationBucket.UNKNOWN: ("unknown", "remote"),
}


def location_bucket(value: str) -> LocationBucket:
    return LocationBucket(_LEGACY_LOCATIONS.get(value, value))


def stored_locations(bucket: LocationBucket) -> tuple[str, ...]:
    return _STORED_LOCATIONS.get(bucket, (bucket.value,))


def _scope(value: str | None) -> RemoteScope | None:
    return RemoteScope(value) if value else None


def upsert(table: str, columns: Sequence[str], key: str, updates: Sequence[str]) -> str:
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks(len(columns))}) "
        f"ON CONFLICT({key}) DO UPDATE SET {', '.join(updates)}"
    )


def refreshed(*columns: str) -> list[str]:
    return [f"{column} = excluded.{column}" for column in columns]


JOB_COLUMNS = (
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
UPSERT_JOB = upsert(
    "jobs",
    JOB_COLUMNS,
    "id",
    [
        "first_seen = MIN(jobs.first_seen, excluded.first_seen)"
        if column == "first_seen"
        else f"{column} = excluded.{column}"
        for column in JOB_COLUMNS[1:-1]
    ],
)

QUARANTINE_COLUMNS = (
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
UPSERT_QUARANTINE = upsert(
    "quarantine",
    QUARANTINE_COLUMNS,
    "id",
    refreshed(*(column for column in QUARANTINE_COLUMNS[1:] if column != "first_seen")),
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
_LONGER_BLOCK = (
    "blocked_until = NULLIF(MAX(COALESCE(excluded.blocked_until, ''), "
    "COALESCE(rate_state.blocked_until, '')), '')",
    "reason = CASE WHEN COALESCE(rate_state.blocked_until, '') > "
    "COALESCE(excluded.blocked_until, '') THEN rate_state.reason ELSE excluded.reason END",
)
UPSERT_RATE_STATE = upsert(
    "rate_state",
    _RATE_STATE_COLUMNS,
    "bucket",
    [
        *_LONGER_BLOCK,
        *refreshed(
            "min_interval_override",
            "consecutive_failures",
            "last_failure_at",
            "rotation_cursor",
            "updated_at",
        ),
    ],
)
UPSERT_PUSHBACK = upsert(
    "rate_state",
    _RATE_STATE_COLUMNS,
    "bucket",
    [
        *_LONGER_BLOCK,
        "min_interval_override = NULLIF(MAX(COALESCE(rate_state.min_interval_override, 0), "
        "COALESCE(excluded.min_interval_override, 0)), 0)",
        "consecutive_failures = MAX(rate_state.consecutive_failures, "
        "excluded.consecutive_failures)",
        "last_failure_at = COALESCE(excluded.last_failure_at, rate_state.last_failure_at)",
        "updated_at = excluded.updated_at",
    ],
)
RATE_STATE_SELECT = f"SELECT {', '.join(_RATE_STATE_COLUMNS)} FROM rate_state"

_VISIT_COLUMNS = (
    "source",
    "board",
    "label",
    "last_attempt_at",
    "last_success_at",
    "consecutive_failures",
    "last_error",
)
UPSERT_VISIT = upsert(
    "source_visits",
    _VISIT_COLUMNS,
    "source, board",
    [
        *refreshed("label", "last_attempt_at"),
        "last_success_at = COALESCE(excluded.last_success_at, source_visits.last_success_at)",
        "consecutive_failures = CASE WHEN excluded.consecutive_failures = 0 THEN 0 "
        "ELSE source_visits.consecutive_failures + 1 END",
        "last_error = excluded.last_error",
    ],
)
VISIT_SELECT = f"SELECT {', '.join(_VISIT_COLUMNS)} FROM source_visits"

_FETCH_COLUMNS = ("id", "source", "fetched_at", "resolved", "attempts", "failed")
_FETCHED_AGAIN = (
    *refreshed("fetched_at", "resolved"),
    "attempts = detail_fetches.attempts + 1",
    "failed = excluded.failed",
)
UPSERT_DETAIL = upsert("detail_fetches", _FETCH_COLUMNS, "id", _FETCHED_AGAIN)
UPSERT_HEIR = upsert("detail_fetches", _FETCH_COLUMNS, "id", refreshed("source", "fetched_at"))
UPSERT_CHECK = (
    f"INSERT INTO detail_fetches ({', '.join(_FETCH_COLUMNS)}) "
    "SELECT ?, source, ?, ?, 1, ? FROM jobs WHERE id = ? "
    f"ON CONFLICT(id) DO UPDATE SET {', '.join(_FETCHED_AGAIN)}"
)

UPSERT_CRAWL = upsert(
    "workday_crawls",
    ("board", "next_offset", "total", "facet_parameter", "facet_ids"),
    "board",
    refreshed("next_offset", "total", "facet_parameter", "facet_ids"),
)
UPSERT_FACET = upsert(
    "workday_facets",
    ("tenant", "site", "parameter", "facet_id", "descriptor", "resolved_at"),
    "tenant, site",
    refreshed("parameter", "facet_id", "descriptor", "resolved_at"),
)
UPSERT_VALIDATOR = upsert(
    "http_cache",
    ("url", "source", "etag", "last_modified", "fetched_at"),
    "url",
    refreshed("source", "etag", "last_modified", "fetched_at"),
)
UPSERT_TOMBSTONE = upsert(
    "tombstones", ("id", "source", "first_seen", "purged_at"), "id", refreshed("purged_at")
)
UPSERT_CLASSIFICATION = upsert(
    "coverage_classifications",
    ("company", "company_fold", "disposition", "note", "checked_on", "url"),
    "company_fold",
    refreshed("company", "disposition", "note", "checked_on", "url"),
)

STAT_COLUMNS = (
    "source",
    "fetched",
    "added",
    "updated",
    "closed",
    "quarantined",
    "errors",
    "requests",
    "not_modified",
    "retries",
    "tightenings",
    "latency_p50_ms",
    "latency_p95_ms",
    "elapsed_ms",
    "deferred",
    "blocked",
    "stored",
)


def job_from(row: sqlite3.Row) -> Job:
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
        location=location_bucket(row["location"]),
        remote_scope=_scope(row["remote_scope"]),
        language=Language(row["language"]),
        term=row["term"],
        role=RoleCategory(row["role"]),
        work_auth_flag=bool(row["work_auth_flag"]),
        degree_requirement=DegreeRequirement(row["degree_requirement"]),
        compensation=row["compensation"],
        signals=SourceSignals(
            employment_type=row["employment_type"], category=row["source_category"]
        ),
        status=JobStatus(row["status"]),
        first_seen=required_time(row["first_seen"], "first_seen"),
        last_seen=required_time(row["last_seen"], "last_seen"),
        source_posted_at=from_text(row["source_posted_at"]),
        duplicate_of=row["duplicate_of"],
    )


def job_params(job: Job) -> tuple[Any, ...]:
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
        to_text(job.first_seen),
        to_text(job.last_seen),
        to_text(job.source_posted_at) if job.source_posted_at else None,
        job.duplicate_of,
    )


def quarantined_from(row: sqlite3.Row) -> QuarantinedJob:
    return QuarantinedJob(
        id=row["id"],
        source=row["source"],
        company=row["company"],
        title_raw=row["title_raw"],
        reason=RejectionReason(row["reason"]),
        first_seen=required_time(row["first_seen"], "first_seen"),
        last_seen=required_time(row["last_seen"], "last_seen"),
        apply_url_raw=row["apply_url_raw"],
        location_raw=row["location_raw"],
        location=location_bucket(row["location"]),
        remote_scope=_scope(row["remote_scope"]),
        matched_phrase=row["matched_phrase"],
    )


def quarantined_params(entry: QuarantinedJob, first_seen: datetime) -> tuple[Any, ...]:
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
        to_text(first_seen),
        to_text(entry.last_seen),
    )


def rate_state_from(row: sqlite3.Row) -> RateState:
    override = row["min_interval_override"]
    return RateState(
        bucket=str(row["bucket"]),
        updated_at=required_time(row["updated_at"], "updated_at"),
        blocked_until=from_text(row["blocked_until"]),
        min_interval_override=float(override) if override is not None else None,
        consecutive_failures=int(row["consecutive_failures"]),
        last_failure_at=from_text(row["last_failure_at"]),
        reason=str(row["reason"]),
        rotation_cursor=str(row["rotation_cursor"]),
    )


def rate_state_params(state: RateState) -> tuple[Any, ...]:
    return (
        state.bucket,
        to_text(state.blocked_until) if state.blocked_until else None,
        state.min_interval_override,
        state.consecutive_failures,
        to_text(state.last_failure_at) if state.last_failure_at else None,
        state.reason,
        state.rotation_cursor,
        to_text(state.updated_at),
    )


def visit_from(row: sqlite3.Row) -> SourceVisit:
    return SourceVisit(
        source=str(row["source"]),
        board=str(row["board"]),
        label=str(row["label"]),
        last_attempt_at=required_time(row["last_attempt_at"], "last_attempt_at"),
        last_success_at=from_text(row["last_success_at"]),
        consecutive_failures=int(row["consecutive_failures"]),
        last_error=str(row["last_error"]),
    )


def stats_from(row: sqlite3.Row) -> SourceRunStats:
    values = {column: row[column] for column in STAT_COLUMNS}
    return SourceRunStats(**{**values, "blocked": bool(values["blocked"])})


def stats_params(run_id: int | None, stats: SourceRunStats) -> tuple[Any, ...]:
    return (run_id, *(getattr(stats, column) for column in STAT_COLUMNS))


def classification_from(row: sqlite3.Row) -> CoverageClassification:
    return CoverageClassification(
        company=str(row["company"]),
        disposition=CoverageDisposition(str(row["disposition"])),
        note=str(row["note"]),
        checked_on=required_time(row["checked_on"], "checked_on"),
        url=str(row["url"]) if row["url"] is not None else None,
    )


def company_key(company: str) -> str:
    folded = fold_company(company)
    if not folded:
        raise ValueError(
            f'"{company.strip()}" is not an employer name. Give one with letters or numbers.'
        )
    return folded


def classification_params(entry: CoverageClassification) -> tuple[Any, ...]:
    company = entry.company.strip()
    folded = company_key(company)
    note = entry.note.strip()
    if not note:
        raise ValueError(
            f"The note for {company} is empty. Say why it is filed this way with --note."
        )
    if not isinstance(entry.disposition, CoverageDisposition):
        raise ValueError(f'"{entry.disposition}" is not a coverage status.')
    url = None if entry.url is None else public_https_url(entry.url)
    if entry.url is not None and url is None:
        raise ValueError(
            f'"{entry.url}" is not a public https address. Give --url a link such as '
            "https://example.com/careers, without a user name or password."
        )
    return company, folded, entry.disposition.value, note, to_text(entry.checked_on), url
