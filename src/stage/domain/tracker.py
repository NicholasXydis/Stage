import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import get_close_matches
from enum import StrEnum

from stage.domain.preferences import clean_text

MAX_NOTE = 1_000
EARLIEST = date(2000, 1, 1)

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApplicationStatus(StrEnum):
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    HIDDEN = "hidden"


MARKERS = {
    ApplicationStatus.SAVED: "[S]",
    ApplicationStatus.APPLIED: "[A]",
    ApplicationStatus.INTERVIEWING: "[I]",
    ApplicationStatus.OFFER: "[O]",
    ApplicationStatus.REJECTED: "[R]",
    ApplicationStatus.WITHDRAWN: "[W]",
}
REPLIED = frozenset(
    {ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER, ApplicationStatus.REJECTED}
)
SENT = frozenset({ApplicationStatus.APPLIED, ApplicationStatus.WITHDRAWN, *REPLIED})
PROGRESS = {
    ApplicationStatus.HIDDEN: -1,
    ApplicationStatus.SAVED: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.INTERVIEWING: 2,
    ApplicationStatus.OFFER: 3,
    ApplicationStatus.REJECTED: 3,
    ApplicationStatus.WITHDRAWN: 3,
}


class TrackerError(ValueError):
    pass


class PostingState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    GONE = "gone"


@dataclass(frozen=True, slots=True)
class Snapshot:
    company: str
    title: str
    location: str
    apply_url: str
    source: str
    term: str


@dataclass(frozen=True, slots=True)
class StatusChange:
    status: ApplicationStatus
    on: date
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class TrackedPosting:
    posting_id: str
    status: ApplicationStatus
    on: date
    changed_at: datetime
    note: str
    snapshot: Snapshot
    history: tuple[StatusChange, ...] = ()

    @property
    def marker(self) -> str:
        return MARKERS.get(self.status, "")


@dataclass(frozen=True, slots=True)
class TrackerStats:
    by_status: dict[ApplicationStatus, int]
    applied_last_7: int
    applied_last_30: int
    applications: int
    heard_back: int
    replies: dict[ApplicationStatus, int]

    @property
    def tracked(self) -> int:
        return sum(
            count
            for status, count in self.by_status.items()
            if status is not ApplicationStatus.HIDDEN
        )


def moves_back(current: ApplicationStatus, wanted: ApplicationStatus) -> bool:
    return PROGRESS[wanted] < PROGRESS[current]


def parse_status(value: str) -> ApplicationStatus:
    choices = [status.value for status in ApplicationStatus]
    wanted = value.strip().lower()
    if wanted in choices:
        return ApplicationStatus(wanted)
    near = get_close_matches(wanted, choices, n=1, cutoff=0.6)
    if near:
        raise TrackerError(f"Unknown status {value!r}. Did you mean {near[0]!r}?")
    raise TrackerError(f"Unknown status {value!r}. Choose from: {', '.join(choices)}")


def parse_day(value: str, today: date) -> date:
    text = value.strip()
    if not _DAY.match(text):
        raise TrackerError(f"Unknown date {value!r}. Write dates like {today.isoformat()}.")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise TrackerError(f"{text} is not a real date.") from exc
    if day > today:
        raise TrackerError(f"{text} is in the future; today is {today.isoformat()}.")
    if day < EARLIEST:
        raise TrackerError(f"{text} is before {EARLIEST.year}, which is too far back.")
    return day


def parse_note(value: str) -> str:
    note = clean_text(value)
    if len(note) > MAX_NOTE:
        raise TrackerError(f"A note can be at most {MAX_NOTE:,} characters.")
    return note


def summarize(entries: list[TrackedPosting], today: date) -> TrackerStats:
    by_status = dict.fromkeys(ApplicationStatus, 0)
    replies = dict.fromkeys(sorted(REPLIED, key=PROGRESS.__getitem__), 0)
    applied_7 = applied_30 = applications = heard_back = 0
    for entry in entries:
        by_status[entry.status] += 1
        day = applied_on(entry)
        if day is not None:
            applications += 1
            applied_7 += day > today - timedelta(days=7)
            applied_30 += day > today - timedelta(days=30)
        answers = [change.status for change in entry.history if change.status in REPLIED]
        if answers:
            heard_back += 1
            replies[answers[-1]] += 1
    return TrackerStats(
        by_status=by_status,
        applied_last_7=applied_7,
        applied_last_30=applied_30,
        applications=applications,
        heard_back=heard_back,
        replies=replies,
    )


def applied_on(entry: TrackedPosting) -> date | None:
    applied = [c.on for c in entry.history if c.status is ApplicationStatus.APPLIED]
    if applied:
        return applied[-1]
    later = [change.on for change in entry.history if change.status in SENT]
    return later[0] if later else None


__all__ = [
    "MARKERS",
    "MAX_NOTE",
    "PROGRESS",
    "REPLIED",
    "SENT",
    "ApplicationStatus",
    "PostingState",
    "Snapshot",
    "StatusChange",
    "TrackedPosting",
    "TrackerError",
    "TrackerStats",
    "applied_on",
    "moves_back",
    "parse_day",
    "parse_note",
    "parse_status",
    "summarize",
]
