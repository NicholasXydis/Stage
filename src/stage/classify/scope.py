import re
from dataclasses import dataclass
from datetime import UTC, datetime

from stage.domain import Job, LocationBucket, QuarantinedJob, RejectionReason

_SEASON_ENDS = {"spring": (6, 30), "summer": (8, 31)}
_YEAR = re.compile(r"\b20[0-9]{2}\b")


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: RejectionReason
    matched_phrase: str


def screen_is_internship(job: Job) -> Rejection | None:
    from stage.classify.internship import screen_internship

    verdict = screen_internship(job.title_raw, job.signals.employment_type, job.signals.category)
    if not verdict.is_internship:
        return Rejection(
            reason=RejectionReason.NOT_AN_INTERNSHIP,
            matched_phrase=verdict.disqualified_by or "no internship marker in title",
        )
    ended = _term_ended(job)
    return Rejection(RejectionReason.NOT_AN_INTERNSHIP, ended) if ended else None


def _term_ended(job: Job) -> str:
    from stage.normalize import resolve_term

    named = resolve_term(title=job.title_raw)
    end = _SEASON_ENDS.get(named.season)
    if end is None or named.year is None or len(set(_YEAR.findall(job.title_raw))) > 1:
        return ""
    finished = datetime(named.year, *end, 23, 59, tzinfo=UTC)
    posted = job.source_posted_at
    if _aware(job.last_seen) <= finished or (posted is not None and _aware(posted) > finished):
        return ""
    return f"{named.season} {named.year} has ended"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def screen_location(job: Job) -> Rejection | None:
    if job.location is not LocationBucket.INTERNATIONAL:
        return None
    return Rejection(
        reason=RejectionReason.OUT_OF_SCOPE_LOCATION,
        matched_phrase=job.location_raw or "outside Canada and the United States",
    )


def to_quarantined(job: Job, rejection: Rejection) -> QuarantinedJob:
    return QuarantinedJob(
        id=job.id,
        source=job.source,
        company=job.company,
        title_raw=job.title_raw,
        reason=rejection.reason,
        first_seen=job.first_seen,
        last_seen=job.last_seen,
        apply_url_raw=job.apply_url_raw,
        location_raw=job.location_raw,
        location=job.location,
        remote_scope=job.remote_scope,
        matched_phrase=rejection.matched_phrase,
    )
