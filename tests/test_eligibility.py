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
from stage.services.screening import normalize_batch
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


@pytest.mark.parametrize(
    "title",
    [
        "Process Engineering Intern",
        "Category Management Project Intern",
        "Talent Acquisition Intern",
        "Environmental Engineering Intern",
        "Construction Management Intern",
        "Investor Relations Intern",
        "Claims Intern",
        "Underwriting Data Analyst Intern",
        "Stage 6 mois juriste protection des données personnelles",
    ],
)
def test_a_named_non_cs_discipline_is_labelled_rather_than_left_unknown(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Supply Chain Applied AI Engineering Intern",
        "Software Engineer Intern - Logistics",
        "Staff Software Developer Manufacturing Engineering",
        "System Design Engineer - AI Cluster Software Engineer",
        "Claims Lab Developer Intern - Summer 2027",
    ],
)
def test_a_cs_role_in_a_non_cs_domain_is_still_accepted(title: str) -> None:
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
        "Technical Project Manager Intern",
        "Product Manager Intern",
        "UX Designer Intern",
        "Help Desk Intern",
        "Marketing Intern",
        "Sales Engineer Intern",
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
        "Pursuing MS or PhD in Computer Science, Electrical Engineering, or Statistics",
        "What You Bring: Pursuing Masters, or PhD in Computer Science",
        "Graduating class of 2029 pursuing a Masters or Doctorate degree in CS",
        "We are looking for a master's or PhD student to join our team",
        "Currently enrolled in or recently graduated from a Master's or PhD program",
        "You will graduate in spring 2028 with a Master's or PhD degree in Statistics",
        "Current MS or PhD student or recent graduate in Computational Biology",
        "Currently enrolled in or will receive a Master's in Computer Science",
        "Qualifications: Master's degree or above in Computer Science",
        "Basic Requirements: Current graduate student in Computer Science",
        "Candidate must be enrolled in a Ph.D. and/or graduate programs in Statistics",
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
        "Currently pursuing a BS, MS, or PhD in Computer Science",
        "Open to a current graduate student or to undergraduates in their final year",
        "Enrolled in a Bachelor's program; a master's or PhD student may also apply",
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


@pytest.mark.parametrize(
    "title",
    [
        "2027 | Americas | Toronto | Engineering | Summer Analyst",
        "Summer Analyst, Engineering",
        "IT Engineer Intern (Early Careers Summer 2027)",
        "Summer Intern 2027 - Business Technology",
        "Technology Analyst Intern, Production Support",
        "Applied Science Intern - Multiple Teams",
        "Perception Intern (Summer 2027)",
        "Cloud Network Engineering Internship Opportunities",
        "Stage universitaire | Technologies numériques - Développement - Hiver 2027",
        "Stage universitaire | Administration des systèmes et des infrastructures TI",
        "Database Analyst, Winter 2027 (Co-op/Internship) - 8 months",
        "Data Architecture and Engineering Intern",
        "ML Ops Intern",
        "GenAI and Digital Workplace Intern (Winter 2027)",
        "AI Co-op (Winter 2027)",
        "AI Solution Architect - Undergraduate Intern",
        "Security Intern, Threat Operations - Winter 2027",
        "Intern - IT Cloud Enablement - Summer - 2027",
        "Information Technology - Development and Infrastructure Internship (hybrid)",
        "Enterprise Architecture Co-Op",
        "Early Careers - Identify & Access Management (IAM) Intern",
        "College to Corporate IT Internship-Risk & Security Engineer (PA)",
        "Systems Developer, Project Management Office, Student Intern (Winter 2027 - 8 Months)",
        "Finance AI Engineering Intern",
        "2027 Institutional Equity Quantitative Finance Off-Cycle Internship - Derivatives",
        "Web Marketing Intern - Front-end Developer",
        "Site Reliability Engineer Intern",
        "Stage en TI - D\u00e9veloppeur d'applications",
        "Stagiaire TI - programmation et infonuagique",
        "Software Engineer Intern, Global Traffic Architecture",
        "Transportation Data Analyst Intern - Summer 2027",
        "Embedded Software Engineer Intern, Body Controls Firmware",
        "Rust Software Engineering Intern, Chip Design Tools",
        "Software Engineering Co-op, Design Automation",
        "Software CAD Engineer, VLSI (RDSS Intern)",
        "ASIC Firmware and Diagnostic Intern",
        "SoC Firmware Engineer Intern, SI Hardware Infrastructure",
        "Applied AI Intern (Non-Silicon)",
        "IT Infrastructure & AI Enablement Intern (Summer 2027)",
        "Rebele Editorial AI Development Intern",
        "SOC Intern",
        "CyberSecurity SOC Intern",
    ],
)
def test_titles_reviewed_on_real_data_are_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None


