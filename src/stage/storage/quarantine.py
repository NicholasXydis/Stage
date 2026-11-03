from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from typing import Any

from stage.domain import QuarantinedJob, QuarantineFilters
from stage.storage.rows import quarantined_from
from stage.storage.sql import EACH, Store, count, limit_clause, listed


def _where(filters: QuarantineFilters) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for clause, value in (
        ("reason = ?", filters.reason.value if filters.reason is not None else None),
        ("source = ?", filters.source),
        (
            "company = ? COLLATE NOCASE",
            None if filters.company is None else filters.company.strip(),
        ),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params


class QuarantineStore(Store):
    def list_quarantined(self, filters: QuarantineFilters) -> list[QuarantinedJob]:
        where, params = _where(filters)
        clause, bounds = limit_clause(filters.limit, filters.offset)
        rows = self._conn.execute(
            f"SELECT * FROM quarantine{where} ORDER BY company COLLATE NOCASE, "
            f"first_seen DESC, title_raw COLLATE NOCASE, id{clause}",
            (*params, *bounds),
        )
        return [quarantined_from(row) for row in rows]

    def count_quarantined(self, filters: QuarantineFilters) -> int:
        where, params = _where(filters)
        return count(self._conn, f"SELECT COUNT(*) FROM quarantine{where}", params)

    def quarantine_reason_counts(self, filters: QuarantineFilters | None = None) -> dict[str, int]:
        where, params = _where(replace(filters, reason=None) if filters else QuarantineFilters())
        rows = self._conn.execute(
            f"SELECT reason, COUNT(*) AS total FROM quarantine{where} GROUP BY reason "
            "ORDER BY total DESC",
            params,
        )
        return {str(row["reason"]): int(row["total"]) for row in rows}

    def quarantine_company_counts(self) -> dict[str, dict[str, int]]:
        return self._by_company("source")

    def quarantine_company_reasons(
        self, reasons: Collection[str] = ()
    ) -> dict[str, dict[str, int]]:
        return self._by_company("reason", reasons)

    def _by_company(self, column: str, reasons: Collection[str] = ()) -> dict[str, dict[str, int]]:
        found: dict[str, dict[str, int]] = {}
        chosen = f"WHERE reason IN {EACH} " if reasons else ""
        for row in self._conn.execute(
            f"SELECT company, {column} AS part, COUNT(*) AS total FROM quarantine "
            f"{chosen}GROUP BY company, {column}",
            (listed(reasons),) if reasons else (),
        ):
            found.setdefault(str(row["company"]), {})[str(row["part"])] = int(row["total"])
        return found

    def relabel_quarantine(self, entries: Sequence[QuarantinedJob]) -> int:
        if not entries:
            return 0
        with self._transaction() as conn:
            return conn.executemany(
                "UPDATE quarantine SET reason = ?, matched_phrase = ? "
                "WHERE id = ? AND (reason <> ? OR matched_phrase <> ?)",
                [
                    (
                        entry.reason.value,
                        entry.matched_phrase,
                        entry.id,
                        entry.reason.value,
                        entry.matched_phrase,
                    )
                    for entry in entries
                ],
            ).rowcount

    def refresh_quarantine_locations(
        self, resolve: Callable[[str, str, str], tuple[str, str | None]]
    ) -> int:
        updates = []
        for ident, raw, url, title, bucket, scope in self._conn.execute(
            "SELECT id, location_raw, apply_url_raw, title_raw, location, remote_scope "
            "FROM quarantine"
        ):
            found = resolve(raw or "", url or "", title or "")
            if found != (bucket, scope):
                updates.append((*found, ident))
        if not updates:
            return 0
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE quarantine SET location = ?, remote_scope = ? WHERE id = ?", updates
            )
        return len(updates)
