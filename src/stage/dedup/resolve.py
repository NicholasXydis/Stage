from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from stage.dedup.identity import MatchKind, MatchResult, would_merge
from stage.domain import Job, source_rank
from stage.domain.text import is_workday_opening
from stage.lexicon import fold


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
        if is_workday_opening(job.apply_url_canonical):
            continue
        key = (job.source, job.apply_url_canonical)
        seen[key] = seen.get(key, 0) + 1
    return frozenset(url for (_, url), count in seen.items() if count > 1)


def _same_opening(left: Job, right: Job) -> bool:
    url = left.apply_url_canonical
    return bool(url) and url == right.apply_url_canonical and is_workday_opening(url)


def _one_opening(left: Sequence[Job], right: Sequence[Job]) -> bool:
    return all(
        _same_opening(one, other)
        for one in left
        for other in right
        if one.board_key == other.board_key or one.source == other.source
    )


def _candidates(jobs: Sequence[Job], touched: set[str]) -> list[set[int]]:
    boards = [job.board_key for job in jobs]
    fresh = [job.id in touched for job in jobs]
    blocks: dict[tuple[str, str], list[int]] = {}
    for index, job in enumerate(jobs):
        blocks.setdefault(("company", fold(job.company).partition(" ")[0]), []).append(index)
        if job.apply_url_canonical:
            blocks.setdefault(("url", job.apply_url_canonical), []).append(index)
    later: list[set[int]] = [set() for _ in jobs]
    for key, members in blocks.items():
        by_board: dict[str, list[int]] = {}
        for index in members:
            by_board.setdefault(boards[index], []).append(index)
        opening = key[0] == "url" and is_workday_opening(key[1])
        if len(by_board) < 2 and not opening:
            continue
        for index in members:
            for board, others in by_board.items():
                if board != boards[index] or opening:
                    later[index].update(
                        other
                        for other in others[bisect_right(others, index) :]
                        if fresh[index] or fresh[other]
                    )
    return later


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
    sources: dict[str, set[str]] = {job.id: {job.source} for job in jobs}
    members: dict[str, list[Job]] = {job.id: [job] for job in jobs}
    reason: dict[str, tuple[str, str]] = {}

    def find(job_id: str) -> str:
        while parent[job_id] != job_id:
            parent[job_id] = parent[parent[job_id]]
            job_id = parent[job_id]
        return job_id

    order = {job.id: index for index, job in enumerate(jobs)}

    def union(left_id: str, right_id: str) -> None:
        left_root, right_root = find(left_id), find(right_id)
        if left_root == right_root:
            return
        if order[right_root] < order[left_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        boards[left_root] |= boards[right_root]
        sources[left_root] |= sources[right_root]
        members[left_root] += members.pop(right_root)

    for job in jobs:
        if job.duplicate_of and job.duplicate_of in parent:
            union(job.id, job.duplicate_of)

    touched = {job.id for job in incoming}
    pairs = [
        (index, other)
        for index, following in enumerate(_candidates(jobs, touched))
        for other in sorted(following)
    ]
    matched: dict[tuple[int, int], MatchResult] = {}
    for by_link in (True, False):
        for index, other in pairs:
            left, right = jobs[index], jobs[other]
            left_root, right_root = find(left.id), find(right.id)
            if left_root == right_root:
                continue
            shared = (
                boards[left_root] & boards[right_root] or sources[left_root] & sources[right_root]
            )
            if shared and not _one_opening(members[left_root], members[right_root]):
                continue
            if (index, other) not in matched:
                matched[index, other] = would_merge(left, right)
            match = matched[index, other]
            if not match or (match.kind is MatchKind.URL) is not by_link:
                continue
            if match.kind is MatchKind.URL and left.apply_url_canonical in ambiguous:
                continue
            union(left.id, right.id)
            demoted = right.id if find(right.id) != right_root else left.id
            reason[demoted] = (match.kind.value, match.evidence)

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