@pytest.mark.parametrize(
    "title",
    [
        "Hardware Development Engineer Intern",
        "Presales Systems Engineer - Summer 2027 Internship",
        "Tax Technology Intern - Summer 2027",
        "Electrical Test Engineer Intern",
        "Hardware Test Engineer Intern",
        "Analyst - IT Audit & Assurance - Co-op/Intern - 2027",
        "Intern- Information Technology (End User Support- OIT)",
        "2027 Future Talent Program – Pharmaceutical Analysis and Digital Technology",
        "GE Vernova - Energy Technology Analyst Intern - Summer 2027",
        "Intern, IT Infrastructure Support",
        "SkillBridge Intern DoD: Information System Security Officer (ISSO)",
        "Geographic Information Systems Intern/Co-op",
        "Stage collégial | Technologies numériques - Service aux utilisateurs (TI)",
        "Presale Systems Engineer - Summer 2027 Internship",
        "Tax & Data Analytics Intern",
        "Federal Tax Policy Intern",
        "Economics Intern - Structural IO",
        "Applied Scientist (Economics) Intern",
        "Finance Co-op",
        "Fund Finance Infrastructure Intern - December 2026 | MTL",
        "Project Coordinator Co-op (Winter 2027), Capital Markets Technology",
        "AI Consulting Associate Program Product Owner Intern",
        "Software and Go-To-Market Strategy Summer Intern",
        "Intern - Sales Strategy & Analytics",
        "Sales and Application Engineering Co-op",
        "Quality Control Strategy and Science Co-op",
        "2027 Summer Intern Quality Assurance - Compliance & Quality Systems (IOPS)",
        "Technical Writer Intern, Developer Experience (Summer 2027)",
        "Technical Content Author Intern - Technical Content Analytics",
        "Workforce Data & Analytics Intern \u2013 Summer 2027 \u2013 Onsite in Dallas, TX",
        "Junior Material Developer Intern",
        "Creative & AI Intern - Winter/Spring 2027 - On-Site",
        "Photonics Test Engineer Intern",
        "Electrical Systems Engineering Intern - The Toro Company",
        "Stagiaire Eng . Assurance qualit\u00e9 & logistique",
        "Oil and Gas Project Application Engineering Intern",
        "Test Engineer Intern - Exmark",
        "Equipment Engineering Controls Engineer Intern",
        "Manufacturing Engineering (Operational Technology) Co-op (January - August 2027)",
        "Charlotte Technology Consulting Intern - 2027",
        "Sales Tools & Technology Intern \u2013 Undergrad Internship \u2013 Summer 2027",
        "Intern - Business Systems Analyst",
        "mRNA Design Engineer Co-op",
        "IT Business Analyst\u2013 4-month Internship/Coop (Winter 2027)",
        "Business Systems Analyst, Winter 2027 Co-op",
        "Application Engineer Intern",
        "Power Systems Undergraduate Intern - Cyber and Critical Infrastructure Summer Institute",
        "Hardware Developer Eng Co-op/Intern",
        "Electrical Engineering Intern \u2013 R&D Design Validation & Test Automation",
        "Electrical Engineer Intern - Digital Circuitry & Test Automation",
        "2027 Summer Internship: Vehicle Performance Test Engineer (Denton, TX, US, 76205-7864)",
        "Digital Design Backend Engineering Intern, 3D and AI Flows",
        "Student \u2013 Transportation Planning & Traffic Engineering (Winter 2027) 4 Month Term",
        "Research Scientist - ATG Optical Sensor Development Intern (Summer 2027)",
        "Reliability Engineer Intern (McPherson, KS, US, 67460-9402)",
        "Reliability Engineer Co-Op",
        "Power Conversion and Storage Solutions Spring Co-Op/Intern 2027 Jan-May",
        "2027 Electrical Design and Technology Internship - Baltimore MD",
        "Summer Intern - Setpoint and Control Systems (Warrendale, US)",
        "Finance Intern - Data Analysis",
        "HR AI Automation Co-op",
        "Sales Cyber Security Intern",
        "FPGA Design Intern",
        "Engineering Student - FPGA Verification - Winter 2027",
        "FY27 Intern - Display IP FPGA Emulation Intern - Canada (4 months)",
        "Electronic Warfare FPGA Engineer Intern",
        "R&D DSP/FPGA Design Automation Intern",
        "Hardware Intern - AI HW & SoC (Canada)",
        "Associate Footwear Developer Intern",
        "Process Technology Intern Industrial Services | Summer 2027",
        "2027 Summer Intern - Supplier Development Engineer - 6463",
        "Carrier Operations Intern (Portfolio Analytics)",
        "GIS Analyst Intern",
        "2027 Digital Infrastructure Intern - NYC",
        "Business and Technology Intern: Live and VIP Product",
        "Recording Head Characterization & Data Analytics Intern - Summer 2027",
        "2027 Geothermal Production Analytics Engineering Intern - Geysers",
        "Intern - Health Automation & Analytics, Provider Advisory - Campus 2027",
        "Summer intern, Financial Risk Analytics Product",
        "Intern - Automation Engineer (Summer 2027)",
        "Automation Engineer Intern - Electrical",
        "Network Operations Process Improvement Undergraduate Summer Intern",
        "Network Operations Internship",
        "Growth Operations Intern",
        "Summer Intern - Digital Manufacturing",
        "Co-Op, Automation, Digital Manufacturing & Controls",
        "Network Engineer Intern",
        "Network Engineering Intern",
        "Intern - I&C and Network Engineer - Spring or Summer 2027",
        "Network Site Design Intern",
        "Network Administrator Co-op",
        "2027 Finance Data Analyst Intern",
        "FPGA Software Tool Developer Co-op",
        "FPGA Development Tools - Intern",
        "FPGA Verification and Developer Intern",
        "Electrical Design Automation Intern - AI & EDA Automation",
        "Sales Data Analytics Intern",
        "Customer Support AI Intern",
        "AI Content Creator Intern - Generative AI & Digital Media",
        "AI Enablement Intern",
        "Intern, AI Enablement",
        "2027 Guardian Summer Intern, HR Technology Knowledge Management & AI Operations",
        "Center for Global Security Research (CGSR) Undergraduate Research Associate - Summer 2027",
        "Intern - Front End Materials Category Supplier Manager (CSM)",
        "Audit & Assurance - Intern - Technology Controls Advisory (Japanese Services Group)",
        "Year-Round Intern - Business Systems Analysis",
        "Summer Internship Program 2027 - End User Computing (EUC) Intern",
        "Summer Intern - Robotics Technician",
        "Intern (Automation and Controls)",
        "Robotics/Mechatronics Engineering Co-Op - Spring 2027",
        "Investment Processes Intern",
        "Engineering & Technology Intern- Summer 2027 Opportunities",
        "Seismic Imaging Technology Intern",
        "Analyst Co-op - Bilingual \u2013 Winter 2027",
        "ASIC EDA Infrastructure Engineer Intern",
        "Intern - Design Verification Infrastructure Engineer - Platform",
        "Agentic Mixed-Signal Verification Platform Winter/Spring Co-Op (Jan-June '27)",
        "Design Automation & Quality Assurance Engineer Intern",
        "SoC Engineering Intern",
        "2027 Intern - SoC Digital Engineer",
        "VLSI CAD Timing Intern, Applied AI - Summer 2027",
        "Engineering Co-op, AI/ML ASIC CAD",
        "Applied AI Engineer Intern (Silicon Engineering)",
        "Commodities Trading Intern",
    ],
)
def test_titles_reviewed_on_real_data_are_rejected(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Software Test Engineer Intern",
        "Test Automation Engineer Intern",
        "IT Application Engineer Intern, Investments - Summer 2027",
        "HW/SW System and Application Engineer Intern-Summer 2027",
        "Hardware/Software System and Application Engineer Intern",
        "Power Systems Software Intern",
        "Embedded Firmware and Controls Engineer Intern",
        "Mercury Marine: Software Controls Engineering Intern",
        "Embedded Software Engineer Intern - Power Systems",
        "Intern - Power Systems Time-Series Analytics",
        "Summer Intern - Advanced Manufacturing Engineering, Computer Engineering",
        "Performance Test Engineer Intern",
        "Applied AI Test Engineer - Intern",
        "Data Application Engineer Intern",
        "GIS Software Developer Intern",
        "Banking Automation Engineer Intern",
        "QA Automation Engineer Intern",
        "Systems Test Automation Engineering Intern",
        "AI and Automation Engineer Intern",
        "Software and Product Development Engineer Intern",
        "Data Science Intern - Real Estate",
        "Early Career Intern - Real Estate (Data Science)",
        "P&C Actuarial & Underwriting Modeling Intern - Data Scientist",
        "Network Development Engineer Intern, 2027 Summer",
        "Network Security Software Engineer Intern, Security Engineering",
        "Software Development Engineer Intern, Network Automation",
        "Network Automation Engineer Intern",
        "HPC - AI Networking Intern - Network Transport",
        "Summer 2027 Intern-Networking and Computer Engineering",
        "Network Operations Software Engineer Intern",
        "Cloud Network Engineering Internship Opportunities",
    ],
)
def test_software_work_inside_an_excluded_discipline_is_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, title


