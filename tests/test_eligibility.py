from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.classify import resolve_eligibility, screen_degree_scope, screen_is_cs_role
from stage.domain import (
    DegreeRequirement,
    Job,
    JobFilters,
    QuarantineFilters,
    RejectionReason,
    RoleCategory,
)
from stage.services.sync import normalize_batch
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _job(title: str, description: str = "", location: str = "Montreal, QC, Canada") -> Job:
    return Job(
        id=f"greenhouse:acme:{abs(hash((title, description))) % 10**8}",
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw="",
        description=description,
        location_raw=location,
        first_seen=NOW,
        last_seen=NOW,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("PhD required for this position.", DegreeRequirement.PHD),
        ("Doctorate required.", DegreeRequirement.PHD),
        ("Masters required, or equivalent experience.", DegreeRequirement.MASTERS),
        ("Bachelor's degree required.", DegreeRequirement.BACHELORS),
        ("Build backend services in Go.", DegreeRequirement.UNKNOWN),
        ("", DegreeRequirement.UNKNOWN),
    ],
)
def test_degree_requirement_is_read_from_positive_evidence(
    body: str, expected: DegreeRequirement
) -> None:
    verdict = resolve_eligibility(_job("Software Engineer Intern", body))
    assert verdict.degree_requirement is expected


def test_a_phd_requirement_outranks_a_bachelors_mention() -> None:
    verdict = resolve_eligibility(
        _job("Research Intern", "Bachelor's degree required. PhD required for this team.")
    )
    assert verdict.degree_requirement is DegreeRequirement.PHD, (
        "the strictest stated requirement wins, or a bachelors mention masks a doctorate"
    )


def test_an_unstated_degree_is_unknown_and_never_none() -> None:
    verdict = resolve_eligibility(_job("Software Engineer Intern", "Build things."))
    assert verdict.degree_requirement is DegreeRequirement.UNKNOWN, (
        "silence is not a claim that no degree is required"
    )


def test_a_french_degree_requirement_resolves_too() -> None:
    verdict = resolve_eligibility(_job("Stagiaire recherche", "Doctorat requis."))
    assert verdict.degree_requirement is DegreeRequirement.PHD


def test_work_auth_is_set_only_on_positive_exclusion() -> None:
    assert resolve_eligibility(_job("Intern", "Must be a US citizen.")).work_auth_flag
    assert resolve_eligibility(_job("Intern", "Active security clearance required.")).work_auth_flag


@pytest.mark.parametrize(
    "body",
    [
        "We will sponsor visas for exceptional candidates.",
        "Sponsorship available.",
        "Open to international students.",
        "",
    ],
)
def test_willingness_to_sponsor_is_not_evidence_of_exclusion(body: str) -> None:
    assert not resolve_eligibility(_job("Intern", body)).work_auth_flag, (
        "will sponsor means welcome, not excluded"
    )


def test_a_clearly_non_cs_role_is_quarantined_with_its_phrase() -> None:
    rejection = screen_is_cs_role(_job("Registered Nurse Intern"))
    assert rejection is not None
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE
    assert rejection.matched_phrase == "registered nurse"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Intern",
        "Data Science Intern",
        "Stagiaire en genie logiciel",
        "Security Analyst Intern",
        "Summer Intern",
        "Intern",
    ],
)
def test_a_cs_or_unclear_title_is_never_rejected(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, (
        "a title with no discipline signal must be kept, never rejected"
    )


def test_a_technical_word_rescues_an_otherwise_non_cs_title() -> None:
    assert screen_is_cs_role(_job("Cashier Systems Software Intern")) is None, (
        "the rescue list runs first, so exclusion fires only on unambiguous evidence"
    )


def test_an_unknown_role_is_never_rejected_for_being_unknown() -> None:
    job = _job("Summer Intern")
    kept, rejected = normalize_batch([job])
    assert kept, "an unresolved role is a gap in understanding, not grounds for rejection"
    assert kept[0].role is RoleCategory.UNKNOWN
    assert not rejected


async def test_eligibility_round_trips_through_storage_and_filters(db_path: Path) -> None:
    doctorate = _job("Research Intern", "Master's or PhD required.")
    open_to_all = _job("Software Engineer Intern", "Build things.")
    kept, _ = normalize_batch([doctorate, open_to_all])
    assert len(kept) == 2

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=kept)
        )
        stored = {job.id: job for job in await repository.list_jobs(JobFilters())}
        phd_only = await repository.list_jobs(JobFilters(degree=DegreeRequirement.PHD))

    assert stored[doctorate.id].degree_requirement is DegreeRequirement.PHD
    assert stored[open_to_all.id].degree_requirement is DegreeRequirement.UNKNOWN
    assert [job.id for job in phd_only] == [doctorate.id]


async def test_a_non_cs_posting_lands_in_quarantine_not_the_jobs_table(
    db_path: Path,
) -> None:
    kept, rejected = normalize_batch(
        [_job("Registered Nurse Intern"), _job("Software Engineer Intern")]
    )
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                jobs=kept,
                quarantined=rejected,
            )
        )
        entries = await repository.list_quarantined(QuarantineFilters())
        counts = await repository.quarantine_reason_counts()

    assert len(kept) == 1
    assert [entry.reason for entry in entries] == [RejectionReason.NOT_A_CS_ROLE]
    assert counts["not-a-cs-role"] == 1


