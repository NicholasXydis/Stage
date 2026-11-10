import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from functools import lru_cache

from stage.domain import Job, QuarantinedJob, RejectionReason, board_of, source_rank
from stage.storage.repository import Resolver, SourceBatch
from stage.storage.rows import OPEN, UPSERT_JOB, job_from, job_params, quarantined_from
from stage.storage.sql import EACH, fold_company, listed

COPY_PREFIX = "copy of "
_COPY_GLOB = f"{COPY_PREFIX}*"
_COPY_REASON = " rejected for "
_DEGREE = RejectionReason.OUT_OF_SCOPE_DEGREE.value


@lru_cache(maxsize=16384)
def canonical(url: str) -> str:
    from stage.normalize import canonical_apply_url

    return canonical_apply_url(url) if url else ""


@lru_cache(maxsize=16384)
def _posting_keys(company: str, title: str) -> tuple[str, frozenset[str]]:
    from stage.lexicon import fold

    return next(iter(fold_company(company).split()), ""), frozenset(fold(title).split())


def copied_from(phrase: str) -> tuple[str, str]:
    if not phrase.startswith(COPY_PREFIX):
        return "", ""
    origin, _, reason = phrase[len(COPY_PREFIX) :].partition(_COPY_REASON)
    return origin, reason


def _origin(entry: QuarantinedJob) -> tuple[str, str]:
    copied = copied_from(entry.matched_phrase)
    return copied if copied[0] else (entry.id, entry.matched_phrase)


def _inherits(copy: Job, main: QuarantinedJob) -> bool:
    return (
        not copy.description
        and copy.board_key != board_of(main.id, main.source)
        and source_rank(copy.source, copy.id) > source_rank(main.source, main.id)
    )


def _rejected_copy(job: Job, origin: str, reason: str) -> QuarantinedJob:
    return QuarantinedJob(
        id=job.id,
        source=job.source,
        company=job.company,
        title_raw=job.title_raw,
        reason=RejectionReason.OUT_OF_SCOPE_DEGREE,
        first_seen=job.first_seen,
        last_seen=job.last_seen,
        apply_url_raw=job.apply_url_raw,
        location_raw=job.location_raw,
        location=job.location,
        remote_scope=job.remote_scope,
        matched_phrase=f"{COPY_PREFIX}{origin}{_COPY_REASON}{reason}",
    )


def as_job(entry: QuarantinedJob, duplicate_of: str | None = None) -> Job:
    from stage.lexicon import fold

    return Job(
        id=entry.id,
        source=entry.source,
        company=entry.company,
        title_raw=entry.title_raw,
        title_normalized=fold(entry.title_raw),
        apply_url_raw=entry.apply_url_raw,
        apply_url_canonical=canonical(entry.apply_url_raw),
        description="",
        first_seen=entry.first_seen,
        last_seen=entry.last_seen,
        location_raw=entry.location_raw,
        location=entry.location,
        remote_scope=entry.remote_scope,
        duplicate_of=duplicate_of,
    )


