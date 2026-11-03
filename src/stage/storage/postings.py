from collections.abc import Collection, Sequence
from typing import Any

from stage.domain import (
    CoverageClassification,
    Job,
    JobFilters,
    JobStatus,
    Language,
    LocationBucket,
    board_of,
)
from stage.storage.rows import (
    CLOSED,
    OPEN,
    UPSERT_CLASSIFICATION,
    classification_from,
    classification_params,
    company_key,
    job_from,
    location_bucket,
    stored_locations,
)
from stage.storage.search import match_expression, search_terms
from stage.storage.sql import (
    EACH,
    Store,
    board_rows,
    count,
    fold_company,
    limit_clause,
    listed,
    marks,
    to_text,
)

_NEWEST_FIRST = (
    "jobs.first_seen DESC, "
    "CASE WHEN jobs.source_posted_at <= jobs.last_seen THEN jobs.source_posted_at END DESC, "
    "jobs.id ASC"
)
_COMPOSITION_COLUMNS = frozenset(
    {"source", "location", "role", "term", "language", "status", "degree_requirement"}
)
_KEPT_URLS = 5


def _among(column: str, values: Collection[str], verb: str = "IN") -> tuple[str, list[Any]]:
    chosen = sorted(values)
    return f"{column} {verb} ({marks(len(chosen))})", chosen


def _clauses(filters: JobFilters) -> tuple[list[str], list[Any]]:
    clauses: list[str] = ["1"] if filters.with_duplicates else ["jobs.duplicate_of IS NULL"]
    params: list[Any] = []

    def where(clause: str, values: Sequence[Any]) -> None:
        clauses.append(clause)
        params.extend(values)

    if filters.status is not None:
        where("jobs.status = ?", [filters.status.value])
    if filters.location is not None:
        where(*_among("jobs.location", stored_locations(filters.location)))
    if filters.term is not None:
        where("jobs.term = ?", [filters.term])
    if filters.degree is not None:
        where("jobs.degree_requirement = ?", [filters.degree.value])
    if filters.role is not None:
        where("jobs.role = ?", [filters.role.value])
    if filters.language in (Language.EN, Language.FR):
        where(*_among("jobs.language", (filters.language.value, Language.BILINGUAL.value)))
    elif filters.language is not None:
        where("jobs.language = ?", [filters.language.value])
    if filters.source is not None:
        where("jobs.source = ?", [filters.source])
    if filters.company is not None:
        folded = fold_company(filters.company)
        where(
            "jobs.company_fold = ?" if folded else "jobs.company = ?", [folded or filters.company]
        )
    if filters.first_seen_after is not None:
        where("jobs.first_seen >= ?", [to_text(filters.first_seen_after)])
    if filters.locations:
        placed, places = _among(
            "jobs.location",
            {value for bucket in filters.locations for value in stored_locations(bucket)},
        )
        if filters.follow_companies:
            followed, companies = _among("jobs.company_fold", filters.follow_companies)
            unplaced, nowhere = _among(
                "jobs.location", stored_locations(LocationBucket.UNKNOWN), "NOT IN"
            )
            placed = f"({placed} OR ({followed} AND {unplaced}))"
            places = [*places, *companies, *nowhere]
        where(placed, places)
    if filters.roles:
        where(*_among("jobs.role", {role.value for role in filters.roles}))
    if filters.terms:
        where(*_among("jobs.term", filters.terms))
    if filters.avoid_companies:
        where(*_among("jobs.company_fold", filters.avoid_companies, "NOT IN"))
    if filters.excluded_ids:
        where(f"jobs.id NOT IN {EACH}", [listed(filters.excluded_ids)])
    return clauses, params


