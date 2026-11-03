import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta

from stage.domain import (
    CLOSED_RETENTION_DAYS,
    FIRST_READ_PREFIX,
    FULL_READ_PREFIX,
    HEIR_PREFIX,
    OPEN_RETENTION_DAYS,
    QUARANTINE_RETENTION_DAYS,
    SEARCH_PREFIX,
    IntegrityFinding,
    IntegrityRepair,
    PurgeResult,
    source_rank,
)
from stage.domain.liveness import CHECK_PREFIX
from stage.storage.rows import CLOSED, OPEN, UPSERT_HEIR, UPSERT_TOMBSTONE
from stage.storage.sql import EACH, Store, count, listed, marks, starting, to_text

MAX_CHAIN_REPAIR_PASSES = 8

_EXPIRED = "(status = ? AND first_seen < ?) OR (status = ? AND first_seen < ?)"

_SAME_BOARD = (
    "SELECT a.id FROM jobs a JOIN jobs b ON a.duplicate_of = b.id "
    "WHERE stage_board_of(a.id, a.source) = stage_board_of(b.id, b.source) "
    "AND NOT (a.apply_url_canonical = b.apply_url_canonical "
    "AND stage_workday_opening(a.apply_url_canonical))"
)
_CHECKS = (
    (
        "dangling duplicate links",
        "SELECT COUNT(*) FROM jobs a LEFT JOIN jobs b ON a.duplicate_of = b.id "
        "WHERE a.duplicate_of IS NOT NULL AND b.id IS NULL",
        "a copy points at a main posting that is gone, so it is hidden",
    ),
    (
        "duplicate chains",
        "SELECT COUNT(*) FROM jobs a JOIN jobs b ON a.duplicate_of = b.id "
        "WHERE b.duplicate_of IS NOT NULL",
        "a copy points at another copy instead of the main posting",
    ),
    (
        "same-board merges",
        f"SELECT COUNT(*) FROM ({_SAME_BOARD})",
        "two postings from one board were merged, but they are separate jobs",
    ),
    (
        "postings in both tables",
        "SELECT COUNT(*) FROM jobs j JOIN quarantine q ON q.id = j.id",
        "a posting is both listed and rejected",
    ),
    (
        "tombstoned rows re-ingested as new",
        "SELECT COUNT(*) FROM jobs j JOIN tombstones t ON t.id = j.id "
        "WHERE j.first_seen > t.first_seen",
        "a purged posting came back with a newer first-seen date",
    ),
    (
        "open postings with no first_seen",
        "SELECT COUNT(*) FROM jobs WHERE first_seen IS NULL OR first_seen = ''",
        "a posting has no first-seen date, so it cannot be sorted",
    ),
)


def _expiry(now: datetime, open_days: int, closed_days: int) -> tuple[str, ...]:
    return (
        OPEN,
        to_text(now - timedelta(days=open_days)),
        CLOSED,
        to_text(now - timedelta(days=closed_days)),
    )


def _rejections_cutoff(now: datetime) -> str:
    return to_text(now - timedelta(days=QUARANTINE_RETENTION_DAYS))


def promote_orphaned_duplicates(conn: sqlite3.Connection, leaving: Sequence[str]) -> dict[str, str]:
    doomed = set(leaving)
    rows = conn.execute(
        "SELECT j.id, j.source, j.duplicate_of FROM json_each(?) w "
        "CROSS JOIN jobs j ON j.duplicate_of = w.value",
        (listed(doomed),),
    ).fetchall()
    rows += conn.execute(
        "SELECT a.id, a.source, a.duplicate_of FROM jobs a LEFT JOIN jobs b "
        "ON a.duplicate_of = b.id WHERE a.duplicate_of IS NOT NULL AND b.id IS NULL"
    ).fetchall()
    return _promote(conn, [row for row in rows if str(row["id"]) not in doomed])


def promote_open_copies(conn: sqlite3.Connection, closed: Sequence[str]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT j.id, j.source, j.duplicate_of FROM json_each(?) w "
        "CROSS JOIN jobs j ON j.duplicate_of = w.value WHERE j.status = ?",
        (listed(closed), OPEN),
    ).fetchall()
    return _promote(conn, rows)


