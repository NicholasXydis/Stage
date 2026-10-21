from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from stage.domain import ExportFormat, Job, JobStatus, dump
from stage.domain.tracker import (
    MARKERS,
    ApplicationStatus,
    PostingState,
    Snapshot,
    StatusChange,
    TrackedPosting,
    TrackerStats,
    moves_back,
    summarize,
)
from stage.storage import AsyncRepository
from stage.storage.tracker import TrackerStore, TrackerUnreadableError

FILE_NAME = "tracker.db"
FORMATS = (ExportFormat.CSV, ExportFormat.JSON, ExportFormat.MD)
LIST_PROBLEM = (
    "Your tracker could not be read, so hidden postings show and none are marked. "
    "Run stage tracker to see why."
)


class TrackerProblemError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TrackerView:
    statuses: Mapping[str, ApplicationStatus]
    problem: str = ""

    @property
    def hidden(self) -> frozenset[str]:
        return frozenset(
            key for key, status in self.statuses.items() if status is ApplicationStatus.HIDDEN
        )

    @property
    def tracked(self) -> frozenset[str]:
        return frozenset(self.statuses)

    def marks(self, jobs: Sequence[Job]) -> dict[str, str]:
        found: dict[str, str] = {}
        for job in jobs:
            status = self.statuses.get(job.id)
            if status in MARKERS:
                found[job.id] = MARKERS[status]
        return found


@dataclass(frozen=True, slots=True)
class Change:
    entry: TrackedPosting
    before: ApplicationStatus | None
    previous: TrackedPosting | None = None


@dataclass(frozen=True, slots=True)
class ChangeReport:
    changed: tuple[Change, ...] = ()
    unchanged: tuple[TrackedPosting, ...] = ()
    missing: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoveReport:
    removed: tuple[TrackedPosting, ...] = ()
    not_tracked: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrackerRow:
    entry: TrackedPosting
    state: PostingState


@dataclass(frozen=True, slots=True)
class TrackerListing:
    rows: tuple[TrackerRow, ...]
    hidden: int
    stats: TrackerStats
    last_sync_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Target:
    reference: str
    posting_id: str
    live: Job | None
    record: TrackedPosting | None


def tracker_path() -> Path:
    from stage.paths import data_dir

    return data_dir() / FILE_NAME


def _problem(exc: TrackerUnreadableError) -> TrackerProblemError:
    return TrackerProblemError(
        f"Stage could not use your tracker at {tracker_path()} ({exc}). It was left as it was."
    )


def _read_entries() -> list[TrackedPosting]:
    path = tracker_path()
    if not path.exists():
        return []
    try:
        with TrackerStore.connect(path) as store:
            return store.entries()
    except TrackerUnreadableError as exc:
        raise _problem(exc) from exc


async def tracker_view(repository: AsyncRepository) -> TrackerView:
    path = tracker_path()
    if not path.exists():
        return TrackerView({})
    try:
        with TrackerStore.connect(path) as store:
            raw = store.statuses()
    except TrackerUnreadableError:
        return TrackerView({}, LIST_PROBLEM)
    if not raw:
        return TrackerView({})
    canonical = await repository.canonical_ids(list(raw))
    latest: dict[str, tuple[datetime, ApplicationStatus]] = {}
    for posting_id, (status, changed) in raw.items():
        for key in {posting_id, canonical.get(posting_id, posting_id)}:
            if key not in latest or changed > latest[key][0]:
                latest[key] = (changed, status)
    return TrackerView({key: status for key, (_, status) in latest.items()})


