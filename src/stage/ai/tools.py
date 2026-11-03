import asyncio
import sqlite3
from collections.abc import AsyncIterator, Collection
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, override

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BeforeValidator, Field

from stage.ai.references import MARK, expand, short
from stage.ai.shape import (
    CAUTION,
    COMPACT_COLUMNS,
    answer,
    change_row,
    compact_row,
    day,
    error_text,
    freshness,
    kept_copy,
    next_cursor,
    past_the_last_page,
    posting_detail,
    posting_row,
    read_cursor,
    rejected_row,
    text,
    tracker_row,
)
from stage.domain import DEFAULT_WINDOW_DAYS, MAX_OPEN_AT_ONCE, Job, JobFilters, JobStatus
from stage.domain.preferences import Preferences
from stage.domain.text import counted, did_you_mean, first_line, plain_error
from stage.storage import AsyncRepository, DatabaseWriter, open_repository

if TYPE_CHECKING:
    from stage.domain import Company as Board
    from stage.domain.tracker import TrackedPosting
    from stage.services.tracker import Change

Place = Literal["montreal", "canada", "usa", "unknown"]
Role = Literal[
    "swe", "security", "data", "ml-ai", "quant", "infra", "embedded", "general-cs", "unknown"
]
Language = Literal["en", "fr", "bilingual", "unknown"]
Status = Literal["saved", "applied", "interviewing", "offer", "rejected", "withdrawn", "hidden"]
Reason = Literal[
    "out-of-scope-location",
    "not-an-internship",
    "out-of-scope-degree",
    "not-a-cs-role",
    "unknown-cs-role",
]
ChosenPlace = Literal["montreal", "canada", "usa"]
ChosenRole = Literal["swe", "security", "data", "ml-ai", "quant", "infra", "embedded", "general-cs"]
SEASONS = ("summer", "fall", "winter", "spring")
TERM = rf"^(({'|'.join(SEASONS)})-[0-9]{{4}}|unknown)$"
CHOSEN_TERM = rf"^({'|'.join(SEASONS)})-[0-9]{{4}}$"

PAGE = 25
MAX_PAGE = 100
COMPACT_PAGE = 250
MAX_LOOKUP = 10
MAX_CHANGE = 50
MAX_BOARDS = 100
SYNC_HISTORY = 5
MAX_UNDO = 200
SEARCHED = "stage/search"

NO_DATABASE = (
    "Stage has no postings on this computer yet. The user can run stage sync in a terminal "
    "to collect them."
)
NO_IDS = (
    "Stage has no posting with the ids in not_found. Ids look like greenhouse:acme:1234 and come "
    "from search_postings or get_tracker."
)
GONE = (
    "A posting marked gone is no longer stored by Stage. Its fields are the copy saved when the "
    "user tracked it."
)
SOURCES_NOTE = (
    "A blocked host or a failing board means Stage already backed off and will try again on a "
    "later sync. Do not suggest disabling boards. The user can run stage doctor in a terminal "
    "to see what each warning means."
)
QUARANTINE_NOTE = (
    "Stage rejected these postings, so they never appear in search_postings. A rejection that "
    "looks wrong is worth telling the user about."
)
NO_BROWSER = (
    "No web browser could be opened on this computer, so the links in not_opened were not "
    "opened. Give them to the user."
)
REFUSED_LINKS = (
    "Postings in refused have no plain http or https apply link, so Stage did not open them."
)
NO_LONGER_LISTED = (
    "The employers' own boards no longer list the postings in no_longer_listed, so the pages "
    "that opened for them may say the job is closed."
)
NO_UNDO = (
    "Stage has no change with that undo token. A token works once, and only until Stage "
    "restarts. Nothing was changed."
)
CHANGED_AGAIN = (
    "Postings in left_alone changed again after that change, so Stage left them as they are."
)
SYNC_NOTE = (
    "A sync takes several minutes and runs on its own. database_status shows its progress. "
    "Search again once it has finished."
)
READ_ALONE = (
    "Stage read the tracker without its database, so posting is null: whether each posting is "
    "still open is unknown."
)
CHANGED_ALONE = "Stage changed the tracker without its database."
KEPT_COPIES = "Each posting keeps the copy saved when it was first tracked."
TRACKED_ONLY = (
    "Without its database Stage can find only postings in the tracker, which holds no posting "
    "with id {ids}. Nothing was changed."
)
UNCOUNTED = (
    "Without its database Stage cannot count open postings, so open_postings is null for each "
    "employer."
)
UNKNOWN_SYNC = "unknown, because Stage could not read its database"
ExportType = Literal["csv", "md", "json", "pdf"]


def _lowered(value: object) -> object:
    return value.lower() if isinstance(value, str) else value


