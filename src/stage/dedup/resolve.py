from collections.abc import Sequence
from dataclasses import dataclass

from stage.dedup.identity import MatchKind, would_merge
from stage.domain import Job, source_rank


@dataclass(frozen=True, slots=True)
class DuplicateLink:
    duplicate_id: str
    canonical_id: str
    kind: str
    evidence: str


def rank(job: Job) -> tuple[int, str]:
    return source_rank(job.source, job.id)


def _ambiguous_urls(jobs: Sequence[Job]) -> frozenset[str]:
    seen: dict[tuple[str, str], int] = {}
    for job in jobs:
        if not job.apply_url_canonical:
            continue
        key = (job.source, job.apply_url_canonical)
        seen[key] = seen.get(key, 0) + 1
    return frozenset(url for (_, url), count in seen.items() if count > 1)


def resolve_duplicates(
    incoming: Sequence[Job], existing: Sequence[Job]
) -> tuple[DuplicateLink, ...]:
    pool: dict[str, Job] = {job.id: job for job in existing}
    for job in incoming:
        pool[job.id] = job
    jobs = sorted(pool.values(), key=rank)
    ambiguous = _ambiguous_urls(jobs)

    parent: dict[str, str] = {job.id: job.id for job in jobs}
    boards: dict[str, set[str]] = {job.id: {job.board_key} for job in jobs}
    reason: dict[str, tuple[str, str]] = {}

    def find(job_id: str) -> str:
        while parent[job_id] != job_id:
            parent[job_id] = parent[parent[job_id]]
            job_id = parent[job_id]
        return job_id

    touched = {job.id for job in incoming}
    for index, left in enumerate(jobs):
        for right in jobs[index + 1 :]:
            if left.id not in touched and right.id not in touched:
                continue
            left_root, right_root = find(left.id), find(right.id)
            if left_root == right_root:
                continue
            if boards[left_root] & boards[right_root]:
                continue
            match = would_merge(left, right)
            if not match or (match.kind is MatchKind.URL and left.apply_url_canonical in ambiguous):
                continue
            parent[right_root] = left_root
            boards[left_root] |= boards[right_root]
            reason[right.id] = (match.kind.value, match.evidence)

    links: list[DuplicateLink] = []
    for job in jobs:
        canonical = find(job.id)
        if canonical == job.id:
            continue
        kind, evidence = reason.get(job.id, ("", ""))
        links.append(
            DuplicateLink(
                duplicate_id=job.id,
                canonical_id=canonical,
                kind=kind,
                evidence=evidence,
            )
        )
    return tuple(links)
