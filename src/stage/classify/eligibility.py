from dataclasses import dataclass

from stage.classify.scope import Rejection
from stage.domain import DegreeRequirement, Job, RejectionReason
from stage.lexicon import eligibility_lexicon, fold

_ORDER = ("phd", "masters", "bachelors")
_CLAUSE = 60


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    degree_requirement: DegreeRequirement
    work_auth_flag: bool
    matched_phrase: str = ""


def _ranked(phrases: frozenset[str]) -> list[str]:
    return sorted(phrases, key=lambda phrase: (-len(phrase), phrase))


def _hit(haystack: str, phrases: frozenset[str]) -> str:
    padded = f" {haystack} "
    for phrase in _ranked(phrases):
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


def _restricted_hit(haystack: str, phrases: frozenset[str], alternatives: frozenset[str]) -> str:
    padded = f" {haystack} "
    for phrase in _ranked(phrases):
        needle = f" {phrase} "
        start = padded.find(needle)
        while start >= 0:
            window = padded[max(0, start - _CLAUSE) : start + len(needle) + _CLAUSE]
            if not _hit(window.strip(), alternatives):
                return phrase
            start = padded.find(needle, start + 1)
    return ""


def screen_degree_scope(job: Job) -> Rejection | None:
    lexicon = eligibility_lexicon()

    required = _restricted_hit(
        fold(f"{job.title_raw} {job.description}"),
        lexicon.phd_required,
        lexicon.degree_list_tokens,
    )
    if required:
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=required)

    title = fold(job.title_raw)
    token = _hit(title, lexicon.phd_title_tokens)
    if not token or _hit(title, lexicon.degree_list_tokens):
        return None
    return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=token)


def screen_is_cs_role(job: Job) -> Rejection | None:
    lexicon = eligibility_lexicon()
    title = fold(job.title_raw)

    if _hit(title, lexicon.non_cs_rescue):
        return None
    phrase = _hit(title, lexicon.non_cs)
    if not phrase:
        return None
    return Rejection(reason=RejectionReason.NOT_A_CS_ROLE, matched_phrase=phrase)
