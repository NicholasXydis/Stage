from dataclasses import dataclass

from stage.domain import Job, LocationBucket, QuarantinedJob, RejectionReason


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: RejectionReason
    matched_phrase: str


def screen_location(job: Job, evidence: tuple[str, ...] = ()) -> Rejection | None:
    if job.location is not LocationBucket.UNKNOWN:
        return None
    if job.remote_scope is None:
        return None
    return Rejection(
        reason=RejectionReason.UNKNOWN_LOCATION,
        matched_phrase=", ".join(evidence) or "remote is not a location",
    )


def screen_is_internship(job: Job) -> Rejection | None:
    from stage.classify.internship import screen_internship

    verdict = screen_internship(job.title_raw, job.signals.employment_type)
    if verdict.is_internship:
        return None
    return Rejection(
        reason=RejectionReason.NOT_AN_INTERNSHIP,
        matched_phrase=verdict.disqualified_by or "no internship marker in title",
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
