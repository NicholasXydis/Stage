from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage.classify import resolve_eligibility, screen_degree_scope, screen_is_cs_role
from stage.domain import (
    DegreeRequirement,
    Job,
    JobFilters,
    LocationBucket,
    QuarantineFilters,
    RejectionReason,
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
    ],
)
def test_a_known_cs_title_is_never_rejected(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None


def test_an_ambiguous_title_with_known_cs_signals_is_never_rejected() -> None:
    assert screen_is_cs_role(_job("Software Engineer Intern - AI Infrastructure")) is None


def test_a_vehicle_software_engineer_is_not_rejected_as_a_ui_role() -> None:
    title = "Vehicle Software Intern - Vehicle Software Engineer-Diagnostic User Interface"
    assert screen_is_cs_role(_job(title)) is None


def test_an_unknown_non_cs_title_is_quarantined_for_review() -> None:
    rejection = screen_is_cs_role(_job("Cashier Systems Software Intern"))
    assert rejection is not None
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


def test_an_unknown_role_is_quarantined_for_review() -> None:
    job = _job("Summer Intern")
    kept, rejected = normalize_batch([job])
    assert kept == ()


async def test_eligibility_round_trips_through_storage_and_filters(db_path: Path) -> None:
    doctorate = _job(
        "Software Engineer Research Intern",
        "Bachelor's students welcome. Currently pursuing a PhD is a plus.",
    )
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
        "Data Science Intern - Bachelor's, Master's or PhD",
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
        "the longest match wins, and set order would vary between processes"
    )
    assert all(screen_degree_scope(_job(job.title_raw, job.description)) == first for _ in range(5))


def test_a_degree_list_near_the_requirement_keeps_the_posting() -> None:
    body = "Open to bachelors, masters or PhD students. Applicants must be enrolled in a PhD."
    assert screen_degree_scope(_job("Software Engineer Intern", body)) is None, (
        "a degree list beside the requirement suppresses the rejection"
    )


def test_a_distant_unguarded_requirement_is_not_masked_by_an_earlier_list() -> None:
    filler = "We build distributed systems and care about craft. " * 3
    body = f"Open to bachelors, masters or PhD students. {filler} PhD candidates only."
    rejection = screen_degree_scope(_job("Software Engineer Intern", body))
    assert rejection is not None, (
        "the scan must reach past the guarded list to the bare requirement"
    )
    assert rejection.matched_phrase == "phd candidates only"


@pytest.mark.parametrize(
    "title",
    [
        "Information Technology Intern",
        "Technical Project Manager Intern",
        "Product Manager Intern",
        "UX Designer Intern",
        "Help Desk Intern",
        "Marketing Intern",
        "Sales Engineer Intern",
        "Stagiaire TI",
        "Chef de produit stagiaire",
        "Concepteur UX stagiaire",
        "Ingénieure commerciale stagiaire",
    ],
)
def test_explicitly_excluded_title_families_are_quarantined(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Postdoctoral Research Intern",
        "Stagiaire postdoctorante en informatique",
    ],
)
def test_postdoctoral_titles_are_outside_degree_scope(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


def test_a_fullstack_vehicle_ui_engineer_is_not_rejected_as_a_ui_role() -> None:
    title = "Fullstack C++ Engineer Intern, Vehicle User Interface"
    assert screen_is_cs_role(_job(title)) is None


@pytest.mark.parametrize(
    "description",
    [
        "Currently pursuing a PhD or Master's degree in Computer Science",
        "Currently pursuing a PhD (preferred), or advanced Master's degree",
        "Currently enrolled in a PhD program or have a master degree in Robotics",
        "PhD required",
        "Masters required",
        "Master's or PhD required.",
        "Candidate must be currently pursuing a PhD degree or MS degree.",
        "Currently enrolled in your last year of a Master's degree in a university",
        "Current PhD (or MSc) studies in machine learning and robotics",
        "Currently pursuing a Master's degree in Computer Science",
        "The ideal candidate is currently pursuing a Master's or PhD in Engineering",
    ],
)
def test_a_posting_offering_no_bachelor_option_is_out_of_scope(description: str) -> None:
    verdict = screen_degree_scope(_job("Research Intern", description))

    assert verdict is not None
    assert verdict.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "description",
    [
        "Currently pursuing a Bachelor's or Master's degree",
        "Bachelor's degree required",
        "Pursuing a BS, MS, or PhD in Computer Science",
        "Open to undergraduate and graduate students",
        "Enrolled in an undergraduate program, PhD candidates also welcome",
        "Currently pursuing a Bachelor's, Master's or PhD",
        "Enrolled in your last year of a Bachelor's degree",
        "PhD is a plus",
        "You will work alongside scientists who hold PhDs",
        "",
    ],
)
def test_a_posting_open_to_undergraduates_is_kept(description: str) -> None:
    assert screen_degree_scope(_job("Software Engineering Intern", description)) is None