def duplicate_candidates(conn: sqlite3.Connection, jobs: Iterable[Job]) -> list[Job]:
    firsts: set[str] = set()
    urls: set[str] = set()
    for job in jobs:
        firsts.add(next(iter(fold_company(job.company).split()), ""))
        urls.add(job.apply_url_canonical)
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ? AND (substr(company_fold, 1, "
        f"instr(company_fold || ' ', ' ') - 1) IN {EACH} OR apply_url_canonical IN {EACH})",
        (OPEN, listed(firsts - {""}), listed(urls - {""})),
    )
    return [job_from(row) for row in rows]


def _main_copy(resolve: Resolver, job: Job, candidates: Sequence[Job]) -> str:
    for link in resolve((job,), candidates) if candidates else ():
        if link.duplicate_id == job.id:
            return link.canonical_id
    return ""


class _Near[T]:
    def __init__(self, url_of: Callable[[T], str], keys_of: Callable[[T], tuple[str, str]]) -> None:
        self._url_of = url_of
        self._keys_of = keys_of
        self._by_url: dict[str, list[T]] = {}
        self._by_title: dict[tuple[str, frozenset[str]], list[T]] = {}
        self._by_root: dict[str, list[T]] = {}

    def add(self, item: T) -> None:
        if url := self._url_of(item):
            self._by_url.setdefault(url, []).append(item)
        keys = _posting_keys(*self._keys_of(item))
        self._by_title.setdefault(keys, []).append(item)
        self._by_root.setdefault(keys[0], []).append(item)

    def near(self, url: str, company: str, title: str) -> list[T]:
        return self._by_url.get(url, []) + self._by_title.get(_posting_keys(company, title), [])

    def of_employer(self, url: str, company: str) -> list[T]:
        return self._by_url.get(url, []) + self._by_root.get(_posting_keys(company, "")[0], [])


def copies_of_rejected(
    conn: sqlite3.Connection, batch: SourceBatch, jobs: tuple[Job, ...]
) -> tuple[tuple[Job, ...], tuple[QuarantinedJob, ...]]:
    bare = [job for job in jobs if not job.description]
    roots = _degree_roots(conn) if bare else {}
    if not roots:
        return jobs, ()
    marks = _copy_marks(conn, [job.id for job in bare])
    index = _Near[QuarantinedJob](
        lambda root: canonical(root.apply_url_raw), lambda root: (root.company, root.title_raw)
    )
    if batch.resolve_duplicates is not None:
        for root in roots.values():
            index.add(root)
    copies: list[QuarantinedJob] = []
    for job in bare:
        marked = roots.get(marks.get(job.id, ""))
        if marked is not None and _inherits(job, marked):
            copies.append(_rejected_copy(job, marked.id, marked.matched_phrase))
            continue
        if batch.resolve_duplicates is None:
            continue
        near = {
            root.id: root
            for root in index.of_employer(job.apply_url_canonical, job.company)
            if _inherits(job, root)
        }
        pool = [as_job(root) for root in near.values()]
        main = near.get(_main_copy(batch.resolve_duplicates, job, pool))
        if main is not None:
            copies.append(_rejected_copy(job, main.id, main.matched_phrase))
    moved = {entry.id for entry in copies}
    return tuple(job for job in jobs if job.id not in moved), tuple(copies)


def followers_of_rejected(
    conn: sqlite3.Connection, batch: SourceBatch, entries: Sequence[QuarantinedJob]
) -> tuple[QuarantinedJob, ...]:
    mains = {
        entry.id: entry for entry in entries if entry.reason is RejectionReason.OUT_OF_SCOPE_DEGREE
    }
    if not mains:
        return ()
    followers: dict[str, QuarantinedJob] = {}
    rows = conn.execute(f"SELECT * FROM jobs WHERE duplicate_of IN {EACH}", (listed(mains),))
    for row in rows:
        job = job_from(row)
        main = mains[str(row["duplicate_of"])]
        if job.id not in mains and _inherits(job, main):
            followers[job.id] = _rejected_copy(job, *_origin(main))
    resolve = batch.resolve_duplicates
    if resolve is None:
        return tuple(followers.values())
    probes = {
        main.id: as_job(main) for main in mains.values() if not copied_from(main.matched_phrase)[0]
    }
    index = _Near[Job](
        lambda job: job.apply_url_canonical, lambda job: (job.company, job.title_raw)
    )
    for candidate in duplicate_candidates(conn, probes.values()) if probes else ():
        if not (candidate.description or candidate.id in mains or candidate.id in followers):
            index.add(candidate)
    for main_id, probe in probes.items():
        main = mains[main_id]
        near = {
            candidate.id: candidate
            for candidate in index.near(probe.apply_url_canonical, probe.company, probe.title_raw)
            if candidate.id not in followers and _inherits(candidate, main)
        }
        for candidate in near.values():
            if _main_copy(resolve, candidate, (probe,)) == main_id:
                followers[candidate.id] = _rejected_copy(candidate, *_origin(main))
    return tuple(followers.values())


def restore_copies(conn: sqlite3.Connection, accepted: Collection[str]) -> None:
    if not accepted:
        return
    rows = conn.execute(
        "SELECT * FROM quarantine WHERE reason = ? AND matched_phrase GLOB ?", (_DEGREE, _COPY_GLOB)
    )
    back = [
        as_job(entry, origin)
        for entry in map(quarantined_from, rows)
        if (origin := copied_from(entry.matched_phrase)[0]) in accepted
    ]
    if not back:
        return
    conn.executemany(UPSERT_JOB, [job_params(job) for job in back])
    conn.execute(f"DELETE FROM quarantine WHERE id IN {EACH}", (listed(job.id for job in back),))


def _degree_roots(conn: sqlite3.Connection) -> dict[str, QuarantinedJob]:
    rows = conn.execute(
        "SELECT * FROM quarantine WHERE reason = ? AND matched_phrase NOT GLOB ?",
        (_DEGREE, _COPY_GLOB),
    )
    return {str(row["id"]): quarantined_from(row) for row in rows}


def _copy_marks(conn: sqlite3.Connection, job_ids: Sequence[str]) -> dict[str, str]:
    rows = conn.execute(
        f"SELECT id, matched_phrase FROM quarantine WHERE id IN {EACH} AND matched_phrase GLOB ?",
        (listed(job_ids), _COPY_GLOB),
    )
    return {str(row["id"]): copied_from(str(row["matched_phrase"]))[0] for row in rows}
