import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BeforeValidator, Field

from stage.ai.references import MARK, expand, short
from stage.ai.shape import (
    CAUTION,
    COMPACT_COLUMNS,
    CursorError,
    answer,
    change_row,
    compact_row,
    day,
    freshness,
    kept_copy,
    next_cursor,
    past_the_last_page,
    plural,
    posting_detail,
    posting_row,
    read_cursor,
    rejected_row,
    text,
    tracker_row,
)
from stage.domain import DEFAULT_WINDOW_DAYS, JobFilters
from stage.domain.preferences import Preferences
from stage.storage import AsyncRepository, open_repository

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

NO_DATABASE = (
    "Stage has no postings on this computer yet. The user can run stage sync in a terminal "
    "to collect them."
)
NO_IDS = "Ids look like greenhouse:acme:1234; search_postings returns them."
GONE = (
    "A posting marked gone is no longer stored by Stage; its fields are the copy saved when "
    "the user tracked it."
)
SOURCES_NOTE = (
    "A blocked host or a failing board means Stage already backed off and will try again on a "
    "later sync. Do not suggest disabling boards; stage doctor in a terminal explains each "
    "warning."
)
QUARANTINE_NOTE = (
    "These postings were rejected by Stage's screens and never appear in search_postings. "
    "A rejection that looks wrong is worth telling the user about."
)


def _lowered(value: object) -> object:
    return value.lower() if isinstance(value, str) else value


def _default(fallback: object) -> BeforeValidator:
    return BeforeValidator(lambda value: fallback if _blank(value) else value)


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