def _stripped(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _default(fallback: object) -> BeforeValidator:
    return BeforeValidator(lambda value: fallback if _blank(value) else value)


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


PostingId = Annotated[str, Field(min_length=1, max_length=2_000), BeforeValidator(_stripped)]
Employer = Annotated[str, Field(min_length=1, max_length=120), BeforeValidator(_stripped)]
Cursor = Annotated[
    str | None,
    Field(
        max_length=100,
        description="next_cursor from the previous page, sent with the same other arguments",
    ),
    _default(None),
]
Limit = Annotated[
    int,
    Field(ge=1, le=MAX_PAGE, description=f"How many to return, {PAGE} by default"),
    _default(PAGE),
]
Company = Annotated[
    str | None,
    Field(min_length=1, max_length=120, description="Employer name in any case, such as Shopify"),
    _default(None),
]
Words = Annotated[
    str,
    Field(
        max_length=200,
        description=(
            "Words to find in titles, employers and descriptions. Leave empty to list "
            "the newest postings instead."
        ),
    ),
    _default(""),
]
PlaceFilter = Annotated[
    Place | None,
    Field(description="montreal, canada (the rest of Canada), usa, or unknown"),
    _default(None),
]
RoleFilter = Annotated[Role | None, Field(description="The kind of work"), _default(None)]
TermFilter = Annotated[
    str | None,
    Field(pattern=TERM, description="A term such as summer-2027, or unknown"),
    BeforeValidator(_lowered),
    _default(None),
]
LanguageFilter = Annotated[
    Language | None,
    Field(description="The language the posting is written in"),
    _default(None),
]
Mine = Annotated[
    bool,
    Field(description="Apply the places, roles, terms and employers the user saved"),
    _default(False),
]
Untracked = Annotated[
    bool,
    Field(description="Leave out every posting the user saved, applied to or hid"),
    _default(False),
]
Days = Annotated[
    int | None,
    Field(
        ge=0,
        le=3650,
        description=(
            "Only postings first seen in the last N days, where 0 means any age. Without "
            f"words the default is {DEFAULT_WINDOW_DAYS}, and with words it is any age."
        ),
    ),
    _default(None),
]


class Tools:
    def __init__(self, database: Path) -> None:
        self.database = database
        self._undo: dict[str, tuple[Change, ...]] = {}
        self._tracking = asyncio.Lock()
        self._registry: tuple[tuple[object, ...], tuple[Board, ...], str] | None = None

    @asynccontextmanager
    async def _repository(self) -> AsyncIterator[AsyncRepository]:
        from stage.storage.migrations import SchemaVersionError

        if not self.database.exists():
            raise ToolError(NO_DATABASE)
        try:
            async with open_repository(self.database, read_only=True) as repository:
                yield repository
        except SchemaVersionError as exc:
            said = str(exc).rstrip(".")
            raise ToolError(f"Stage could not open its database. {said}.") from exc
        except sqlite3.Error as exc:
            raise ToolError(_unreadable(self.database, exc)) from exc

    @asynccontextmanager
    async def _tracker_repository(self) -> AsyncIterator[tuple[AsyncRepository, str]]:
        from stage.services.tracker import TrackerProblemError

        async with AsyncExitStack() as stack:
            problem = ""
            try:
                repository = await stack.enter_async_context(self._repository())
            except ToolError as exc:
                repository, problem = _NoPostings(self.database), str(exc)
            try:
                yield repository, problem
            except TrackerProblemError as exc:
                raise ToolError(str(exc)) from exc

    async def _boards(self) -> "tuple[tuple[Board, ...], str]":
        stamp = _registry_stamp()
        if self._registry is None or self._registry[0] != stamp:
            self._registry = (stamp, *await anyio.to_thread.run_sync(_load_boards))
        return self._registry[1], self._registry[2]

    async def search_postings(
        self,
        words: Words = "",
        location: PlaceFilter = None,
        role: RoleFilter = None,
        term: TermFilter = None,
        language: LanguageFilter = None,
        company: Company = None,
        mine: Mine = False,
        untracked: Untracked = False,
        new: Annotated[
            bool,
            Field(description="Only postings the last sync brought in"),
            _default(False),
        ] = False,
        days: Days = None,
        limit: Limit = PAGE,
        all_matches: Annotated[
            bool,
            Field(
                description=(
                    f"Return up to {COMPACT_PAGE} matches per page as compact rows instead of "
                    "full ones, ignoring limit"
                )
            ),
            _default(False),
        ] = False,
        cursor: Cursor = None,
    ) -> CallToolResult:
        from stage.services.query import company_match, count_hidden, list_jobs, search_jobs
        from stage.services.tracker import tracker_view

        arguments: dict[str, object] = {
            "words": words,
            "location": location,
            "role": role,
            "term": term,
            "language": language,
            "company": company,
            "mine": mine,
            "untracked": untracked,
            "new": new,
            "days": days,
            "all_matches": all_matches,
        }
        page = COMPACT_PAGE if all_matches else limit
        filters, described, notes = _chosen(location, role, term, language, company, mine)
        query = words if words.strip() else ""
        window = _window(query, days)
        now = datetime.now(UTC)
        async with self._repository() as repository:
            last_sync = await repository.last_sync_at()
            view = await tracker_view(repository)
            excluded = view.tracked if untracked else view.hidden
            version = [last_sync, sorted(excluded), described]
            offset = read_cursor(cursor, arguments, version)
            scoped = replace(filters, excluded_ids=excluded, limit=offset + page)
            if new:
                since = await repository.previous_sync_at()
                if since is not None:
                    scoped = replace(scoped, first_seen_after=since)
                elif last_sync is not None:
                    notes.append("Only one sync so far, so every posting counts as new.")
            if query:
                listing = await search_jobs(repository, query, scoped, window_days=window)
            else:
                listing = await list_jobs(repository, scoped, window_days=window)
            hidden = await count_hidden(
                repository, scoped, view.hidden, window_days=window, query=query
            )
            known: bool = True
            near: str | None = None
            if company and not listing.jobs:
                known, near = await company_match(repository, company)

        shown = listing.jobs[offset : offset + page]
        statuses = view.statuses
        following = offset + len(shown)
        payload: dict[str, object] = {
            "total": listing.total_matching,
            "offset": offset,
            "count": len(shown),
            "next_cursor": next_cursor(following, listing.total_matching, arguments, version),
            "window_days": None if scoped.first_seen_after is not None else window,
        }
        if described:
            payload["preferences"] = described
        if all_matches:
            payload["columns"] = list(COMPACT_COLUMNS)
            payload["rows"] = [compact_row(job, statuses.get(job.id)) for job in shown]
        else:
            payload["postings"] = [posting_row(job, statuses.get(job.id)) for job in shown]
        notes.extend(_searched_as(query, listing.terms))
        if not known:
            notes.append(_unknown_employer(company or "", near))
        if listing.no_location:
            notes.append(
                f"Left out {counted(listing.no_location, 'more posting')} with no location "
                "given. Send location='unknown' with mine=true for those."
            )
        if hidden:
            notes.append(
                f"Left out {counted(hidden, 'posting')} the user hid. get_tracker with "
                "status='hidden' shows them."
            )
        if all_matches and following < listing.total_matching:
            notes.append(f"The user can get every match with: {_command(arguments)}")
        if view.problem:
            payload["warning"] = view.problem
        payload["notes"] = notes
        payload["caution"] = CAUTION
        return answer({**payload, **freshness(last_sync, now)}, {SEARCHED: arguments})

    async def get_postings(
        self,
        ids: Annotated[
            list[PostingId],
            Field(
                min_length=1,
                max_length=MAX_LOOKUP,
                description=f"Up to {MAX_LOOKUP} posting ids from search_postings or get_tracker",
            ),
        ],
    ) -> CallToolResult:
        from stage.services.query import PostingDetail, get_posting
        from stage.services.tracker import tracked_for

        now = datetime.now(UTC)
        async with self._repository() as repository:
            wanted = await _resolved(repository, ids)
            details: list[PostingDetail | None] = [
                await get_posting(repository, ident) for ident in wanted
            ]
            tracked, problem = await tracked_for(repository, wanted)
            last_sync = await repository.last_sync_at()
        postings: list[dict[str, object]] = []
        missing: list[str] = []
        for ident, detail in zip(wanted, details, strict=True):
            entry = tracked.get(ident)
            if detail is not None:
                postings.append(posting_detail(detail, entry))
            elif entry is not None:
                postings.append(kept_copy(entry))
            else:
                missing.append(short(ident))
        notes: list[str] = []
        if missing:
            notes.append(NO_IDS)
        if any(found.get("posting") == "gone" for found in postings):
            notes.append(GONE)
        payload: dict[str, object] = {"postings": postings, "not_found": missing}
        if problem:
            payload["warning"] = problem
        payload["notes"] = notes
        payload["caution"] = CAUTION
        return answer({**payload, **freshness(last_sync, now)})

    async def database_status(self) -> CallToolResult:
        from stage.services.background import sync_progress
        from stage.services.health import statistics
        from stage.services.preferences import load_preferences

        now = datetime.now(UTC)
        async with self._repository() as repository:
            report = await statistics(repository, history=SYNC_HISTORY)
            open_now = await repository.count_jobs(JobFilters(limit=None))
            recent = await repository.count_jobs(
                JobFilters(limit=None, first_seen_after=now - timedelta(days=DEFAULT_WINDOW_DAYS))
            )
            since = await repository.previous_sync_at()
            fresh = (
                await repository.count_jobs(JobFilters(limit=None, first_seen_after=since))
                if since is not None
                else None
            )
            last_sync = await repository.last_sync_at()
        syncs = [
            {
                "finished_at": run.finished_at.astimezone(UTC).isoformat(),
                "outcome": run.outcome.value,
                "added": sum(source.added for source in run.sources),
                "closed": sum(source.closed for source in run.sources),
                "errors": sum(source.errors for source in run.sources),
            }
            for run in report.runs
        ]
        return answer(
            {
                "open_postings": open_now,
                "recent_postings": recent,
                "window_days": DEFAULT_WINDOW_DAYS,
                "new_since_previous_sync": fresh,
                "stored_postings": report.total_jobs,
                "duplicates_linked": report.duplicates,
                "quarantined": sum(report.quarantined.values()),
                "quarantined_by_reason": report.quarantined,
                "stored_by": report.composition,
                "recent_syncs": syncs,
                "background_sync": _background(sync_progress(self.database)),
                "preferences_saved": not load_preferences().preferences.is_empty,
                "notes": [
                    "stored_by counts every stored posting, open or closed, without duplicates."
                ],
                **freshness(last_sync, now),
            }
        )

    async def review_quarantine(
        self,
        reason: Annotated[
            Reason | None, Field(description="Only this rejection reason"), _default(None)
        ] = None,
        company: Company = None,
        limit: Limit = PAGE,
        cursor: Cursor = None,
    ) -> CallToolResult:
        from stage.domain import QuarantineFilters, RejectionReason
        from stage.services.quarantine import list_quarantined

        arguments: dict[str, object] = {"reason": reason, "company": company}
        now = datetime.now(UTC)
        async with self._repository() as repository:
            last_sync = await repository.last_sync_at()
            offset = read_cursor(cursor, arguments, last_sync)
            listing = await list_quarantined(
                repository,
                QuarantineFilters(
                    reason=RejectionReason(reason) if reason else None,
                    company=company,
                    limit=limit,
                    offset=offset,
                ),
            )
        shown = listing.entries
        following = offset + len(shown)
        return answer(
            {
                "total": listing.total_matching,
                "offset": offset,
                "count": len(shown),
                "next_cursor": next_cursor(following, listing.total_matching, arguments, last_sync),
                "quarantined_by_reason": listing.reason_counts,
                "postings": [rejected_row(entry) for entry in shown],
                "notes": [QUARANTINE_NOTE, *past_the_last_page(following, listing.total_matching)],
                "caution": CAUTION,
                **freshness(last_sync, now),
            }
        )

    async def check_sources(
        self,
        include_boards: Annotated[
            bool,
            Field(description=f"Also list failing and stale boards, up to {MAX_BOARDS}"),
            _default(False),
        ] = False,
    ) -> CallToolResult:
        from stage.services.health import doctor

        now = datetime.now(UTC)
        registered, unread = await self._boards()
        async with self._repository() as repository:
            report = await doctor(repository, now=now, companies=registered)
        sources = [
            {
                "source": source.source,
                "open_rows": source.stored,
                "volume": source.volume.verdict.value,
                "errors": source.errors,
                "deferred": source.deferred,
                "blocked": source.blocked,
                "failing_boards": len(source.failing_boards),
                "stale_boards": len(source.stale_boards),
            }
            for source in report.sources
        ]
        payload: dict[str, object] = {
            "healthy": report.is_healthy,
            "never_synced": report.never_synced,
            "integrity_problems": [
                {"check": finding.check, "count": finding.count}
                for finding in report.integrity_problems
            ],
            "volume_alerts": [
                {"source": source.source, "detail": text(source.volume.detail, 200)}
                for source in report.volume_alerts
            ],
            "blocked_hosts": [
                {
                    "host": state.bucket,
                    "until": state.blocked_until.astimezone(UTC).isoformat()
                    if state.blocked_until
                    else None,
                    "reason": error_text(state.reason, 200),
                }
                for state in report.blocks
            ],
            "failing_boards": len(report.failing_boards),
            "stale_boards": len(report.stale_boards),
            "stale_after_days": report.stale_after_days,
            "sources": sources,
        }
        if include_boards:
            boards = [*report.failing_boards, *report.stale_boards]
            payload["boards"] = [
                {
                    "source": board.source,
                    "board": text(board.board, 120),
                    "label": text(board.label, 120),
                    "state": board.state.value,
                    "failures": board.consecutive_failures,
                    "last_success": day(board.last_success_at),
                    "last_error": error_text(board.last_error, 200),
                }
                for board in boards[:MAX_BOARDS]
            ]
            payload["boards_not_shown"] = max(0, len(boards) - MAX_BOARDS)
        payload["notes"] = [SOURCES_NOTE, unread] if unread else [SOURCES_NOTE]
        payload["caution"] = CAUTION
        return answer({**payload, **freshness(report.last_sync_at, now)})

    async def track_postings(
        self,
        ids: Annotated[
            list[PostingId],
            Field(min_length=1, max_length=MAX_CHANGE, description="Posting ids to track"),
        ],
        status: Annotated[
            Status | None,
            Field(
                description=(
                    "The new status. saved and applied never move a posting back, so an applied "
                    "posting stays applied. Set override to move it back on purpose."
                )
            ),
        ] = None,
        note: Annotated[
            str | None,
            Field(max_length=1_000, description="A short note, or an empty string to clear it"),
        ] = None,
        on: Annotated[
            str | None,
            Field(
                pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                description="The day it happened, like 2026-10-20 (today by default)",
            ),
            _default(None),
        ] = None,
        override: Annotated[
            bool,
            Field(description="Set the status exactly, even if that moves it back"),
            _default(False),
        ] = False,
    ) -> CallToolResult:
        from stage.domain.tracker import ApplicationStatus, TrackerError, parse_day, parse_note
        from stage.services.tracker import record_status

        if status is None and note is None and on is None:
            raise ToolError("Send a status, a note or a date. Nothing was changed.")
        chosen = ApplicationStatus(status) if status else None
        try:
            when = parse_day(on, datetime.now(UTC).astimezone().date()) if on is not None else None
            written = parse_note(note) if note is not None else None
        except TrackerError as exc:
            raise ToolError(f"{exc} Nothing was changed.") from exc
        forward_only = (
            chosen in (ApplicationStatus.SAVED, ApplicationStatus.APPLIED) and not override
        )
        now = datetime.now(UTC)
        async with self._tracking, self._tracker_repository() as (repository, problem):
            wanted = await _resolved(repository, ids)
            report = await record_status(
                repository, wanted, chosen, on=when, note=written, forward_only=forward_only
            )
            last_sync = await repository.last_sync_at()
        if report.missing:
            raise ToolError(_not_found(report.missing, problem))
        if report.untracked:
            one = len(report.untracked) == 1
            raise ToolError(
                f"{_listed(report.untracked)} {'is' if one else 'are'} not tracked yet, so there "
                f"is no status to keep. Send status='saved' to start tracking "
                f"{'it' if one else 'them'}. Nothing was changed."
            )
        notes = [f"{problem} {CHANGED_ALONE} {KEPT_COPIES}"] if problem else []
        if forward_only and report.unchanged:
            notes.append(
                "saved and applied never move a posting back. Send override=true to set the "
                "status exactly."
            )
        payload: dict[str, object] = {
            "changed": [
                {
                    **change_row(change.entry),
                    "before": change.before.value if change.before else None,
                }
                for change in report.changed
            ],
            "unchanged": [change_row(entry) for entry in report.unchanged],
        }
        if report.changed:
            payload["undo"] = self._remember(report.changed)
        payload["notes"] = notes
        return answer({**payload, **_synced(last_sync, now, problem)})

    def _remember(self, changes: "tuple[Change, ...]") -> str:
        import secrets

        token = secrets.token_urlsafe(9)
        self._undo[token] = changes
        while len(self._undo) > MAX_UNDO:
            self._undo.pop(next(iter(self._undo)))
        return token

    async def undo_tracking(
        self,
        undo: Annotated[
            str,
            Field(
                min_length=1,
                max_length=40,
                description="The undo token from a track_postings answer",
            ),
        ],
    ) -> CallToolResult:
        from stage.services.tracker import restore_unless_changed

        token = undo.strip()
        async with self._tracking:
            changes = self._undo.pop(token, None)
            if changes is None:
                raise ToolError(NO_UNDO)
            undone = False
            try:
                async with self._tracker_repository() as (repository, problem):
                    last_sync = await repository.last_sync_at()
                    with anyio.CancelScope(shield=True):
                        restored, kept = await anyio.to_thread.run_sync(
                            restore_unless_changed, changes
                        )
                        undone = True
            finally:
                if not undone:
                    self._undo[token] = changes
        now = datetime.now(UTC)
        notes = [f"{problem} {CHANGED_ALONE}"] if problem else []
        return answer(
            {
                "restored": [_as_now(change.previous, change) for change in restored],
                "left_alone": [_as_now(current, change) for change, current in kept],
                "notes": [*notes, CHANGED_AGAIN] if kept else notes,
                **_synced(last_sync, now, problem),
            }
        )

    async def untrack_postings(
        self,
        ids: Annotated[
            list[PostingId],
            Field(
                min_length=1,
                max_length=MAX_CHANGE,
                description="Posting ids to stop tracking. Hidden ones show in searches again",
            ),
        ],
    ) -> CallToolResult:
        from stage.services.tracker import remove_tracked

        now = datetime.now(UTC)
        async with self._tracking, self._tracker_repository() as (repository, problem):
            wanted = await _resolved(repository, ids)
            report = await remove_tracked(repository, wanted)
            last_sync = await repository.last_sync_at()
        if report.missing:
            raise ToolError(_not_found(report.missing, problem))
        return answer(
            {
                "removed": [change_row(entry) for entry in report.removed],
                "not_tracked": [short(ident) for ident in report.not_tracked],
                "notes": [f"{problem} {CHANGED_ALONE}"] if problem else [],
                **_synced(last_sync, now, problem),
            }
        )

    async def get_tracker(
        self,
        status: Annotated[
            Status | None,
            Field(description="Only this status. Hidden postings show only when asked for"),
            _default(None),
        ] = None,
        limit: Limit = PAGE,
        cursor: Cursor = None,
    ) -> CallToolResult:
        from stage.domain.tracker import ApplicationStatus
        from stage.services.tracker import stats_payload, tracker_listing

        arguments: dict[str, object] = {"status": status}
        now = datetime.now(UTC)
        async with self._tracker_repository() as (repository, problem):
            listing = await tracker_listing(
                repository, ApplicationStatus(status) if status else None
            )
        version = [
            listing.last_sync_at,
            [(row.entry.posting_id, row.entry.changed_at) for row in listing.rows],
        ]
        offset = read_cursor(cursor, arguments, version)
        shown = [tracker_row(row) for row in listing.rows[offset : offset + limit]]
        if problem:
            shown = [{**row, "posting": None} for row in shown]
        notes = [GONE] if any(row["posting"] == "gone" for row in shown) else []
        following = offset + len(shown)
        return answer(
            {
                "total": len(listing.rows),
                "offset": offset,
                "count": len(shown),
                "next_cursor": next_cursor(following, len(listing.rows), arguments, version),
                "hidden": listing.hidden,
                "summary": stats_payload(listing.stats),
                "postings": shown,
                "notes": [*notes, f"{problem} {READ_ALONE}"] if problem else notes,
                "caution": CAUTION,
                **_synced(listing.last_sync_at, now, problem),
            }
        )

    async def get_preferences(self) -> CallToolResult:
        from stage.services.preferences import load_preferences

        loaded = load_preferences()
        counts, synced, problem = await self._counts(loaded.preferences)
        return answer(
            {
                **_preferences(loaded.preferences, counts),
                "notes": [*loaded.problems, *_uncounted(loaded.preferences, problem)],
                **synced,
            }
        )

    async def set_preferences(
        self,
        locations: Annotated[
            list[ChosenPlace] | None,
            Field(max_length=3, description="Places to keep (canada includes montreal)"),
        ] = None,
        roles: Annotated[
            list[ChosenRole] | None, Field(max_length=8, description="Kinds of work to keep")
        ] = None,
        terms: Annotated[
            list[Annotated[str, Field(pattern=CHOSEN_TERM), BeforeValidator(_lowered)]] | None,
            Field(max_length=12, description="Terms such as summer-2027"),
        ] = None,
        follow: Annotated[
            list[Employer] | None,
            Field(max_length=50, description="Employers to include from any place"),
        ] = None,
        avoid: Annotated[
            list[Employer] | None,
            Field(max_length=50, description="Employers to leave out"),
        ] = None,
        about: Annotated[
            str | None,
            Field(
                max_length=2_000,
                description="A short note about the user, kept on this computer",
            ),
        ] = None,
        clear: Annotated[
            bool,
            Field(description="Delete every saved preference. Send it alone"),
            _default(False),
        ] = False,
    ) -> CallToolResult:
        from stage.domain.preferences import PreferenceError
        from stage.services.preferences import (
            LoadedPreferences,
            clear_preferences,
            load_preferences,
            preferences_lock,
            preferences_path,
            revise_preferences,
            save_preferences,
            stored_companies,
            unseen_companies,
        )

        fields = (locations, roles, terms, follow, avoid, about)
        changing = any(value is not None for value in fields)
        if clear and changing:
            raise ToolError("clear deletes every saved preference, so send it alone.")
        if clear:
            try:
                removed = await anyio.to_thread.run_sync(clear_preferences)
            except OSError as exc:
                raise ToolError(_could_not("delete", preferences_path(), exc)) from exc
            _, synced, problem = await self._counts(Preferences())
            return answer(
                {
                    **_preferences(Preferences(), {}),
                    "cleared": removed,
                    "notes": _uncounted(Preferences(), problem),
                    **synced,
                }
            )
        if not changing:
            raise ToolError("Send at least one field to change. get_preferences shows them.")
        known: dict[str, str] = {}
        if follow is not None or avoid is not None:
            try:
                async with self._repository() as repository:
                    known = await stored_companies(repository)
            except ToolError:
                known = {}

        def save() -> tuple[LoadedPreferences, Preferences]:
            with preferences_lock():
                loaded = load_preferences()
                revised = revise_preferences(
                    loaded.preferences,
                    known,
                    locations=locations,
                    roles=roles,
                    terms=terms,
                    follow=follow,
                    avoid=avoid,
                    about=about,
                )
                save_preferences(revised)
            return loaded, revised

        try:
            loaded, revised = await anyio.to_thread.run_sync(save)
        except PreferenceError as exc:
            raise ToolError(f"{exc} Nothing was saved.") from exc
        except OSError as exc:
            raise ToolError(_could_not("save", preferences_path(), exc)) from exc
        counts, synced, problem = await self._counts(revised)
        replaced = (
            ["The old preferences file could not be read, so these choices replaced it."]
            if loaded.damaged
            else list(loaded.problems)
        )
        return answer(
            {
                **_preferences(revised, counts),
                "notes": [
                    *replaced,
                    *unseen_companies(revised, known),
                    *_uncounted(revised, problem),
                ],
                **synced,
            }
        )

    async def open_postings(
        self,
        ids: Annotated[
            list[PostingId],
            Field(
                min_length=1,
                max_length=MAX_OPEN_AT_ONCE,
                description=f"Up to {MAX_OPEN_AT_ONCE} posting ids whose apply pages to open",
            ),
        ],
    ) -> CallToolResult:
        from stage.domain import web_url
        from stage.services.query import get_posting
        from stage.services.tracker import tracked_for

        now = datetime.now(UTC)
        async with self._repository() as repository:
            wanted = await _resolved(repository, ids)
            details = [await get_posting(repository, ident) for ident in wanted]
            tracked, _ = await tracked_for(repository, wanted)
            last_sync = await repository.last_sync_at()
        links: list[tuple[dict[str, object], str]] = []
        sent: list[str] = []
        refused: list[dict[str, object]] = []
        missing: list[str] = []
        for ident, detail in zip(wanted, details, strict=True):
            entry = tracked.get(ident)
            if detail is not None:
                company, title, raw = (
                    detail.job.company,
                    detail.job.title_raw,
                    detail.job.apply_url_raw,
                )
            elif entry is not None:
                snapshot = entry.snapshot
                company, title, raw = snapshot.company, snapshot.title, snapshot.apply_url
            else:
                missing.append(short(ident))
                continue
            row: dict[str, object] = {
                "id": short(ident),
                "company": text(company, 120),
                "title": text(title, 200),
            }
            url = web_url(raw)
            if url is None:
                refused.append(row)
            else:
                links.append((row, url))
                sent.append(ident)
        launched = await anyio.to_thread.run_sync(_launch_all, [url for _, url in links])
        results = list(zip(links, launched, strict=True))
        opened = [{**row, "url": url} for (row, url), done in results if done]
        stranded = [{**row, "url": url} for (row, url), done in results if not done]
        unlisted = await _no_longer_listed(
            self.database, [ident for ident, done in zip(sent, launched, strict=True) if done]
        )
        notes: list[str] = []
        if missing:
            notes.append(NO_IDS)
        if refused:
            notes.append(REFUSED_LINKS)
        if stranded:
            notes.append(NO_BROWSER)
        if unlisted:
            notes.append(NO_LONGER_LISTED)
        return answer(
            {
                "opened": opened,
                "not_opened": stranded,
                "refused": refused,
                "not_found": missing,
                "no_longer_listed": unlisted,
                "notes": notes,
                "caution": CAUTION,
                **freshness(last_sync, now),
            }
        )

    async def export_postings(
        self,
        file_type: Annotated[
            ExportType,
            Field(
                description="csv for spreadsheets, md for notes, json for programs, pdf to print"
            ),
            _default("csv"),
        ] = "csv",
        words: Words = "",
        location: PlaceFilter = None,
        role: RoleFilter = None,
        term: TermFilter = None,
        language: LanguageFilter = None,
        company: Company = None,
        mine: Mine = False,
        untracked: Untracked = False,
        days: Days = None,
    ) -> CallToolResult:
        from stage.domain import ExportFormat
        from stage.services.export import (
            ExportError,
            downloads_root,
            export_jobs,
            free_destination,
        )
        from stage.services.tracker import tracker_view

        fmt = ExportFormat(file_type)
        root = downloads_root()
        unusable = _no_folder(root)
        if unusable:
            raise ToolError(unusable)
        filters, described, notes = _chosen(location, role, term, language, company, mine)
        query = words if words.strip() else ""
        window = _window(query, days)
        now = datetime.now(UTC)
        async with self._repository() as repository:
            view = await tracker_view(repository)
            scoped = replace(
                filters, excluded_ids=view.tracked if untracked else view.hidden, limit=None
            )
            try:
                result = await export_jobs(
                    repository,
                    scoped,
                    fmt=fmt,
                    destination=free_destination(root, fmt, now),
                    claimed=True,
                    window_days=window,
                    now=now,
                    query=query,
                )
            except ExportError as exc:
                raise ToolError(f"Stage could not export: {exc}. Nothing was exported.") from exc
            try:
                last_sync = await repository.last_sync_at()
            except BaseException:
                result.path.unlink(missing_ok=True)
                raise
        if not result.count:
            notes.append(
                "Nothing matched, so the file holds an empty list."
                if fmt is ExportFormat.JSON
                else "Nothing matched, so the file holds column headers only."
            )
        notes.extend(text(note, 200) for note in result.notes)
        payload: dict[str, object] = {
            "path": str(result.path),
            "file_type": fmt.value,
            "count": result.count,
            "total": result.total_matching,
            "window_days": window,
        }
        if described:
            payload["preferences"] = described
        if view.problem:
            payload["warning"] = view.problem
        payload["notes"] = notes
        return answer({**payload, **freshness(last_sync, now)})

    async def start_sync(self) -> CallToolResult:
        from stage.services.background import RUNNING, start_sync

        launched = await anyio.to_thread.run_sync(start_sync, self.database)
        progress = _background(dict(launched.progress)) if launched.progress else None
        state = progress["state"] if progress is not None else None
        if not launched.started and state not in (*RUNNING, "blocked"):
            raise ToolError(f"{text(launched.detail, 400)} No sync is running.")
        _, synced, _ = await self._counts(Preferences())
        return answer(
            {
                "started": launched.started,
                "detail": text(launched.detail, 400),
                "background_sync": progress,
                "notes": [SYNC_NOTE] if launched.started else [],
                **synced,
            }
        )

    async def _counts(
        self, preferences: Preferences
    ) -> tuple[dict[str, int] | None, dict[str, object], str]:
        wanted = {company.folded for company in (*preferences.follow, *preferences.avoid)}
        now = datetime.now(UTC)
        try:
            async with self._repository() as repository:
                counts = await repository.open_counts_by_company(wanted)
                last_sync = await repository.last_sync_at()
        except ToolError as exc:
            return None, _synced(None, now, str(exc)), str(exc)
        return counts, freshness(last_sync, now), ""


class _NoPostings(AsyncRepository):
    def __init__(self, database: Path) -> None:
        super().__init__(DatabaseWriter(database, read_only=True))

    @override
    async def ids_with_prefix(self, prefix: str) -> list[str]:
        return []

    @override
    async def canonical_ids(self, job_ids: Collection[str]) -> dict[str, str]:
        return {}

    @override
    async def posting_states(self, job_ids: Collection[str]) -> dict[str, JobStatus]:
        return {}

    @override
    async def get_job(self, job_id: str) -> Job | None:
        return None

    @override
    async def last_sync_at(self) -> datetime | None:
        return None


def _synced(last_sync: datetime | None, now: datetime, problem: str) -> dict[str, object]:
    if problem and problem != NO_DATABASE:
        return {"last_sync_at": None, "last_sync": UNKNOWN_SYNC}
    return freshness(last_sync, now)


def _not_found(missing: tuple[str, ...], problem: str) -> str:
    if problem:
        return f"{problem} {TRACKED_ONLY.format(ids=_listed(missing))}"
    return f"No posting with id {_listed(missing)}. Nothing was changed."


def _uncounted(preferences: Preferences, problem: str) -> list[str]:
    if not problem:
        return []
    counted = preferences.follow or preferences.avoid
    return [f"{problem} {UNCOUNTED}" if counted else problem]


def _searched_as(query: str, terms: tuple[str, ...]) -> list[str]:
    from stage.services.query import searched_as

    note = searched_as(query, terms)
    return [note] if note else []


async def _resolved(repository: AsyncRepository, ids: list[str]) -> list[str]:
    from stage.services.tracker import tracker_view

    wanted = list(dict.fromkeys(ident.strip() for ident in ids))
    if not any(f":{MARK}" in ident for ident in wanted):
        return wanted
    view = await tracker_view(repository)
    return list(dict.fromkeys(await expand(repository, wanted, view.statuses.keys())))


def _chosen(
    location: str | None,
    role: str | None,
    term: str | None,
    language: str | None,
    company: str | None,
    mine: bool,
) -> tuple[JobFilters, str, list[str]]:
    from stage.domain import Language as Written
    from stage.domain import LocationBucket, RoleCategory
    from stage.services.preferences import load_preferences

    filters = JobFilters(
        location=LocationBucket(location) if location else None,
        term=term,
        role=RoleCategory(role) if role else None,
        language=Written(language) if language else None,
        company=company,
        limit=None,
    )
    if not mine:
        return filters, "", []
    loaded = load_preferences()
    if loaded.damaged:
        raise ToolError(
            "The user's saved preferences could not be read. Save them again with "
            "set_preferences, or search without mine."
        )
    preferences = loaded.preferences
    if preferences.is_empty:
        raise ToolError(
            "No preferences are saved yet. Ask the user which places, roles and terms they "
            "want, save them with set_preferences, then search again."
        )
    if not preferences.narrows:
        held = "followed employers" if preferences.follow else "a note about the user"
        raise ToolError(
            f"The saved preferences only hold {held}, so mine would match everything. Ask "
            "which places, roles or terms they want and save them with set_preferences."
        )
    narrowed = preferences.apply(filters)
    return narrowed, preferences.describe(narrowed), list(loaded.problems)


def _as_now(entry: "TrackedPosting | None", change: "Change") -> dict[str, object]:
    if entry is not None:
        return change_row(entry)
    return {**change_row(change.entry), "status": None, "on": None}


def _unknown_employer(company: str, near: str | None) -> str:
    said = f'Stage stores no posting from an employer named "{text(company, 60)}".'
    return f"{said} {did_you_mean(text(near, 60))}" if near else said


def _registry_stamp() -> tuple[object, ...]:
    from stage.paths import boards_path, registry_path

    source = registry_path()
    files = sorted(source.glob("*.yaml")) if source.is_dir() else [source]
    return (datetime.now(UTC).date(), *(_stamp(path) for path in (*files, boards_path())))


def _stamp(path: Path) -> tuple[object, ...]:
    try:
        found = path.stat()
    except OSError:
        return (str(path),)
    return str(path), found.st_mtime_ns, found.st_size, found.st_ino


def _load_boards() -> "tuple[tuple[Board, ...], str]":
    from stage.companies import RegistryError, load_boards

    try:
        boards, personal = load_boards(None)
    except RegistryError as exc:
        return (), (
            f"Stage could not read its list of boards ({_first(exc)}), so boards it no longer "
            "reads may be listed."
        )
    problem = getattr(personal, "problem", None)
    if not problem:
        return boards, ""
    return boards, f"The user's own board list is ignored until it can be read: {_first(problem)}."


def _first(problem: object) -> str:
    return text(first_line(str(problem)), 200).rstrip(".:")


def _count(counts: dict[str, int] | None, key: str) -> int | None:
    return None if counts is None else counts.get(key, 0)


def _preferences(preferences: Preferences, counts: dict[str, int] | None) -> dict[str, object]:
    from stage.domain.preferences import CHOOSABLE_LOCATIONS, CHOOSABLE_ROLES

    return {
        "saved": not preferences.is_empty,
        "locations": [location.value for location in preferences.locations],
        "roles": [role.value for role in preferences.roles],
        "terms": list(preferences.terms),
        "follow": [
            {"name": company.name, "open_postings": _count(counts, company.folded)}
            for company in preferences.follow
        ],
        "avoid": [
            {"name": company.name, "open_postings": _count(counts, company.folded)}
            for company in preferences.avoid
        ],
        "about": preferences.about,
        "choices": {
            "locations": [location.value for location in CHOOSABLE_LOCATIONS],
            "roles": [role.value for role in CHOOSABLE_ROLES],
            "terms": "a season and a year, such as summer-2027",
        },
    }


def _window(query: str, days: int | None) -> int | None:
    if query:
        return days or None
    return DEFAULT_WINDOW_DAYS if days is None else days or None


async def _no_longer_listed(database: Path, job_ids: list[str]) -> list[dict[str, object]]:
    from stage.services.liveness import check_opened

    checked = await check_opened(database, job_ids, record=False)
    return [
        {"id": short(job.id), "company": text(job.company, 120), "title": text(job.title_raw, 200)}
        for job in checked.gone
    ]


def _launch_all(urls: list[str]) -> list[bool]:
    import webbrowser

    launched: list[bool] = []
    for url in urls:
        if launched and not launched[-1]:
            launched.append(False)
            continue
        launched.append(bool(webbrowser.open(url)))
    return launched


def _background(found: dict[str, object] | None) -> dict[str, object] | None:
    if found is None:
        return None
    error = found.get("error")
    return {**found, "error": text(error, 300) if isinstance(error, str) else None}


def _unreadable(database: Path, exc: sqlite3.Error) -> str:
    from stage.storage.problems import database_problem

    return database_problem(exc, database)


def _no_folder(root: Path) -> str:
    try:
        if root.is_dir():
            return ""
        found = root.exists()
    except OSError as exc:
        return (
            f"Stage cannot reach {root} ({exc.strerror or plain_error(str(exc))}). The user can "
            "set STAGE_EXPORT_DIR to another folder. Nothing was exported."
        )
    if found:
        return (
            f"Stage saves exports in {root}, which is not a folder. The user can set "
            "STAGE_EXPORT_DIR to a folder. Nothing was exported."
        )
    return (
        f"Stage saves exports in {root}, which does not exist. The user can create it or set "
        "STAGE_EXPORT_DIR to another folder. Nothing was exported."
    )


def _listed(ids: tuple[str, ...]) -> str:
    shown = ", ".join(text(short(ident), 200) for ident in ids[:5])
    return shown + (f" and {len(ids) - 5} more" if len(ids) > 5 else "")


def _could_not(verb: str, path: Path, exc: OSError) -> str:
    reason = exc.strerror or plain_error(str(exc))
    return (
        f"Stage could not {verb} {path} ({reason}). The user can check that the file and its "
        "folder are theirs and writable."
    )


def _command(arguments: dict[str, object]) -> str:
    import shlex

    words = str(arguments["words"])
    if words.strip():
        parts = ["stage", "search", shlex.quote(words)]
    elif arguments["new"]:
        parts = ["stage", "list"]
    else:
        parts = ["stage", "export", "--format", "csv"]
    flags = {
        "location": "--location",
        "role": "--role",
        "term": "--term",
        "language": "--lang",
        "company": "--company",
        "days": "--last",
    }
    for key, flag in flags.items():
        value = arguments[key]
        if value is not None:
            parts += [flag, shlex.quote(str(value))]
    parts += [f"--{key}" for key in ("mine", "untracked", "new") if arguments[key]]
    if parts[1] != "export":
        parts.append("--all")
    return " ".join(parts)


__all__ = ["CHOSEN_TERM", "SEASONS", "TERM", "ChosenPlace", "ChosenRole", "Tools"]
