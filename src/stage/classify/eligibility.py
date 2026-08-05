from dataclasses import dataclass

from stage.classify.scope import Rejection
from stage.domain import DegreeRequirement, Job, RejectionReason
from stage.lexicon import eligibility_lexicon, fold

_ORDER = ("phd", "masters", "bachelors")


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    degree_requirement: DegreeRequirement
    work_auth_flag: bool
    matched_phrase: str = ""


def _hit(haystack: str, phrases: frozenset[str]) -> str:
    padded = f" {haystack} "
    for phrase in phrases:
        if f" {phrase} " in padded:
            return phrase
    return ""


def resolve_eligibility(job: Job) -> EligibilityVerdict:
    lexicon = eligibility_lexicon()
    body = fold(f"{job.title_raw} {job.description}")

    degree = DegreeRequirement.UNKNOWN
    matched = ""
    for level in _ORDER:
        phrase = _hit(body, lexicon.degree_required.get(level, frozenset()))
        if phrase:
            degree = DegreeRequirement(level)
            matched = phrase
            break

    excluded = _hit(body, lexicon.work_auth_excluded)
    return EligibilityVerdict(
        degree_requirement=degree,
        work_auth_flag=bool(excluded),
        matched_phrase=matched or excluded,
    )


def screen_is_cs_role(job: Job) -> Rejection | None:
    lexicon = eligibility_lexicon()
    title = fold(job.title_raw)

    if _hit(title, lexicon.non_cs_rescue):
        return None
    phrase = _hit(title, lexicon.non_cs)
    if not phrase:
        return None
    return Rejection(reason=RejectionReason.NOT_A_CS_ROLE, matched_phrase=phrase)
