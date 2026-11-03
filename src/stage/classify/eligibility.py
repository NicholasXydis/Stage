import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from stage.classify.role import classify_role
from stage.classify.scope import Rejection
from stage.domain import DegreeRequirement, Job, RejectionReason
from stage.lexicon import eligibility_lexicon, fold

_ORDER = ("phd", "masters", "bachelors")
_CLAUSE = 60
_NOT_A_DEGREE = re.compile(
    r",\s*MS\s*(?=$|\)|\d{5})"
    r"|\bMS[\s-]+(?:Access|Office|Excel|SQL|Dynamics|Teams|Word|Project|Fabric|Azure)\b"
)


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    degree_requirement: DegreeRequirement
    work_auth_flag: bool
    matched_phrase: str = ""
    work_auth_phrase: str = ""


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
        work_auth_phrase=excluded,
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

    body = fold(f"{job.title_raw} {job.description}")

    required = _restricted_hit(body, lexicon.phd_required, lexicon.undergraduate_tokens)
    if required:
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=required)

    graduate_only = _restricted_hit(
        body,
        lexicon.degree_required.get("phd", frozenset())
        | lexicon.degree_required.get("masters", frozenset()),
        lexicon.undergraduate_tokens,
    )
    if graduate_only:
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=graduate_only)

    title = fold(_NOT_A_DEGREE.sub(" ", job.title_raw))
    token = _hit(title, lexicon.phd_title_tokens)
    if token and not _hit(title, lexicon.degree_list_tokens):
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=token)

    graduate = _hit(title, lexicon.graduate_title_tokens) or token
    if graduate and not _hit(title, lexicon.undergraduate_tokens):
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=graduate)

    convention = _hit(title, lexicon.graduate_by_convention)
    if convention and not (
        _hit(title, lexicon.undergraduate_tokens)
        or _hit(fold(job.description), lexicon.degree_required.get("bachelors", frozenset()))
    ):
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=convention)
    if _hit(title, lexicon.undergraduate_tokens) or _hit(title, lexicon.degree_list_tokens):
        return None
    linked = _linked_degree(job.apply_url_raw)
    if linked:
        return Rejection(reason=RejectionReason.OUT_OF_SCOPE_DEGREE, matched_phrase=linked)
    return None


def _linked_degree(url: str) -> str:
    lexicon = eligibility_lexicon()
    try:
        path = urlsplit(url or "").path
    except ValueError:
        return ""
    slug = fold(path.rstrip("/").rpartition("/")[2])
    if len(slug.split()) < 3 or _hit(slug, lexicon.undergraduate_tokens):
        return ""
    graduate = frozenset(phrase for phrase in lexicon.graduate_title_tokens if len(phrase) > 3)
    return _hit(slug, lexicon.phd_title_tokens) or _hit(slug, graduate)


def screen_is_cs_role(job: Job) -> Rejection | None:
    lexicon = eligibility_lexicon()
    title = fold(job.title_raw)
    title_verdict = classify_role(job.title_raw, source_category=job.signals.category)

    phrase = _hit(title, lexicon.excluded_titles)
    exception = _hit(title, lexicon.technical_title_exceptions)
    if phrase and not (exception and title_verdict.matched):
        return Rejection(reason=RejectionReason.NOT_A_CS_ROLE, matched_phrase=phrase)

    phrase = _hit(title, lexicon.non_cs)
    if phrase and not _hit(title, lexicon.non_cs_exceptions):
        return Rejection(reason=RejectionReason.NOT_A_CS_ROLE, matched_phrase=phrase)

    if title_verdict.matched:
        return None
    return Rejection(
        reason=RejectionReason.UNKNOWN_CS_ROLE,
        matched_phrase="no matching CS role or source category",
    )