async def record_status(
    repository: AsyncRepository,
    references: Sequence[str],
    status: ApplicationStatus | None,
    *,
    on: date | None = None,
    note: str | None = None,
    now: datetime | None = None,
    forward_only: bool = False,
) -> ChangeReport:
    moment = now or datetime.now(UTC)
    path = tracker_path()
    try:
        store = TrackerStore.connect(path) if path.exists() else None
        try:
            entries = {entry.posting_id: entry for entry in store.entries()} if store else {}
            targets = await _targets(repository, references, entries)
            plan = _plan(targets, status, on, note, moment, forward_only)
            if plan.writes:
                store = store or TrackerStore.connect(path)
                store.write(plan.writes, plan.history, plan.renamed or {})
            return plan.report
        finally:
            if store is not None:
                store.close()
    except TrackerUnreadableError as exc:
        raise _problem(exc) from exc


@dataclass(frozen=True, slots=True)
class _Plan:
    report: ChangeReport
    writes: tuple[TrackedPosting, ...] = ()
    history: tuple[tuple[str, StatusChange], ...] = ()
    renamed: Mapping[str, str] | None = None


def _plan(
    targets: Sequence[_Target],
    status: ApplicationStatus | None,
    on: date | None,
    note: str | None,
    moment: datetime,
    forward_only: bool,
) -> _Plan:
    missing = tuple(t.reference for t in targets if t.live is None and t.record is None)
    if missing:
        return _Plan(ChangeReport(missing=missing))
    if status is None:
        untracked = tuple(t.reference for t in targets if t.record is None)
        if untracked:
            return _Plan(ChangeReport(untracked=untracked))
    today = moment.astimezone().date()
    changed: list[Change] = []
    unchanged: list[TrackedPosting] = []
    writes: list[TrackedPosting] = []
    history: list[tuple[str, StatusChange]] = []
    renamed: dict[str, str] = {}
    for target in _distinct(targets):
        record = target.record
        wanted = status or (record.status if record else ApplicationStatus.SAVED)
        if forward_only and record is not None and moves_back(record.status, wanted):
            unchanged.append(record)
            continue
        moved = record is None or wanted is not record.status
        noted = note is not None and (record is None or note != record.note)
        if record is not None and not moved and on is None and not noted:
            unchanged.append(record)
            continue
        dated = moved or on is not None
        change = StatusChange(wanted, on or today, moment) if dated else None
        entry = TrackedPosting(
            posting_id=target.posting_id,
            status=wanted,
            on=change.on if change else (record.on if record else today),
            changed_at=moment,
            note=note if note is not None else (record.note if record else ""),
            snapshot=_snapshot(target.live) if target.live else _kept(record),
            history=(record.history if record else ()) + ((change,) if change else ()),
        )
        if record is not None and record.posting_id != target.posting_id:
            renamed[record.posting_id] = target.posting_id
        writes.append(entry)
        if change is not None:
            history.append((target.posting_id, change))
        changed.append(Change(entry, None if record is None else record.status, record))
    return _Plan(
        ChangeReport(changed=tuple(changed), unchanged=tuple(unchanged)),
        tuple(writes),
        tuple(history),
        renamed,
    )


def undo_changes(changes: Sequence[Change]) -> None:
    if not changes:
        return
    path = tracker_path()
    try:
        with TrackerStore.connect(path) as store:
            store.restore(
                [change.entry.posting_id for change in changes],
                [change.previous for change in changes if change.previous is not None],
            )
    except TrackerUnreadableError as exc:
        raise _problem(exc) from exc


async def remove_tracked(repository: AsyncRepository, references: Sequence[str]) -> RemoveReport:
    path = tracker_path()
    if not path.exists():
        missing, absent = await _unknown(repository, references)
        return RemoveReport(missing=missing) if missing else RemoveReport(not_tracked=absent)
    try:
        with TrackerStore.connect(path) as store:
            entries = {entry.posting_id: entry for entry in store.entries()}
            targets = await _targets(repository, references, entries)
            missing = tuple(t.reference for t in targets if t.live is None and t.record is None)
            if missing:
                return RemoveReport(missing=missing)
            canonical = await repository.canonical_ids(list(entries))
            removed: dict[str, TrackedPosting] = {}
            not_tracked: list[str] = []
            for target in targets:
                victims = [
                    entry
                    for entry in entries.values()
                    if target.posting_id in (entry.posting_id, canonical.get(entry.posting_id))
                    or entry.posting_id == target.reference
                ]
                if not victims:
                    not_tracked.append(target.reference)
                for victim in victims:
                    removed.setdefault(victim.posting_id, victim)
            store.remove(list(removed))
    except TrackerUnreadableError as exc:
        raise _problem(exc) from exc
    return RemoveReport(removed=tuple(removed.values()), not_tracked=tuple(not_tracked))