PostingId = Annotated[str, Field(min_length=1, max_length=2_000)]
Cursor = Annotated[
    str | None,
    Field(
        max_length=100,
        description="next_cursor from the previous page; send the same other arguments with it",
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
    Field(min_length=1, max_length=120, description="Exact employer name, such as Shopify"),
    _default(None),
]


class Tools:
    def __init__(self, database: Path) -> None:
        self.database = database

    @asynccontextmanager
    async def _repository(self) -> AsyncIterator[AsyncRepository]:
        from stage.storage.migrations import SchemaVersionError

        if not self.database.exists():
            raise ToolError(NO_DATABASE)
        try:
            async with open_repository(self.database) as repository:
                yield repository
        except SchemaVersionError as exc:
            raise ToolError(f"Stage could not open its database: {exc}.") from exc
        except sqlite3.Error as exc:
            raise ToolError(_unreadable(self.database, exc)) from exc

    async def search_postings(
        self,
        words: Annotated[
            str,
            Field(
                max_length=200,
                description=(
                    "Words to find in titles, employers and descriptions. Leave empty to list "
                    "the newest postings instead."
                ),
            ),
            _default(""),
        ] = "",
        location: Annotated[
            Place | None,
            Field(description="montreal, canada (the rest of Canada), usa, or unknown"),
            _default(None),
        ] = None,
        role: Annotated[Role | None, Field(description="The kind of work"), _default(None)] = None,
        term: Annotated[
            str | None,
            Field(pattern=TERM, description="A term such as summer-2027, or unknown"),
            BeforeValidator(_lowered),
            _default(None),
        ] = None,
        language: Annotated[
            Language | None,
            Field(description="The language the posting is written in"),
            _default(None),
        ] = None,
        company: Company = None,
        mine: Annotated[
            bool,
            Field(description="Apply the places, roles, terms and employers the user saved"),
            _default(False),
        ] = False,
        untracked: Annotated[
            bool,
            Field(description="Leave out every posting the user saved, applied to or hid"),
            _default(False),
        ] = False,
        new: Annotated[
            bool,
            Field(description="Only postings that appeared since the sync before last"),
            _default(False),
        ] = False,
        days: Annotated[
            int | None,
            Field(
                ge=0,
                le=3650,
                description=(
                    "Only postings first seen in the last N days; 0 means any age. Without "
                    f"words the default is {DEFAULT_WINDOW_DAYS}; with words it is any age."
                ),
            ),
            _default(None),
        ] = None,
        limit: Limit = PAGE,
        all_matches: Annotated[
            bool,
            Field(
                description=(
                    f"Return up to {COMPACT_PAGE} matches per page as compact rows instead of "
                    "full ones; limit is ignored"
                )
            ),
            _default(False),
        ] = False,
        cursor: Cursor = None,
    ) -> CallToolResult:
        from stage.domain import Language as Written
        from stage.domain import LocationBucket, RoleCategory
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
        filters = JobFilters(
            location=LocationBucket(location) if location else None,
            term=term,
            role=RoleCategory(role) if role else None,
            language=Written(language) if language else None,
            company=company,
            limit=None,
        )
        notes: list[str] = []
        described = ""
        if mine:
            filters, preferences, problems = _mine(filters)
            described = preferences.describe(filters)
            notes.extend(problems)
        query = words if words.strip() else ""
        now = datetime.now(UTC)
        async with self._repository() as repository:
            last_sync = await repository.last_sync_at()
            view = await tracker_view(repository)
            excluded = view.tracked if untracked else view.hidden
            version = [last_sync, sorted(excluded), described]
            offset = _offset(cursor, arguments, version)
            scoped = replace(filters, excluded_ids=excluded, limit=offset + page)
            if new:
                since = await repository.previous_sync_at()
                if since is not None:
                    scoped = replace(scoped, first_seen_after=since)
                elif last_sync is not None:
                    notes.append("Only one sync so far, so every posting counts as new.")
            if query:
                window = days or None
                listing = await search_jobs(repository, query, scoped, window_days=window)
            else:
                window = DEFAULT_WINDOW_DAYS if days is None else days or None
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
        more = following < listing.total_matching
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
        if query and not listing.terms:
            notes.append("Nothing in those words can be searched; punctuation is dropped.")
        if not known:
            asked = text(company or "", 60)
            notes.append(
                f"No employer named {asked!r} is stored."
                + (f" Did you mean {text(near, 60)!r}?" if near else "")
            )
        if listing.no_location:
            notes.append(
                f"Left out {plural(listing.no_location, 'more posting')} with no location "
                "given; send location='unknown' with mine=true for those."
            )
        if hidden:
            notes.append(
                f"Left out {plural(hidden, 'posting')} the user hid; get_tracker with "
                "status='hidden' shows hidden postings."
            )
        if all_matches and more:
            notes.append(f"The user can see every match with: {_command(arguments)}")
        if view.problem:
            payload["warning"] = view.problem
        payload["notes"] = notes
        payload["caution"] = CAUTION
        return answer({**payload, **freshness(last_sync, now)})

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
                "recent_days": DEFAULT_WINDOW_DAYS,
                "new_since_previous_sync": fresh,
                "stored_postings": report.total_jobs,
                "duplicates_linked": report.duplicates,
                "quarantined": sum(report.quarantined.values()),
                "quarantined_by_reason": report.quarantined,
                "stored_by": report.composition,
                "recent_syncs": syncs,
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
            offset = _offset(cursor, arguments, last_sync)
            listing = await list_quarantined(
                repository,
                QuarantineFilters(
                    reason=RejectionReason(reason) if reason else None,
                    company=company,
                    limit=offset + limit,
                ),
            )
        shown = listing.entries[offset : offset + limit]
        following = offset + len(shown)
        return answer(
            {
                "total": listing.total_matching,
                "offset": offset,
                "count": len(shown),
                "next_cursor": next_cursor(following, listing.total_matching, arguments, last_sync),
                "by_reason": listing.reason_counts,
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
        async with self._repository() as repository:
            report = await doctor(repository, now=now)
        sources = [
            {
                "source": source.source,
                "stored": source.stored,
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
                    "reason": text(state.reason, 200),
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
                    "last_error": text(board.last_error, 200),
                }
                for board in boards[:MAX_BOARDS]
            ]
            payload["boards_not_shown"] = max(0, len(boards) - MAX_BOARDS)
        payload["notes"] = [SOURCES_NOTE]
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
                    "posting stays applied; set override to move it back on purpose."
                )
            ),
        ] = None,
        note: Annotated[
            str | None,
            Field(max_length=1_000, description="A short note; an empty string clears it"),
        ] = None,
        on: Annotated[
            str | None,
            Field(
                pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                description="The day it happened, like 2026-10-20; today by default",
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
        from stage.services.tracker import TrackerProblemError, record_status

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
        async with self._repository() as repository:
            wanted = await _resolved(repository, ids)
            try:
                report = await record_status(
                    repository, wanted, chosen, on=when, note=written, forward_only=forward_only
                )
            except TrackerProblemError as exc:
                raise ToolError(str(exc)) from exc
            last_sync = await repository.last_sync_at()
        if report.missing:
            raise ToolError(f"No posting with id {_listed(report.missing)}. Nothing was changed.")
        if report.untracked:
            raise ToolError(
                f"{_listed(report.untracked)} is not tracked yet, so there is no status to keep. "
                "Send status='saved' to start tracking it. Nothing was changed."
            )
        notes: list[str] = []
        if forward_only and report.unchanged:
            notes.append(
                "saved and applied never move a posting back. Send override=true to set the "
                "status exactly."
            )
        return answer(
            {
                "changed": [
                    {
                        **change_row(change.entry),
                        "before": change.before.value if change.before else None,
                    }
                    for change in report.changed
                ],
                "unchanged": [change_row(entry) for entry in report.unchanged],
                "notes": notes,
                **freshness(last_sync, now),
            }
        )

    async def untrack_postings(
        self,
        ids: Annotated[
            list[PostingId],
            Field(
                min_length=1,
                max_length=MAX_CHANGE,
                description="Posting ids to stop tracking; hidden ones show again",
            ),
        ],
    ) -> CallToolResult:
        from stage.services.tracker import TrackerProblemError, remove_tracked

        now = datetime.now(UTC)
        async with self._repository() as repository:
            wanted = await _resolved(repository, ids)
            try:
                report = await remove_tracked(repository, wanted)
            except TrackerProblemError as exc:
                raise ToolError(str(exc)) from exc
            last_sync = await repository.last_sync_at()
        if report.missing:
            raise ToolError(f"No posting with id {_listed(report.missing)}. Nothing was changed.")
        return answer(
            {
                "removed": [change_row(entry) for entry in report.removed],
                "not_tracked": [short(ident) for ident in report.not_tracked],
                **freshness(last_sync, now),
            }
        )

    async def get_tracker(
        self,
        status: Annotated[
            Status | None,
            Field(description="Only this status; hidden postings show only when asked for"),
            _default(None),
        ] = None,
        limit: Limit = PAGE,
        cursor: Cursor = None,
    ) -> CallToolResult:
        from stage.domain.tracker import ApplicationStatus
        from stage.services.tracker import TrackerProblemError, stats_payload, tracker_listing

        arguments: dict[str, object] = {"status": status}
        now = datetime.now(UTC)
        async with self._repository() as repository:
            try:
                listing = await tracker_listing(
                    repository, ApplicationStatus(status) if status else None
                )
            except TrackerProblemError as exc:
                raise ToolError(str(exc)) from exc
        version = [
            listing.last_sync_at,
            [(row.entry.posting_id, row.entry.changed_at) for row in listing.rows],
        ]
        try:
            offset = read_cursor(cursor, arguments, version)
        except CursorError as exc:
            raise ToolError(str(exc)) from exc
        shown = [tracker_row(row) for row in listing.rows[offset : offset + limit]]
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
                "notes": [GONE] if any(row["posting"] == "gone" for row in shown) else [],
                "caution": CAUTION,
                **freshness(listing.last_sync_at, now),
            }
        )

    async def get_preferences(self) -> CallToolResult:
        from stage.services.preferences import load_preferences

        loaded = load_preferences()
        counts, synced = await self._counts(loaded.preferences)
        return answer(
            {
                **_preferences(loaded.preferences, counts),
                "problems": list(loaded.problems),
                **synced,
            }
        )

    async def set_preferences(
        self,
        locations: Annotated[
            list[ChosenPlace] | None,
            Field(max_length=3, description="Places to keep; canada includes montreal"),
        ] = None,
        roles: Annotated[
            list[ChosenRole] | None, Field(max_length=8, description="Kinds of work to keep")
        ] = None,
        terms: Annotated[
            list[Annotated[str, Field(pattern=CHOSEN_TERM), BeforeValidator(_lowered)]] | None,
            Field(max_length=12, description="Terms such as summer-2027"),
        ] = None,
        follow: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=120)]] | None,
            Field(max_length=50, description="Employers to include from any place"),
        ] = None,
        avoid: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=120)]] | None,
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
            Field(description="Delete every saved preference; send it alone"),
            _default(False),
        ] = False,
    ) -> CallToolResult:
        from stage.domain.preferences import PreferenceError
        from stage.services.preferences import (
            clear_preferences,
            load_preferences,
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
                removed = clear_preferences()
            except OSError as exc:
                raise ToolError(_could_not("delete", preferences_path(), exc)) from exc
            return answer({**_preferences(Preferences(), {}), "cleared": removed})
        if not changing:
            raise ToolError("Send at least one field to change; get_preferences shows them.")
        loaded = load_preferences()
        known: dict[str, str] = {}
        if follow is not None or avoid is not None:
            try:
                async with self._repository() as repository:
                    known = await stored_companies(repository)
            except ToolError:
                known = {}
        try:
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
        except PreferenceError as exc:
            raise ToolError(f"{exc} Nothing was saved.") from exc
        try:
            save_preferences(revised)
        except OSError as exc:
            raise ToolError(_could_not("save", preferences_path(), exc)) from exc
        counts, synced = await self._counts(revised)
        return answer(
            {
                **_preferences(revised, counts),
                "warnings": [*loaded.problems, *unseen_companies(revised, known)],
                **synced,
            }
        )

    async def _counts(self, preferences: Preferences) -> tuple[dict[str, int], dict[str, object]]:
        wanted = {company.folded for company in (*preferences.follow, *preferences.avoid)}
        try:
            async with self._repository() as repository:
                counts = await repository.open_counts_by_company(wanted)
                last_sync = await repository.last_sync_at()
        except ToolError:
            return {}, {}
        return counts, freshness(last_sync, datetime.now(UTC))


async def _resolved(repository: AsyncRepository, ids: list[str]) -> list[str]:
    from stage.services.tracker import tracker_view

    wanted = list(dict.fromkeys(ident.strip() for ident in ids))
    if not any(f":{MARK}" in ident for ident in wanted):
        return wanted
    view = await tracker_view(repository)
    return list(dict.fromkeys(await expand(repository, wanted, view.statuses.keys())))


def _mine(filters: JobFilters) -> tuple[JobFilters, Preferences, list[str]]:
    from stage.services.preferences import load_preferences

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
        raise ToolError(
            "The saved preferences only hold a note about the user. Ask which places, roles "
            "or terms they want and save them with set_preferences."
        )
    return preferences.apply(filters), preferences, list(loaded.problems)


def _offset(cursor: str | None, arguments: dict[str, object], version: object) -> int:
    try:
        return read_cursor(cursor, arguments, version)
    except CursorError as exc:
        raise ToolError(str(exc)) from exc


def _preferences(preferences: Preferences, counts: dict[str, int]) -> dict[str, object]:
    from stage.domain.preferences import CHOOSABLE_LOCATIONS, CHOOSABLE_ROLES

    return {
        "saved": not preferences.is_empty,
        "locations": [location.value for location in preferences.locations],
        "roles": [role.value for role in preferences.roles],
        "terms": list(preferences.terms),
        "follow": [
            {"name": company.name, "open_postings": counts.get(company.folded, 0)}
            for company in preferences.follow
        ],
        "avoid": [
            {"name": company.name, "open_postings": counts.get(company.folded, 0)}
            for company in preferences.avoid
        ],
        "about": preferences.about,
        "choices": {
            "locations": [location.value for location in CHOOSABLE_LOCATIONS],
            "roles": [role.value for role in CHOOSABLE_ROLES],
            "terms": "a season and a year, such as summer-2027",
        },
    }


def _unreadable(database: Path, exc: sqlite3.Error) -> str:
    reason = str(exc) or type(exc).__name__
    if "locked" in reason or "busy" in reason:
        return "Stage's database is busy, most likely with a sync. Try again in a minute."
    return (
        f"Stage could not read its database at {database} ({reason}). The user can run "
        "stage doctor in a terminal for details."
    )


def _listed(ids: tuple[str, ...]) -> str:
    shown = ", ".join(text(short(ident), 200) for ident in ids[:5])
    return shown + (f" and {len(ids) - 5} more" if len(ids) > 5 else "")


def _could_not(verb: str, path: Path, exc: OSError) -> str:
    return f"Could not {verb} {path}: {exc.strerror or type(exc).__name__}."


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


__all__ = ["CHOSEN_TERM", "SEASONS", "TERM", "Tools"]
