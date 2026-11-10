from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

from stage.domain import (
    FIRST_READ_PREFIX,
    HttpValidator,
    RateState,
    SourceRunStats,
    SourceVisit,
    SyncOutcome,
    SyncRun,
    VolumePoint,
    board_of,
)
from stage.storage.rows import (
    RATE_STATE_SELECT,
    STAT_COLUMNS,
    VISIT_SELECT,
    rate_state_from,
    stats_from,
    stats_params,
    visit_from,
)
from stage.storage.sql import (
    EACH,
    Store,
    count,
    from_text,
    listed,
    marks,
    required_time,
    to_text,
)

_RUNS_BETWEEN = (
    "FROM sync_run_sources s JOIN sync_runs r ON r.id = s.run_id "
    "WHERE r.started_at >= ? AND r.started_at <= ?"
)


def _window(since: datetime, until: datetime | None) -> tuple[str, str]:
    return to_text(since), to_text(until) if until else "9999"


def _after(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class VisitRecords(Store):
    def load_validators(self, source: str) -> Mapping[str, HttpValidator]:
        rows = self._conn.execute(
            "SELECT url, etag, last_modified, fetched_at FROM http_cache WHERE source = ?",
            (source,),
        )
        return {
            str(row["url"]): HttpValidator(
                url=str(row["url"]),
                etag=row["etag"],
                last_modified=row["last_modified"],
                fetched_at=from_text(row["fetched_at"]),
            )
            for row in rows
        }

    def clear_validators(self, source: str | None = None) -> int:
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM http_cache WHERE ? IS NULL OR source = ?", (source, source)
            )
        return max(cursor.rowcount, 0)

    def cached_url_count(self) -> int:
        return count(self._conn, "SELECT COUNT(*) FROM http_cache")

    def load_rate_state(self) -> Mapping[str, RateState]:
        return {
            str(row["bucket"]): rate_state_from(row)
            for row in self._conn.execute(RATE_STATE_SELECT)
        }

    def clear_rate_state(self, bucket: str | None = None) -> int:
        with self._transaction() as conn:
            return int(
                conn.execute(
                    "UPDATE rate_state SET blocked_until = NULL, min_interval_override = NULL, "
                    "consecutive_failures = 0, last_failure_at = NULL, reason = '' "
                    "WHERE ? IS NULL OR bucket = ?",
                    (bucket, bucket),
                ).rowcount
            )

    def stale_members(self, source: str, before: datetime) -> list[SourceVisit]:
        rows = self._conn.execute(
            f"{VISIT_SELECT} WHERE source = ? AND (last_success_at IS NULL OR last_success_at < ?) "
            "ORDER BY last_success_at IS NOT NULL, last_success_at, board",
            (source, to_text(before)),
        )
        return [visit_from(row) for row in rows]

    def all_visits(self) -> list[SourceVisit]:
        rows = self._conn.execute(
            f"{VISIT_SELECT} ORDER BY last_success_at IS NOT NULL, last_success_at, source, board"
        )
        return [visit_from(row) for row in rows]

    def record_sync_run(self, run: SyncRun) -> None:
        with self._transaction() as conn:
            run_id = conn.execute(
                "INSERT INTO sync_runs (started_at, finished_at, outcome) VALUES (?, ?, ?)",
                (to_text(run.started_at), to_text(run.finished_at), run.outcome.value),
            ).lastrowid
            conn.executemany(
                f"INSERT INTO sync_run_sources (run_id, {', '.join(STAT_COLUMNS)}) "
                f"VALUES (?, {marks(len(STAT_COLUMNS))})",
                [stats_params(run_id, stats) for stats in run.sources],
            )

    def last_sync_at(self) -> datetime | None:
        return self._finished(0)

    def previous_sync_at(self) -> datetime | None:
        return self._finished(1)

    def _finished(self, skipped: int) -> datetime | None:
        row = self._conn.execute(
            "SELECT finished_at FROM sync_runs ORDER BY id DESC LIMIT 1 OFFSET ?", (skipped,)
        ).fetchone()
        return from_text(row["finished_at"]) if row else None

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def run_history(self, limit: int) -> list[SyncRun]:
        runs = self._conn.execute(
            "SELECT id, started_at, finished_at, outcome FROM sync_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        by_run: dict[int, list[SourceRunStats]] = {}
        for row in self._conn.execute(
            f"SELECT * FROM sync_run_sources WHERE run_id IN {EACH} ORDER BY run_id DESC, source",
            (listed(int(run["id"]) for run in runs),),
        ):
            by_run.setdefault(int(row["run_id"]), []).append(stats_from(row))
        return [
            SyncRun(
                started_at=required_time(run["started_at"], "started_at"),
                finished_at=required_time(run["finished_at"], "finished_at"),
                outcome=SyncOutcome(str(run["outcome"])),
                sources=tuple(by_run.get(int(run["id"]), ())),
            )
            for run in runs
        ]

    def volume_history(
        self, limit: int, since: datetime | None = None
    ) -> Mapping[str, list[VolumePoint]]:
        history: dict[str, list[VolumePoint]] = {}
        for row in self._conn.execute(
            "SELECT source, stored, deferred, blocked, fetched, requests, not_modified "
            "FROM sync_run_sources WHERE run_id IN "
            "(SELECT id FROM sync_runs WHERE started_at >= ? ORDER BY id DESC LIMIT ?) "
            "ORDER BY run_id DESC",
            (to_text(since) if since is not None else "", limit),
        ):
            history.setdefault(str(row["source"]), []).append(
                VolumePoint(
                    stored=int(row["stored"]),
                    deferred=int(row["deferred"]),
                    blocked=bool(row["blocked"]),
                    fetched=int(row["fetched"]),
                    requests=int(row["requests"]),
                    not_modified=int(row["not_modified"]),
                )
            )
        return history

    def requests_since(
        self, since: datetime, until: datetime | None = None
    ) -> tuple[dict[str, int], bool]:
        window = _window(since, until)
        rows = self._conn.execute(
            "SELECT s.source AS source, SUM(s.requests - s.not_modified) AS spent "
            f"{_RUNS_BETWEEN} GROUP BY s.source",
            window,
        )
        spent = {str(row["source"]): int(row["spent"] or 0) for row in rows}
        seen = self._conn.execute(
            "SELECT 1 FROM sync_runs WHERE started_at >= ? AND started_at <= ? LIMIT 1", window
        ).fetchone()
        return spent, seen is not None

    def pushback_since(self, since: datetime, until: datetime | None = None) -> frozenset[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT s.source AS source {_RUNS_BETWEEN} "
            "AND (s.retries > 0 OR s.tightenings > 0 OR s.blocked > 0)",
            _window(since, until),
        )
        return frozenset(str(row["source"]) for row in rows)

    def board_activity(self, month: datetime, week: datetime) -> dict[str, tuple[int, int]]:
        since = to_text(month)
        seen: dict[str, list[str]] = {}
        for row in self._conn.execute(
            "SELECT id, first_seen FROM jobs WHERE first_seen >= ? "
            "UNION ALL SELECT id, first_seen FROM tombstones WHERE first_seen >= ?",
            (since, since),
        ):
            if board := board_of(str(row["id"]), ""):
                seen.setdefault(board, []).append(str(row["first_seen"]))
        marked = {key: to_text(moment) for key, moment in self.first_reads().items()}
        firsts = self._first_seen_of_boards([board for board in seen if board not in marked])
        firsts.update({board: marked[board] for board in seen if board in marked})
        recent = to_text(week)
        activity: dict[str, tuple[int, int]] = {}
        for board, moments in seen.items():
            first = firsts.get(board)
            new = [moment for moment in moments if first is None or moment > first]
            if new:
                activity[board] = (len(new), sum(moment >= recent for moment in new))
        return activity

    def _first_seen_of_boards(self, boards: Sequence[str]) -> dict[str, str]:
        within = "WHERE id >= w.value || ':' AND id < w.value || ';'"
        rows = self._conn.execute(
            f"SELECT w.value AS board, (SELECT MIN(first_seen) FROM jobs {within}) AS listed, "
            f"(SELECT MIN(first_seen) FROM tombstones {within}) AS purged, "
            f"(SELECT MIN(first_seen) FROM quarantine {within}) AS rejected "
            "FROM json_each(?) w",
            (listed(boards),),
        )
        firsts: dict[str, str] = {}
        for row in rows:
            found = [moment for moment in (row["listed"], row["purged"], row["rejected"]) if moment]
            if found:
                firsts[str(row["board"])] = min(found)
        return firsts

    def earliest_copies(self, ids: Sequence[str]) -> dict[str, datetime]:
        rows = self._conn.execute(
            "SELECT duplicate_of, MIN(first_seen) AS first FROM jobs "
            f"WHERE duplicate_of IN {EACH} GROUP BY duplicate_of",
            (listed(ids),),
        )
        return {str(row["duplicate_of"]): required_time(row["first"], "first_seen") for row in rows}

    def first_reads(self) -> dict[str, datetime]:
        return {
            key: moment for key, (moment, _, _) in self._reads_starting(FIRST_READ_PREFIX).items()
        }

    def full_reads(self, source: str, prefix: str) -> dict[str, tuple[datetime, bool, bool]]:
        return self._reads_starting(prefix, source)

    def _reads_starting(
        self, prefix: str, source: str | None = None
    ) -> dict[str, tuple[datetime, bool, bool]]:
        rows = self._conn.execute(
            "SELECT id, fetched_at, resolved, failed FROM detail_fetches "
            "WHERE id >= ? AND id < ? AND (? IS NULL OR source = ?)",
            (prefix, _after(prefix), source, source),
        )
        return {
            str(row["id"]).removeprefix(prefix): (
                required_time(row["fetched_at"], "fetched_at"),
                bool(row["resolved"]),
                bool(row["failed"]),
            )
            for row in rows
        }

    def feed_tips(
        self, sources: Collection[str], since: datetime, limit: int
    ) -> dict[str, datetime]:
        if not sources or limit < 1:
            return {}
        feeds = listed(sources)
        firsts: dict[str, str] = {}
        for table in ("jobs", "tombstones", "quarantine"):
            for row in self._conn.execute(
                f"SELECT source, MIN(first_seen) AS first FROM {table} "
                f"WHERE source IN {EACH} GROUP BY source",
                (feeds,),
            ):
                source, first = str(row["source"]), str(row["first"])
                firsts[source] = min(first, firsts.get(source, first))
        latest: dict[str, str] = {}
        for row in self._conn.execute(
            f"SELECT source, company_fold, first_seen FROM jobs WHERE source IN {EACH} "
            "AND first_seen >= ? AND company_fold != ''",
            (feeds, to_text(since)),
        ):
            moment, folded = str(row["first_seen"]), str(row["company_fold"])
            if moment > firsts.get(str(row["source"]), "") and moment > latest.get(folded, ""):
                latest[folded] = moment
        newest = sorted(latest.items(), key=lambda item: item[1], reverse=True)[:limit]
        return {folded: required_time(moment, "first_seen") for folded, moment in newest}