async def tracked_for(
    repository: AsyncRepository, references: Sequence[str]
) -> tuple[dict[str, TrackedPosting], str]:
    try:
        entries = {entry.posting_id: entry for entry in _read_entries()}
    except TrackerProblemError as exc:
        return {}, str(exc)
    if not entries:
        return {}, ""
    canonical = await repository.canonical_ids([*references, *entries])
    found: dict[str, TrackedPosting] = {}
    for reference in references:
        record = _record_for(canonical.get(reference, reference), entries, canonical)
        record = record or entries.get(reference)
        if record is not None:
            found[reference] = record
    return found, ""


async def tracker_listing(
    repository: AsyncRepository,
    status: ApplicationStatus | None = None,
    *,
    now: datetime | None = None,
) -> TrackerListing:
    entries = _read_entries()
    states = await repository.posting_states([entry.posting_id for entry in entries])
    shown = [
        entry
        for entry in entries
        if (entry.status is status if status else entry.status is not ApplicationStatus.HIDDEN)
    ]
    rows = tuple(TrackerRow(entry, _state(states.get(entry.posting_id))) for entry in shown)
    hidden = sum(entry.status is ApplicationStatus.HIDDEN for entry in entries)
    today = (now or datetime.now(UTC)).astimezone().date()
    return TrackerListing(
        rows=rows,
        hidden=hidden,
        stats=summarize(entries, today),
        last_sync_at=await repository.last_sync_at(),
    )