@pytest.mark.parametrize(
    "title",
    [
        "PhD Research Intern",
        "Research Scientist Intern, PhD",
        "PhD Student Researcher",
        "Doctoral Intern",
        "Machine Learning Intern, PhD",
        "Quantitative Research Intern (PHD)",
        "Model Correlation & SI Intern - Ph. D Degree",
        "Data Science PhD Intern",
    ],
)
def test_an_english_phd_restricted_internship_is_quarantined(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None, title
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE
    assert rejection.matched_phrase, "a rejection must name the evidence that produced it"


@pytest.mark.parametrize(
    "body",
    [
        "PhD candidates only.",
        "Applicants must be enrolled in a PhD program.",
        "You must be currently pursuing a PhD.",
        "Doctorate required.",
        "A doctoral degree required for this role.",
    ],
)
def test_an_english_phd_requirement_in_the_body_is_quarantined(body: str) -> None:
    rejection = screen_degree_scope(_job("Software Engineer Intern", body))
    assert rejection is not None, body
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "title",
    [
        "Stage doctoral en apprentissage automatique",
        "Stagiaire doctoral, vision par ordinateur",
        "Stagiaire doctorale en génie logiciel",
        "Stagiaire — doctorant en informatique",
    ],
)
def test_a_french_phd_restricted_internship_is_quarantined(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None, title
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "body",
    [
        "Doctorat requis.",
        "Vous devez être inscrit au doctorat.",
        "Le candidat doit être inscrite au doctorat.",
    ],
)
def test_a_french_phd_requirement_in_the_body_is_quarantined(body: str) -> None:
    rejection = screen_degree_scope(_job("Stagiaire en génie logiciel", body))
    assert rejection is not None, body


@pytest.mark.parametrize(
    "body",
    [
        "PhD preferred but not required.",
        "A PhD is a plus.",
        "PhD or equivalent experience.",
        "Bachelor's, Master's or PhD accepted.",
        "Open to bachelors, masters or PhD students majoring in computer science.",
        "You will work alongside scientists who hold PhDs.",
        "Our team includes several PhD holders.",
        "Un doctorat est un atout.",
        "Master's or PhD required.",
        "Candidate must be currently pursuing a PhD degree or MS degree.",
    ],
)
def test_a_phd_mention_that_is_not_a_requirement_is_kept(body: str) -> None:
    assert screen_degree_scope(_job("Software Engineer Intern", body)) is None, body


@pytest.mark.parametrize(
    "title",
    [
        "Research Intern - BS/MS/PhD",
        "Software Engineering Intern",
        "Stagiaire en génie logiciel",
        "Machine Learning Intern",
        "Data Science Intern - Master's or PhD",
    ],
)
def test_a_title_naming_a_lower_degree_beside_the_doctorate_is_kept(title: str) -> None:
    assert screen_degree_scope(_job(title)) is None, title


def test_the_degree_screen_runs_after_internship_and_before_the_cs_role_screen() -> None:
    ordinary = _job("Senior Data Scientist, PhD")
    kept, rejected = normalize_batch([ordinary])
    assert not kept
    assert rejected[0].reason is RejectionReason.NOT_AN_INTERNSHIP, (
        "a posting that is not an internship is rejected on that ground first"
    )

    doctorate = _job("PhD Research Intern")
    kept, rejected = normalize_batch([doctorate])
    assert not kept
    assert rejected[0].reason is RejectionReason.OUT_OF_SCOPE_DEGREE


async def test_a_phd_internship_reaches_quarantine_and_never_the_jobs_table(
    db_path: Path,
) -> None:
    doctorate = _job("PhD Research Intern")
    ordinary = _job("Software Engineer Intern")
    kept, rejected = normalize_batch([doctorate, ordinary])

    assert [job.id for job in kept] == [ordinary.id]
    assert [entry.id for entry in rejected] == [doctorate.id]

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(source="greenhouse", run_started_at=NOW, jobs=kept, quarantined=rejected)
        )
        stored = {job.id for job in await repository.list_jobs(JobFilters())}
        held = await repository.list_quarantined(
            QuarantineFilters(reason=RejectionReason.OUT_OF_SCOPE_DEGREE)
        )

    assert doctorate.id not in stored
    assert [entry.id for entry in held] == [doctorate.id]
    assert held[0].matched_phrase


def test_the_recorded_evidence_is_deterministic_and_the_most_specific_match() -> None:
    job = _job("Software Engineer Intern", "PhD candidates only. A PhD is required.")
    first = screen_degree_scope(job)
    assert first is not None
    assert first.matched_phrase == "phd candidates only", (
        "the longest match is the most informative evidence, and a set iteration order "
        "would make the audit trail differ between processes"
    )
    assert all(screen_degree_scope(_job(job.title_raw, job.description)) == first for _ in range(5))


def test_a_degree_list_near_the_requirement_keeps_the_posting() -> None:
    body = "Open to bachelors, masters or PhD students. Applicants must be enrolled in a PhD."
    assert screen_degree_scope(_job("Software Engineer Intern", body)) is None, (
        "a lower degree beside the requirement suppresses the rejection; losing a genuine "
        "internship is worse than showing one the reader cannot apply to"
    )


def test_a_distant_unguarded_requirement_is_not_masked_by_an_earlier_list() -> None:
    filler = "We build distributed systems and care about craft. " * 3
    body = f"Open to bachelors, masters or PhD students. {filler} PhD candidates only."
    rejection = screen_degree_scope(_job("Software Engineer Intern", body))
    assert rejection is not None, (
        "scanning only the first occurrence would stop at the guarded list and never reach "
        "the unguarded requirement further down"
    )
    assert rejection.matched_phrase == "phd candidates only"