def test_enrollment_language_reads_as_a_bachelors_requirement() -> None:
    from stage.classify.eligibility import resolve_eligibility

    for text in (
        "Currently pursuing a Bachelor's degree in Computer Science",
        "Working towards bachelor's degree in computer science",
        "Currently enrolled in a Bachelors degree in Computer science",
        "pursuing an undergraduate degree in CS",
    ):
        verdict = resolve_eligibility(_job("Software Engineering Intern", text))
        assert verdict.degree_requirement.value == "bachelors"


def test_enrollment_language_never_quarantines() -> None:
    from stage.classify.eligibility import screen_degree_scope

    job = _job("SWE Intern", "Currently pursuing a Bachelor's or Master's degree")

    assert screen_degree_scope(job) is None


def test_a_clearance_or_sponsorship_limit_is_flagged() -> None:
    from stage.classify.eligibility import resolve_eligibility

    for text in (
        "Must hold an active security clearance",
        "US citizenship required for this role",
        "We cannot sponsor new visas",
    ):
        verdict = resolve_eligibility(_job("Intern", text))
        assert verdict.work_auth_flag
        assert verdict.work_auth_phrase


def test_the_work_auth_reason_is_not_the_degree_reason() -> None:
    from stage.classify.eligibility import resolve_eligibility

    verdict = resolve_eligibility(
        _job("Intern", "Currently pursuing a Bachelor's degree. US citizenship required.")
    )

    assert verdict.degree_requirement.value == "bachelors"
    assert "citizenship" in verdict.work_auth_phrase


def test_a_degree_named_without_a_level_still_reads_as_undergraduate() -> None:
    from stage.classify.eligibility import resolve_eligibility

    for text in (
        "Currently pursuing a degree in Computer Science",
        "Pursuing a degree in Computer Science or a related field",
        "Currently pursuing a technical degree in a quantitative field",
    ):
        assert resolve_eligibility(_job("SWE Intern", text)).degree_requirement.value == "bachelors"


def test_widening_the_degree_lexicon_quarantines_nothing_new() -> None:
    from stage.classify.eligibility import screen_degree_scope

    for text in (
        "Currently pursuing a degree in Computer Science",
        "Currently pursuing a Bachelor's or Master's degree",
        "Pursuing a BS, MS, or PhD in Computer Science",
    ):
        assert screen_degree_scope(_job("SWE Intern", text)) is None


def test_an_export_control_or_residency_limit_is_flagged() -> None:
    from stage.classify.eligibility import resolve_eligibility

    for text in (
        "This role is not eligible for visa sponsorship",
        "This position is subject to export control laws",
        "Must be a US person or lawful permanent resident of the United States",
    ):
        verdict = resolve_eligibility(_job("Intern", text))
        assert verdict.work_auth_flag, text


def test_offering_sponsorship_is_not_a_restriction() -> None:
    from stage.classify.eligibility import resolve_eligibility

    for text in (
        "Visa sponsorship is available for this position",
        "We sponsor work visas for full time positions",
        "Perks include company sponsored lunches",
    ):
        assert not resolve_eligibility(_job("Intern", text)).work_auth_flag, text


@pytest.mark.parametrize(
    ("bucket", "rejected"),
    [
        (LocationBucket.INTERNATIONAL, True),
        (LocationBucket.CANADA, False),
        (LocationBucket.USA, False),
        (LocationBucket.MONTREAL, False),
        (LocationBucket.UNKNOWN, False),
    ],
)
def test_only_international_postings_are_screened_out(
    bucket: LocationBucket, rejected: bool
) -> None:
    from dataclasses import replace

    from stage.classify import screen_location

    job = replace(_job("Software Engineer Intern"), location=bucket)
    verdict = screen_location(job)

    assert (verdict is not None) is rejected
    if verdict is not None:
        assert verdict.reason is RejectionReason.OUT_OF_SCOPE_LOCATION


def test_a_screened_location_records_where_it_was() -> None:
    from dataclasses import replace

    from stage.classify import screen_location

    job = replace(
        _job("Software Engineer Intern", location="Singapore"),
        location=LocationBucket.INTERNATIONAL,
    )
    verdict = screen_location(job)

    assert verdict is not None
    assert verdict.matched_phrase == "Singapore"
