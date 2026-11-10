import json
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

from stage.domain import UNKNOWN_TERM, Job, RateState, RoleCategory, WorkdayCrawl, WorkdayFacet
from stage.domain.liveness import (
    AWAITING_PAGE,
    BOARD_SOURCES,
    CHECK_PREFIX,
    Liveness,
    LivenessCheck,
)
from stage.domain.text import LOCATION_COUNT_GLOB
from stage.storage.rows import (
    CLOSED,
    OPEN,
    UPSERT_CHECK,
    UPSERT_PUSHBACK,
    job_from,
    rate_state_params,
)
from stage.storage.sql import EACH, Store, count, from_text, link_of, listed, to_text
from stage.storage.upkeep import promote_open_copies, remember_heirs

MAX_DETAIL_ATTEMPTS = 3
REREAD_ATTEMPTS = 2

_QUEUED = (
    "FROM jobs j LEFT JOIN detail_fetches d ON d.id = j.id "
    "WHERE j.source = ? AND j.status = ? AND ("
    "(j.description = '' AND (j.term = ? OR j.role = ?) AND (d.id IS NULL "
    "OR (d.failed = 1 AND d.attempts < ?) OR (d.resolved = 1 AND d.attempts < ?))) "
    "OR (j.location_raw GLOB ? AND (d.id IS NULL OR d.attempts < ?)))"
)


_AWAITING = (
    "FROM quarantine q LEFT JOIN detail_fetches d ON d.id = q.id "
    "WHERE q.source = ? AND q.matched_phrase GLOB ? "
    "AND (d.id IS NULL OR (d.failed = 1 AND d.attempts < ?))"
)
_QUEUE = (
    f"SELECT j.id, 1 AS rank, j.first_seen {_QUEUED} "
    f"UNION ALL SELECT q.id, 0 AS rank, q.first_seen {_AWAITING}"
)


def _queued(source: str) -> tuple[object, ...]:
    return (
        source,
        OPEN,
        UNKNOWN_TERM,
        RoleCategory.UNKNOWN.value,
        MAX_DETAIL_ATTEMPTS,
        REREAD_ATTEMPTS,
        LOCATION_COUNT_GLOB,
        REREAD_ATTEMPTS,
        source,
        f"*{AWAITING_PAGE}",
        MAX_DETAIL_ATTEMPTS,
    )


class DetailFetches(Store):
    def detail_queue(self, source: str, limit: int) -> list[str]:
        rows = self._conn.execute(
            f"SELECT id FROM ({_QUEUE}) ORDER BY rank, first_seen DESC LIMIT ?",
            (*_queued(source), limit),
        )
        return [str(row["id"]) for row in rows]

    def detail_queue_size(self, source: str) -> int:
        return count(self._conn, f"SELECT COUNT(*) FROM ({_QUEUE})", _queued(source))

    def stored_details(self, ids: Collection[str]) -> dict[str, tuple[str, str]]:
        rows = self._conn.execute(
            f"SELECT id, description, location_raw FROM jobs WHERE id IN {EACH} "
            "AND (description <> '' OR NOT location_raw GLOB ?)",
            (listed(ids), LOCATION_COUNT_GLOB),
        )
        return {str(row["id"]): (str(row["description"]), str(row["location_raw"])) for row in rows}

    def load_workday_facets(self) -> Mapping[tuple[str, str], WorkdayFacet]:
        rows = self._conn.execute(
            "SELECT tenant, site, parameter, facet_id, descriptor, resolved_at FROM workday_facets"
        )
        return {
            (str(row["tenant"]), str(row["site"])): WorkdayFacet(
                tenant=str(row["tenant"]),
                site=str(row["site"]),
                parameter=str(row["parameter"]),
                facet_ids=tuple(str(row["facet_id"]).split()),
                descriptor=str(row["descriptor"]),
                resolved_at=from_text(row["resolved_at"]),
            )
            for row in rows
        }

    def load_workday_crawls(self) -> Mapping[str, WorkdayCrawl]:
        rows = self._conn.execute(
            "SELECT board, next_offset, total, facet_parameter, facet_ids FROM workday_crawls"
        )
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

    def liveness_queue(
        self,
        since: datetime,
        checked_before: datetime,
        feeds: Collection[str],
        boards: Collection[str],
        url_marks: Sequence[str],
    ) -> list[Job]:
        marked = " OR ".join("j.apply_url_raw LIKE '%' || ? || '%'" for _ in url_marks) or "0"
        rows = self._conn.execute(
            "SELECT j.* FROM jobs j LEFT JOIN detail_fetches d ON d.id = ? || j.id "
            "LEFT JOIN source_visits v ON v.source = j.source "
            "AND v.board = stage_board_of(j.id, j.source) "
            "WHERE j.status = ? AND j.duplicate_of IS NULL AND j.first_seen >= ? "
            "AND (d.fetched_at IS NULL OR d.fetched_at < ?) "
            f"AND ((j.source IN {EACH} AND ({marked})) "
            f"OR (j.source IN {EACH} AND j.last_seen < v.last_success_at)) "
            "ORDER BY d.fetched_at IS NOT NULL, d.fetched_at, j.last_seen, j.id",
            (
                CHECK_PREFIX,
                OPEN,
                to_text(since),
                to_text(checked_before),
                listed(feeds),
                *url_marks,
                listed(boards),
            ),
        )
        return [job_from(row) for row in rows]

    def record_checks(
        self,
        checks: Sequence[LivenessCheck],
        pushback: Sequence[RateState] = (),
        *,
        wait_ms: int | None = None,
    ) -> tuple[str, ...]:
        previous = int(self._conn.execute("PRAGMA busy_timeout").fetchone()[0])
        if wait_ms is not None:
            self._conn.execute(f"PRAGMA busy_timeout = {int(wait_ms)}")
        closed: list[str] = []
        try:
            with self._transaction() as conn:
                for check in checks:
                    keys = [check.id]
                    if check.verdict is Liveness.GONE:
                        shut = [
                            str(row["id"])
                            for row in conn.execute(
                                "SELECT id FROM jobs WHERE (id = ? OR (duplicate_of = ? "
                                f"AND source NOT IN {EACH} AND NOT stage_other_opening("
                                f"(SELECT {link_of()} FROM jobs WHERE id = ?), {link_of()}))) "
                                "AND status = ? ORDER BY id",
                                (
                                    check.id,
                                    check.id,
                                    listed(sorted(BOARD_SOURCES)),
                                    check.id,
                                    OPEN,
                                ),
                            )
                        ]
                        conn.execute(
                            f"UPDATE jobs SET status = ? WHERE id IN {EACH}", (CLOSED, listed(shut))
                        )
                        closed.extend(shut)
                        keys = sorted({check.id, *shut})
                    conn.executemany(
                        UPSERT_CHECK,
                        [
                            (
                                CHECK_PREFIX + key,
                                to_text(check.checked_at),
                                int(check.verdict is Liveness.OPEN),
                                int(check.verdict is Liveness.UNKNOWN),
                                key,
                            )
                            for key in keys
                        ],
                    )
                if closed:
                    stamp = to_text(max(check.checked_at for check in checks))
                    remember_heirs(conn, sorted(promote_open_copies(conn, closed).items()), stamp)
                conn.executemany(UPSERT_PUSHBACK, [rate_state_params(state) for state in pushback])
        finally:
            self._conn.execute(f"PRAGMA busy_timeout = {previous}")
        return tuple(closed)
