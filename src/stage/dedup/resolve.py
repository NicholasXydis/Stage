from bisect import bisect_right
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from stage.dedup.identity import (
    MatchKind,
    MatchResult,
    employer_acronym,
    employer_key,
    employer_name,
    job_opening,
    repost_key,
    would_merge,
)
from stage.domain import REPOST, UNKNOWN_TERM, Job, source_rank, terms_conflict
from stage.domain.text import is_workday_opening


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
    if url and url == right.apply_url_canonical and is_workday_opening(url):
        return True
    named = job_opening(left)
    return bool(named) and named == job_opening(right)


def _repost_of(job: Job) -> str:
    return repost_key(job.company, job.title_raw, job.location_raw)


def _reposts(jobs: Sequence[Job], keys: Mapping[str, str]) -> Iterator[list[str]]:
    grouped: dict[tuple[str, str], list[Job]] = {}
    for job in jobs:
        if keys[job.id]:
            grouped.setdefault((job.board_key, keys[job.id]), []).append(job)
    for members in grouped.values():
        terms = {job.term for job in members} - {UNKNOWN_TERM}
        if len(terms) <= 1:
            yield [job.id for job in members]
            continue
        for term in sorted(terms):
            yield [job.id for job in members if job.term == term]


def _terms_clash(left: Sequence[Job], right: Sequence[Job]) -> bool:
    one = {job.term for job in left} - {UNKNOWN_TERM}
    two = {job.term for job in right} - {UNKNOWN_TERM}
    return bool(one and two) and one.isdisjoint(two)


def _one_opening(left: Sequence[Job], right: Sequence[Job]) -> bool:
    return all(
        _same_opening(one, other)
        for one in left
        for other in right
        if one.board_key == other.board_key or one.source == other.source
    )


PROOFS_TO_LEARN = 2


def learned_employers(jobs: Sequence[Job], ambiguous: frozenset[str]) -> dict[str, str]:
    owners: dict[str, set[str]] = {}
    for job in jobs:
        url = job.apply_url_canonical if job.apply_url_canonical not in ambiguous else ""
        if proof := job_opening(job) or url:
            owners.setdefault(proof, set()).add(employer_name(job.company))
    proofs = Counter(tuple(sorted(names)) for names in owners.values() if len(names) == 2)
    parent: dict[str, str] = {}

    def find(name: str) -> str:
        while parent.setdefault(name, name) != name:
            name = parent[name]
        return name

    for (first, second), count in sorted(proofs.items()):
        if count >= PROOFS_TO_LEARN:
            parent[max(find(first), find(second))] = min(find(first), find(second))
    return {name: find(name) for name in list(parent)}


def _candidates(
    jobs: Sequence[Job], touched: set[str], learned: Mapping[str, str]
) -> list[set[int]]:
    boards = [job.board_key for job in jobs]
    fresh = [job.id in touched for job in jobs]
    blocks: dict[tuple[str, str], list[int]] = {}
    for index, job in enumerate(jobs):
        blocks.setdefault(("company", employer_key(job.company)), []).append(index)
        if (known := learned.get(employer_name(job.company))) is not None:
            blocks.setdefault(("learned", known), []).append(index)
        acronym = employer_acronym(job.company)
        if acronym and acronym != employer_key(job.company):
            blocks.setdefault(("company", acronym), []).append(index)
        if job.apply_url_canonical:
            blocks.setdefault(("url", job.apply_url_canonical), []).append(index)
        named = job_opening(job)
        if named:
            blocks.setdefault(("opening", named), []).append(index)
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
    learned = learned_employers(jobs, ambiguous)

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
        for index, following in enumerate(_candidates(jobs, touched, learned))
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
                matched[index, other] = would_merge(left, right, learned)
            match = matched[index, other]
            if not match or (match.kind is MatchKind.URL) is not by_link:
                continue
            if match.kind is MatchKind.URL and left.apply_url_canonical in ambiguous:
                continue
            if match.kind is not MatchKind.URL and _terms_clash(
                members[left_root], members[right_root]
            ):
                continue
            union(left.id, right.id)
            demoted = right.id if find(right.id) != right_root else left.id
            reason[demoted] = (match.kind.value, match.evidence)

    keys = {job.id: _repost_of(job) for job in jobs}
    for first, *rest in _reposts(jobs, keys):
        for twin in rest:
            if not _terms_clash(members[find(first)], members[find(twin)]):
                union(first, twin)

    by_id = {job.id: job for job in jobs}
    links: list[DuplicateLink] = []
    for job in jobs:
        canonical = find(job.id)
        if canonical == job.id:
            continue
        kind, evidence = reason.get(job.id, ("", ""))
        main = by_id[canonical]
        if (
            main.board_key == job.board_key
            and keys[job.id]
            and keys[job.id] == keys[main.id]
            and not terms_conflict(job.term, main.term)
        ):
            kind, evidence = REPOST, job.location_raw
        links.append(
            DuplicateLink(
                duplicate_id=job.id,
                canonical_id=canonical,
                kind=kind,
                evidence=evidence,
            )
        )
    return tuple(links)