def export_tracker(
    rows: Sequence[TrackerRow],
    fmt: ExportFormat,
    *,
    destination: Path | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> Path:
    from stage.services.export import (
        ExportError,
        _replaced,
        _write_text,
        resolve_destination,
    )

    if fmt not in FORMATS:
        raise ExportError("the tracker exports as csv, json or md")
    moment = now or datetime.now(UTC)
    target = resolve_destination(destination, fmt, moment, force, stem="stage-tracker")
    with _replaced(target) as staged:
        if fmt is ExportFormat.CSV:
            _write_text(staged, tracker_csv(rows), encoding="utf-8-sig")
        elif fmt is ExportFormat.JSON:
            _write_text(staged, tracker_json(rows))
        else:
            _write_text(staged, tracker_markdown(rows, generated_at=moment))
    return target


_READERS: dict[str, Callable[[TrackerRow], str]] = {
    "status": lambda row: row.entry.status.value,
    "on": lambda row: row.entry.on.isoformat(),
    "company": lambda row: row.entry.snapshot.company,
    "title": lambda row: row.entry.snapshot.title,
    "location": lambda row: _place(row.entry.snapshot.location),
    "posting": lambda row: row.state.value,
    "note": lambda row: row.entry.note,
    "apply_url": lambda row: row.entry.snapshot.apply_url,
    "id": lambda row: row.entry.posting_id,
}
COLUMNS = tuple(_READERS)


def tracker_csv(rows: Sequence[TrackerRow]) -> str:
    import csv
    import io

    from stage.services.export import _cell, _guard

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([_guard(_cell(_READERS[name](row))) for name in COLUMNS])
    return buffer.getvalue()


def tracker_markdown(rows: Sequence[TrackerRow], *, generated_at: datetime) -> str:
    from stage.services.export import _markdown_cell

    lines = [
        f"# Stage tracker, {generated_at.astimezone(UTC):%Y-%m-%d %H:%M UTC}",
        "",
        f"{len(rows)} tracked posting(s).",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "| " + " | ".join("---" for _ in COLUMNS) + " |",
    ]
    for row in rows:
        cells = [_markdown_cell(_READERS[name](row)) for name in COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def tracker_json(rows: Sequence[TrackerRow]) -> str:
    return dump([tracked_payload(row.entry, row.state) for row in rows])


def tracked_payload(entry: TrackedPosting, state: PostingState | None = None) -> dict[str, object]:
    snapshot = entry.snapshot
    payload: dict[str, object] = {
        "id": entry.posting_id,
        "status": entry.status.value,
        "on": entry.on.isoformat(),
        "note": entry.note,
        "company": snapshot.company,
        "title": snapshot.title,
        "location": snapshot.location,
        "apply_url": snapshot.apply_url,
        "source": snapshot.source,
        "term": snapshot.term,
        "history": [
            {"status": change.status.value, "on": change.on.isoformat()} for change in entry.history
        ],
    }
    if state is not None:
        payload["posting"] = state.value
    return payload


def _place(raw: str) -> str:
    from stage.normalize.location import display_location

    return display_location(raw)


def _state(status: JobStatus | None) -> PostingState:
    if status is None:
        return PostingState.GONE
    return PostingState.OPEN if status is JobStatus.OPEN else PostingState.CLOSED


def _snapshot(job: Job) -> Snapshot:
    return Snapshot(
        company=job.company,
        title=job.title_raw,
        location=job.location_raw,
        apply_url=job.apply_url_raw,
        source=job.source,
        term=job.term,
    )


def _kept(record: TrackedPosting | None) -> Snapshot:
    if record is None:
        raise TrackerUnreadableError("a posting vanished while it was being tracked")
    return record.snapshot


def _distinct(targets: Sequence[_Target]) -> list[_Target]:
    seen: dict[str, _Target] = {}
    for target in targets:
        seen.setdefault(target.posting_id, target)
    return list(seen.values())


def _record_for(
    target: str, entries: Mapping[str, TrackedPosting], canonical: Mapping[str, str]
) -> TrackedPosting | None:
    if target in entries:
        return entries[target]
    mapped = [entry for entry in entries.values() if canonical.get(entry.posting_id) == target]
    return max(mapped, key=lambda entry: entry.changed_at) if mapped else None


async def _targets(
    repository: AsyncRepository,
    references: Sequence[str],
    entries: Mapping[str, TrackedPosting],
) -> list[_Target]:
    canonical = await repository.canonical_ids([*references, *entries])
    found: list[_Target] = []
    for reference in dict.fromkeys(references):
        live = await repository.get_job(reference)
        if live is not None and live.duplicate_of is not None:
            live = await repository.get_job(live.duplicate_of) or live
        posting_id = live.id if live is not None else canonical.get(reference, reference)
        record = _record_for(posting_id, entries, canonical) or entries.get(reference)
        found.append(_Target(reference, posting_id, live, record))
    return found


async def _unknown(
    repository: AsyncRepository, references: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missing: list[str] = []
    present: list[str] = []
    for reference in dict.fromkeys(references):
        if await repository.get_job(reference) is None:
            missing.append(reference)
        else:
            present.append(reference)
    return tuple(missing), tuple(present)


__all__ = [
    "COLUMNS",
    "FILE_NAME",
    "FORMATS",
    "LIST_PROBLEM",
    "Change",
    "ChangeReport",
    "RemoveReport",
    "TrackerListing",
    "TrackerProblemError",
    "TrackerRow",
    "TrackerView",
    "export_tracker",
    "record_status",
    "remove_tracked",
    "tracked_for",
    "tracked_payload",
    "tracker_csv",
    "tracker_json",
    "tracker_listing",
    "tracker_markdown",
    "tracker_path",
    "tracker_view",
    "undo_changes",
]