@pytest.mark.parametrize(
    "title",
    [
        "Critical Infrastructure Protection Intern",
        "Co-Op \u2013 IT \u2013 Technical Security",
        "Intern/Co-Op-IT & Security (JACKSON, MI, US)",
        "Summer Internship Program 2027 - Trading System Development Intern",
        "Summer Internship Program 2027 - Infrastructure Development & Automation Intern",
        "Data Ops Engineer Intern",
        "Student 2 Intern - Data Management Engineer",
        "Human Computer Interaction Engineer Co-op",
        "AI Labs Intern, Emerging Talent",
        "Equipment Engineering AI Vision Engineer Intern",
        "Physics Ai Internship Summer 2027",
        "AI-Driven Equation Discovery Co-op",
        "Research Assistant Co-op - AI Focus - Winter Term- January 2027 - April 2027",
        "Data Sience internship",
        "AI Workflows & Safety Decision Making Intern",
        "Data Operations Intern (Summer 2027)",
        "Creative Technology Intern",
        "2027 Summer Load Forecasting & Analytics Intern (Baltimore, MD)",
        "Digital Forensics Intern",
    ],
)
def test_computing_titles_found_among_unknown_roles_are_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, title


@pytest.mark.parametrize(
    "title",
    [
        "2027 Future Talent Program - Global Communications Intern - AI focus - Intern",
        "Research Intern - Coding Agents & Post-Training Optimization",
    ],
)
def test_titles_that_only_mention_ai_stay_unknown(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None
    assert rejection.reason is RejectionReason.UNKNOWN_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Computer Architecture Intern - 2027",
        "Research Intern - Algorithms Group: Theory",
        "Research Intern - Brain-Computer Interfaces",
        "Research Internship - AI for Science",
        "Differentiable Simulation Research Intern - Summer 2027",
        "Contract Student Worker \u2013 AI Validation and Benchmarking Engineer (40hrs / wk)",
        "Data, AI & Innovation Intern",
        "Applied Data Solutions Program Intern",
        "Student Intern, Web Cloud Solutions (Toronto, ON, CA)",
        "Intern - Research - Inflammation - AI",
        "Research Intern, Oncology Biomarker - AI",
        "Undergraduate Intern - Neurology AI",
        "Quantum Error Correction Research Intern",
        "Internship - Quantum Applications Research",
        "Research Intern: Linear Algebra and Hamiltonian Simulation - 2027",
        "AI & Automation Development Intern",
        "Simulation \u2013 Sim to Real Intern",
        "2027 Summer Intern - Research and Development - Driving Simulation, Automated Driving",
        "Android Engineering Intern",
        "iOS Engineering Intern",
        "Interactive News Engineering Intern",
        "Anticheat Engineer Intern - Summer 2027",
        "First-Year Explore Program Engineering Intern",
        "Automation Test-INTERN",
        "Automation Quality Engineer Intern",
        "VR Development Intern",
        "Data and Computer Eng/Sci Intern",
        "HPC DevOps Engineer Intern, Science & Technology",
        "FICC and Equities (Sales and Trading) Quantitative Strats, Summer Analyst",
        "Computer Programming Intern",
        "Database Conversion Programming Intern",
        "Stagiaire en d\u00e9veloppement outils & processus outill\u00e9",
        "AI Product Operator Intern",
        "GE Vernova - AGI - AI Power Systems Intern - Summer 2027",
        "Institutional Trader Intern (2027 Start - Chicago)",
        "Proprietary Trading Intern (New York) \u2013 Summer 2027",
        "Futures & Options Trading Analyst Intern - Summer 2027",
        "Trading Assistant Intern",
        "Trader Summer Internship Chicago 2027",
        "2027 BNY Summer Internship Program - Trading (New York, NY)",
        "2027 Summer Internship Program \u2013 Systematic Data Strategy, New York",
        "2027 Summer Internship Program \u2013 Systematic Execution Research, New York",
        "Systematic Fast Trading Intern",
        "Summer 2027 - Execution Tech Internship",
        "Data Trading Analyst Summer Internship - 2027 [New York]",
        "Model Risk Management Intern [2027 Internship Program]",
        "Risk Modeling Intern",
        "Trading Desk Operations Engineer Intern",
        "Summer 2027 - Trading Operations Engineer Intern",
        "Trading Bootcamp Micro-Internship - Summer 2027",
        "Designated Market Maker Dmm Trader Intern Us",
    ],
)
def test_cs_work_the_title_rules_used_to_miss_is_kept(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, title


@pytest.mark.parametrize(
    "title",
    [
        "Quantum Hardware Intern",
        "Quantum Lab Assistant - Intern 2027",
        "Ai Strategy Intern",
        "AI Governance Intern",
        "2027 Summer Internship (US) - AI",
        "Winter 2027 Intern, Simulation",
        "Modeling, Simulation, and Analysis Intern",
        "Computational Physics Intern (Summer 2027)",
        "Edge AI and Display Hardware Architecture Development Engineering Intern",
        "2027 Technology Program Intern",
        "Enterprise & Tactical Networking Intern",
        "Data Center Engineer Intern",
        "Data Governance Intern",
        "Renewable Energy Intern",
        "Sports Trader Intern",
        "Sales and Trading Intern",
        "Derivatives Sales Trader Co-op with Drexel University",
        "Trading Operations Intern -  Summer 2027",
        "Summer Associate Internship (Credit Risk Analyst)",
    ],
)
def test_work_outside_cs_or_writing_no_code_stays_out(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is not None, title


@pytest.mark.parametrize(
    ("description", "flagged"),
    [
        ("No security clearance is required for this role.", False),
        ("Security clearance is not required.", False),
        ("Must be able to obtain a security clearance.", True),
        ("Candidates must be a U.S. citizen.", True),
        ("We will not sponsor visas.", True),
        ("Must be a Canadian citizen.", True),
        ("Non-U.S. citizens are not eligible. Must be a U.S. citizen.", True),
        ("Sponsorship: No. Must be a US citizen.", True),
        ("No relocation. Must be a U.S. citizen.", True),
        ("We do not and will not sponsor visas.", True),
        ("U.S. Citizenship: Required", True),
        ("No sponsorship available, must be a U.S. citizen.", True),
        ("No relocation, but you must be a Canadian citizen.", True),
        ("Clearance not required, and no citizenship rule applies.", False),
    ],
)
def test_a_work_rule_counts_only_when_the_posting_requires_it(
    description: str, flagged: bool
) -> None:
    assert resolve_eligibility(_job("Software Intern", description)).work_auth_flag is flagged


def test_a_graduation_year_in_the_title_is_not_a_graduate_degree() -> None:
    assert screen_degree_scope(_job("Software Intern (Grad Year: Spring 2028)")) is None
    assert screen_degree_scope(_job("Graduate Software Intern")) is not None


@pytest.mark.parametrize(
    ("title", "kept"),
    [
        ("Stagiaire en ingénierie logicielle", True),
        ("Stage en développement de jeux vidéo", True),
        ("Stage en ingénierie de l'IA", True),
        ("Stagiaire fiabilité de site (SRE)", True),
        ("Stagiaire testeur automatisé", True),
        ("Stagiaire technicien informatique", False),
        ("Testeur de jeux - stage", False),
        ("Stagiaire en assurance qualité logiciel", False),
    ],
)
def test_french_titles_follow_the_same_role_rules(title: str, kept: bool) -> None:
    batch, _ = normalize_batch([_job(title)])
    assert bool(batch) is kept