def _matching(query: str, filters: JobFilters) -> tuple[str, list[Any]] | None:
    expression = match_expression(search_terms(query))
    if not expression:
        return None
    clauses, params = _clauses(filters)
    clauses.insert(0, "jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
    return f"FROM jobs WHERE {' AND '.join(clauses)}", [expression, *params]


class PostingReads(Store):
    def list_jobs(self, filters: JobFilters) -> list[Job]:
        clauses, params = _clauses(filters)
        return self._jobs(f"FROM jobs WHERE {' AND '.join(clauses)}", params, filters.limit)

    def count_jobs(self, filters: JobFilters) -> int:
        clauses, params = _clauses(filters)
        return count(self._conn, f"SELECT COUNT(*) FROM jobs WHERE {' AND '.join(clauses)}", params)

    def search_jobs(self, query: str, filters: JobFilters) -> list[Job]:
        found = _matching(query, filters)
        return self._jobs(*found, filters.limit) if found else []

    def count_search(self, query: str, filters: JobFilters) -> int:
        found = _matching(query, filters)
        return count(self._conn, f"SELECT COUNT(*) {found[0]}", found[1]) if found else 0

    def _jobs(self, source: str, params: list[Any], limit: int | None) -> list[Job]:
        clause, bounds = limit_clause(limit)
        rows = self._conn.execute(
            f"SELECT jobs.* {source} ORDER BY {_NEWEST_FIRST}{clause}", (*params, *bounds)
        )
        return [job_from(row) for row in rows]

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return job_from(row) if row else None

    def duplicates_of(self, job_id: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE duplicate_of = ? ORDER BY source, id", (job_id,)
        )
        return [job_from(row) for row in rows]

    def company_names(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT company FROM jobs ORDER BY company COLLATE NOCASE"
        )
        return [str(row["company"]) for row in rows]

    def ids_with_prefix(self, prefix: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM jobs WHERE substr(id, 1, ?) = ? ORDER BY id", (len(prefix), prefix)
        )
        return [str(row["id"]) for row in rows]

    def canonical_ids(self, job_ids: Collection[str]) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT j.id, j.duplicate_of FROM json_each(?) w CROSS JOIN jobs j ON j.id = w.value "
            "WHERE j.duplicate_of IS NOT NULL",
            (listed(job_ids),),
        )
        return {str(row["id"]): str(row["duplicate_of"]) for row in rows}

    def posting_states(self, job_ids: Collection[str]) -> dict[str, JobStatus]:
        rows = self._conn.execute(
            f"SELECT id, status FROM jobs WHERE id IN {EACH}", (listed(job_ids),)
        )
        return {str(row["id"]): JobStatus(row["status"]) for row in rows}

    def closed_among(self, job_ids: Sequence[str]) -> int:
        return count(
            self._conn,
            "SELECT COUNT(*) FROM json_each(?) w CROSS JOIN jobs j ON j.id = w.value "
            "WHERE j.status = ?",
            (listed(job_ids), CLOSED),
        )

    def open_counts_by_company(self, keys: Collection[str]) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT j.company_fold, COUNT(*) AS total FROM json_each(?) w "
            "CROSS JOIN jobs j ON j.company_fold = w.value "
            "WHERE j.duplicate_of IS NULL AND j.status = ? GROUP BY j.company_fold",
            (listed(keys), OPEN),
        )
        return {str(row["company_fold"]): int(row["total"]) for row in rows}

    def stored_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) AS total FROM jobs WHERE status = ? GROUP BY source", (OPEN,)
        )
        return {str(row["source"]): int(row["total"]) for row in rows}

    def board_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._conn.execute("SELECT id, source FROM jobs WHERE status = ?", (OPEN,)):
            key = board_of(str(row["id"]), str(row["source"]))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def company_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for row in self._conn.execute(
            "SELECT company, source, COUNT(*) AS total FROM jobs "
            "WHERE duplicate_of IS NULL AND status = ? GROUP BY company, source",
            (OPEN,),
        ):
            counts.setdefault(str(row["company"]), {})[str(row["source"])] = int(row["total"])
        return counts

    def composition(self, column: str) -> dict[str, int]:
        if column not in _COMPOSITION_COLUMNS:
            raise ValueError(
                f'"{column}" is not a column Stage counts by. '
                f"Use one of {', '.join(sorted(_COMPOSITION_COLUMNS))}."
            )
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            f"SELECT {column} AS bucket, COUNT(*) AS total FROM jobs "
            "WHERE duplicate_of IS NULL GROUP BY bucket ORDER BY total DESC"
        ):
            bucket = str(row["bucket"])
            if column == "location":
                bucket = location_bucket(bucket).value
            counts[bucket] = counts.get(bucket, 0) + int(row["total"])
        return counts

    def company_apply_urls(
        self, companies: Sequence[str], *, kept_only: bool = False
    ) -> dict[str, tuple[str, ...]]:
        wanted = {fold_company(company): company for company in companies}
        if not wanted:
            return {}
        listed_rows = self._conn.execute(
            "SELECT j.company_fold AS folded, j.apply_url_raw FROM json_each(?) w "
            "CROSS JOIN jobs j ON j.company_fold = w.value WHERE j.duplicate_of IS NULL "
            "AND j.status = ? AND j.apply_url_raw <> '' ORDER BY j.last_seen DESC, j.apply_url_raw",
            (listed(wanted), OPEN),
        ).fetchall()
        rejected_rows = (
            []
            if kept_only
            else self._conn.execute(
                f"SELECT company, apply_url_raw FROM quarantine WHERE company IN {EACH} "
                "AND apply_url_raw <> '' ORDER BY last_seen DESC, apply_url_raw",
                (listed(wanted.values()),),
            ).fetchall()
        )
        collected: dict[str, list[str]] = {}
        found = [(str(row["folded"]), str(row["apply_url_raw"])) for row in listed_rows] + [
            (fold_company(str(row["company"])), str(row["apply_url_raw"])) for row in rejected_rows
        ]
        for folded, raw in found:
            company, url = wanted.get(folded), raw.strip()
            if company is None or not url:
                continue
            urls = collected.setdefault(company, [])
            if url not in urls and len(urls) < _KEPT_URLS:
                urls.append(url)
        return {company: tuple(urls) for company, urls in collected.items()}

    def boards_with_postings(self, boards: Sequence[str]) -> set[str]:
        return {
            board
            for board in boards
            if self._conn.execute(
                "SELECT 1 WHERE EXISTS (SELECT 1 FROM jobs WHERE id GLOB ?) "
                "OR EXISTS (SELECT 1 FROM tombstones WHERE id GLOB ?)",
                (board_rows(board), board_rows(board)),
            ).fetchone()
        }

    def coverage_classifications(self) -> list[CoverageClassification]:
        rows = self._conn.execute(
            "SELECT company, disposition, note, checked_on, url FROM coverage_classifications "
            "ORDER BY company COLLATE NOCASE"
        )
        return [classification_from(row) for row in rows]

    def record_coverage_classification(self, entry: CoverageClassification) -> bool:
        params = classification_params(entry)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM coverage_classifications WHERE company_fold = ?", (params[1],)
            ).fetchone()
            conn.execute(UPSERT_CLASSIFICATION, params)
        return existing is not None

    def clear_coverage_classification(self, company: str) -> bool:
        folded = company_key(company)
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM coverage_classifications WHERE company_fold = ?", (folded,)
            )
        return cursor.rowcount > 0