def _promote(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> dict[str, str]:
    clusters: dict[str, set[tuple[int, str]]] = {}
    for row in rows:
        clusters.setdefault(str(row["duplicate_of"]), set()).add(
            source_rank(str(row["source"]), str(row["id"]))
        )
    successors: dict[str, str] = {}
    for former, members in clusters.items():
        (_, winner), *rest = sorted(members)
        conn.execute("UPDATE jobs SET duplicate_of = NULL WHERE id = ?", (winner,))
        conn.executemany(
            "UPDATE jobs SET duplicate_of = ? WHERE id = ?",
            [(winner, member) for _, member in rest],
        )
        successors[former] = winner
    return successors


def heirs_of(
    conn: sqlite3.Connection, leaving: Collection[str], promoted: Mapping[str, str]
) -> dict[str, str]:
    heirs = dict(promoted)
    rows = conn.execute(
        "SELECT j.id, j.duplicate_of FROM json_each(?) w CROSS JOIN jobs j ON j.id = w.value "
        "WHERE j.duplicate_of IS NOT NULL",
        (listed(leaving),),
    )
    for row in rows:
        main = str(row["duplicate_of"])
        heir = promoted.get(main, main if main not in leaving else "")
        if heir:
            heirs[str(row["id"])] = heir
    return heirs


def remember_heirs(conn: sqlite3.Connection, heirs: Sequence[tuple[str, str]], stamp: str) -> None:
    conn.executemany(UPSERT_HEIR, [(HEIR_PREFIX + old, new, stamp, 0, 1, 0) for old, new in heirs])


def _reseat_inverted_links(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
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
        conn.execute(
            "UPDATE jobs SET duplicate_of = ? WHERE duplicate_of = ? AND id != ?",
            (duplicate, canonical, duplicate),
        )
        conn.execute("UPDATE jobs SET duplicate_of = NULL WHERE id = ?", (duplicate,))
        conn.execute("UPDATE jobs SET duplicate_of = ? WHERE id = ?", (duplicate, canonical))
        swapped += 1
    return swapped


def _forget_checks(conn: sqlite3.Connection, leaving: Sequence[str]) -> None:
    conn.execute(
        "DELETE FROM detail_fetches WHERE id IN (SELECT ? || value FROM json_each(?))",
        (CHECK_PREFIX, listed(leaving)),
    )
    conn.execute(
        "DELETE FROM detail_fetches WHERE id GLOB ? AND NOT EXISTS "
        "(SELECT 1 FROM jobs j WHERE j.id = substr(detail_fetches.id, ?))",
        (starting(CHECK_PREFIX), len(CHECK_PREFIX) + 1),
    )
    kept = (CHECK_PREFIX, FULL_READ_PREFIX, FIRST_READ_PREFIX, HEIR_PREFIX, SEARCH_PREFIX)
    conn.execute(
        f"DELETE FROM detail_fetches WHERE {' AND '.join(['id NOT GLOB ?'] * len(kept))} "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = detail_fetches.id)",
        tuple(starting(prefix) for prefix in kept),
    )


def _file_check(conn: sqlite3.Connection) -> IntegrityFinding:
    try:
        said = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        said = [str(exc)]
    lines = (line for answer in said for line in answer.splitlines())
    damage = [line for line in lines if line != "ok" and not line.startswith("***")]
    first = damage[0][:200] if damage else ""
    return IntegrityFinding(
        check="damaged database pages",
        count=len(damage),
        detail=(
            f'SQLite\'s quick_check says "{first}". Copy the file aside before anything else '
            "writes to it, then rebuild it with sqlite3 .recover"
        ),
    )


class Upkeep(Store):
    def purge(
        self,
        now: datetime,
        *,
        open_days: int = OPEN_RETENTION_DAYS,
        closed_days: int = CLOSED_RETENTION_DAYS,
    ) -> PurgeResult:
        stamp = to_text(now)
        cutoff = _rejections_cutoff(now)
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT id, source, first_seen FROM jobs WHERE {_EXPIRED}",
                _expiry(now, open_days, closed_days),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            reseated = _reseat_inverted_links(conn)
            successors = promote_orphaned_duplicates(conn, ids)
            followed = tuple(sorted(heirs_of(conn, set(ids), successors).items()))
            forgotten = conn.execute(
                "DELETE FROM quarantine WHERE last_seen < ?", (cutoff,)
            ).rowcount
            _forget_checks(conn, ids)
            conn.execute(
                "DELETE FROM detail_fetches WHERE id GLOB ? AND fetched_at < ?",
                (starting(HEIR_PREFIX), cutoff),
            )
            remember_heirs(conn, followed, stamp)
            conn.executemany(
                UPSERT_TOMBSTONE,
                [(row["id"], row["source"], row["first_seen"], stamp) for row in rows],
            )
            conn.execute(f"DELETE FROM jobs WHERE id IN {EACH}", (listed(ids),))
        return PurgeResult(
            purged=len(ids),
            tombstoned=len(ids),
            promoted=len(successors) + reseated,
            successors=followed,
            forgotten=forgotten,
        )

    def preview_purge(
        self,
        now: datetime,
        *,
        open_days: int = OPEN_RETENTION_DAYS,
        closed_days: int = CLOSED_RETENTION_DAYS,
    ) -> PurgeResult:
        expired = count(
            self._conn,
            f"SELECT COUNT(*) FROM jobs WHERE {_EXPIRED}",
            _expiry(now, open_days, closed_days),
        )
        stale = count(
            self._conn,
            "SELECT COUNT(*) FROM quarantine WHERE last_seen < ?",
            (_rejections_cutoff(now),),
        )
        return PurgeResult(purged=expired, tombstoned=expired, forgotten=stale)

    def tombstone_count(self) -> int:
        return count(self._conn, "SELECT COUNT(*) FROM tombstones")

    def count_duplicates(self) -> int:
        return count(self._conn, "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL")

    def screened_with(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def mark_screened(self, stamp: int) -> None:
        self._conn.execute(f"PRAGMA user_version = {int(stamp)}")

    def pending_heirs(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT id, source FROM detail_fetches WHERE id GLOB ? ORDER BY fetched_at, id",
            (starting(HEIR_PREFIX),),
        )
        return [(str(row["id"]).removeprefix(HEIR_PREFIX), str(row["source"])) for row in rows]

    def forget_heirs(self, formers: Sequence[str]) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM detail_fetches WHERE id IN (SELECT ? || value FROM json_each(?))",
                (HEIR_PREFIX, listed(formers)),
            )

    def close_orphan_boards(self, sources: Sequence[str], boards: Sequence[str]) -> int:
        if not sources or not boards:
            return 0
        with self._transaction() as conn:
            closed = conn.execute(
                f"UPDATE jobs SET status = ? WHERE source IN ({marks(len(sources))}) "
                "AND status = ? AND stage_board_of(id, source) <> source "
                f"AND stage_board_of(id, source) NOT IN {EACH}",
                (CLOSED, *sources, OPEN, listed(boards)),
            ).rowcount
        return int(closed or 0)

    def repair_integrity(self) -> list[IntegrityRepair]:
        with self._transaction() as conn:
            repairs = [
                IntegrityRepair(
                    check="dangling duplicate links",
                    repaired=conn.execute(
                        "UPDATE jobs SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL "
                        "AND duplicate_of NOT IN (SELECT id FROM jobs)"
                    ).rowcount,
                    detail="the copy shows on its own again",
                ),
                IntegrityRepair(
                    check="same-board merges",
                    repaired=conn.execute(
                        f"UPDATE jobs SET duplicate_of = NULL WHERE id IN ({_SAME_BOARD})"
                    ).rowcount,
                    detail="the two postings show separately again",
                ),
                IntegrityRepair(
                    check="duplicate chains",
                    repaired=self._repair_chains(),
                    detail="copies now point at the main posting",
                ),
                IntegrityRepair(
                    check="tombstoned rows re-ingested as new",
                    repaired=conn.execute(
                        "UPDATE jobs SET first_seen = ("
                        "SELECT t.first_seen FROM tombstones t WHERE t.id = jobs.id) "
                        "WHERE id IN (SELECT j.id FROM jobs j JOIN tombstones t ON t.id = j.id "
                        "WHERE j.first_seen > t.first_seen)"
                    ).rowcount,
                    detail="the posting has its original first-seen date again",
                ),
            ]
        return [repair for repair in repairs if repair.repaired]

    def _repair_chains(self) -> int:
        repaired = 0
        for _ in range(MAX_CHAIN_REPAIR_PASSES):
            changed = self._conn.execute(
                "UPDATE jobs SET duplicate_of = ("
                "SELECT b.duplicate_of FROM jobs b WHERE b.id = jobs.duplicate_of) "
                "WHERE duplicate_of IN (SELECT id FROM jobs WHERE duplicate_of IS NOT NULL)"
            ).rowcount
            repaired += changed
            if not changed:
                break
        return repaired

    def integrity_findings(self, *, whole_file: bool = False) -> list[IntegrityFinding]:
        findings = [_file_check(self._conn)] if whole_file else []
        findings.extend(
            IntegrityFinding(check=check, count=count(self._conn, sql), detail=detail)
            for check, sql, detail in _CHECKS
        )
        return findings
