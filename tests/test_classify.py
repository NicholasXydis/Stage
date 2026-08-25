import json
from pathlib import Path

import pytest

from stage.classify import classify_role, screen_internship
from stage.domain import RoleCategory

FIXTURE = Path(__file__).parent / "fixtures" / "bilingual_titles.json"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern",
        "Software Engineering Internship",
        "Co-op Software Developer",
        "Co-Op Engineer, Backend",
        "Summer Analyst",
        "Summer Associate, Technology",
        "Data Science Intern - Summer 2027",
        "Stagiaire en Développement Logiciel",
        "Stagiaire, génie logiciel",
        "Stage de développement logiciel",
        "Alternance Développeur Full Stack",
        "Working Student — Backend",
    ],
)
def test_internships_are_recognized(title: str) -> None:
    assert screen_internship(title).is_internship, title


@pytest.mark.parametrize(
    ("title", "why"),
    [
        ("Internal Auditor", "intern is a prefix of internal"),
        ("Internal Communications Manager", "same prefix"),
        ("International Education Growth Lead", "intern is a prefix of international"),
        ("Stage Manager", "stage is an English noun"),
        ("Early Stage Investor Relations", "same"),
        ("Senior Engineer, Seed Stage Startup", "same"),
        ("Main Stage Production Assistant", "same"),
        ("Launch Engineer, Stage 0 Propellant Generation (Starship)", "not French"),
        ("Stage Software Engineer", "not French"),
    ],
)
def test_lookalike_titles_are_not_internships(title: str, why: str) -> None:
    assert not screen_internship(title).is_internship, why


@pytest.mark.parametrize(
    "title",
    [
        "Programmeur·se Senior C++ - Apprentissage automatique",
        "Développeur·euse logiciel en exploitation de modèles d'apprentissage",
        "Ingénieur en apprentissage profond",
        "Chercheur en apprentissage automatique",
    ],
)
def test_apprentissage_is_machine_learning_not_an_apprenticeship(title: str) -> None:
    verdict = screen_internship(title)
    assert not verdict.is_internship
    assert verdict.matched == ()


def test_apprenticeships_and_traineeships_are_not_internship_evidence() -> None:
    for title in (
        "Apprentice Software Engineer",
        "Software Developer Trainee",
        "Software Developer Traineeship",
        "Apprenti développeur logiciel",
        "Apprentie développeuse logicielle",
        "Contrat d'apprentissage — Développeur logiciel",
    ):
        assert not screen_internship(title).is_internship


def test_french_alternance_is_still_recognized() -> None:
    assert screen_internship("Alternance Développeur Full Stack").is_internship


@pytest.mark.parametrize(
    "title",
    [
        "Internship Program Manager",
        "Intern Manager, University Programs",
        "Internship Coordinator",
        "New Grad Software Engineer",
        "Recent Graduate Program - Engineering",
        "Entry Level Software Developer",
    ],
)
def test_postings_about_interns_are_not_internships(title: str) -> None:
    verdict = screen_internship(title)
    assert not verdict.is_internship
    assert verdict.disqualified_by, "a disqualified posting must name what disqualified it"


def test_the_internship_screen_never_reads_a_description() -> None:
    import inspect

    assert set(inspect.signature(screen_internship).parameters) == {
        "title",
        "employment_type",
        "category",
    }, "screen_internship reads the title and publisher signals only, never a description"


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Software Engineer Intern", RoleCategory.SWE),
        ("Backend Developer Intern", RoleCategory.SWE),
        ("Security Engineer Intern", RoleCategory.SECURITY),
        ("Cybersecurity Analyst Intern", RoleCategory.SECURITY),
        ("Data Engineer Intern", RoleCategory.DATA),
        ("Machine Learning Engineer Intern", RoleCategory.ML_AI),
        ("Computer Vision Research Intern", RoleCategory.ML_AI),
        ("Quantitative Researcher Intern", RoleCategory.QUANT),
        ("Site Reliability Engineering Intern", RoleCategory.INFRA),
        ("Embedded Software Intern", RoleCategory.EMBEDDED),
        ("Firmware Engineering Intern", RoleCategory.EMBEDDED),
    ],
)
def test_role_categorization(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Stagiaire en génie logiciel", RoleCategory.SWE),
        ("Stagiaire — Développement logiciel", RoleCategory.SWE),
        ("Stagiaire en science des données", RoleCategory.DATA),
        ("Stagiaire en cybersécurité", RoleCategory.SECURITY),
        ("Stagiaire, intelligence artificielle", RoleCategory.ML_AI),
        ("Stagiaire en apprentissage automatique", RoleCategory.ML_AI),
        ("Stagiaire — logiciel embarqué", RoleCategory.EMBEDDED),
        ("Développeur·euse logiciel stagiaire", RoleCategory.SWE),
    ],
)
def test_french_role_categorization(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role


@pytest.mark.parametrize(
    ("english", "french", "role"),
    [
        ("QA Analyst Intern", "Stagiaire, analyste qualité", RoleCategory.SWE),
        ("QA Tester Intern", "Stagiaire testeur de jeux", RoleCategory.SWE),
        (
            "Test Automation Engineer Intern",
            "Stagiaire en automatisation des tests",
            RoleCategory.SWE,
        ),
        ("Compiler Engineer Intern", "Stagiaire, compilateur", RoleCategory.SWE),
        ("Graphics Engineer Intern", "Stagiaire programmeur graphique", RoleCategory.SWE),
        ("Gameplay Programmer Intern", "Stagiaire programmeur gameplay", RoleCategory.SWE),
        ("Algorithm Engineer Intern", "Stagiaire en algorithmique", RoleCategory.SWE),
        ("Software Architect Intern", "Stagiaire architecte logiciel", RoleCategory.SWE),
        ("Data Developer Intern", "Stagiaire en développement de données", RoleCategory.DATA),
        (
            "Systems Administrator Intern",
            "Stagiaire administrateur systèmes",
            RoleCategory.INFRA,
        ),
        ("Network Administrator Intern", "Stagiaire administrateur réseau", RoleCategory.INFRA),
        (
            "High Performance Computing Intern",
            "Stagiaire en calcul haute performance",
            RoleCategory.INFRA,
        ),
        ("Build Operations Intern", "Stagiaire en automatisation des builds", RoleCategory.INFRA),
        ("MLOps Engineer Intern", "Stagiaire MLOps", RoleCategory.INFRA),
        (
            "Database Administrator Intern",
            "Stagiaire administrateur de bases de données",
            RoleCategory.INFRA,
        ),
        (
            "Privacy Engineer Intern",
            "Stagiaire en protection des renseignements",
            RoleCategory.SECURITY,
        ),
        ("SOC Analyst Intern", "Stagiaire, opérations de sécurité", RoleCategory.SECURITY),
        ("Digital Forensics Intern", "Stagiaire en informatique judiciaire", RoleCategory.SECURITY),
        ("DevSecOps Engineer Intern", "Stagiaire DevSecOps", RoleCategory.SECURITY),
        (
            "Embedded Software Engineer Intern",
            "Stagiaire en logiciel embarqué",
            RoleCategory.EMBEDDED,
        ),
        ("Robotics Engineer Intern", "Stagiaire en robotique", RoleCategory.EMBEDDED),
        ("Controls Engineer Intern", "Stagiaire en systèmes de contrôle", RoleCategory.EMBEDDED),
        ("Computer Science Intern", "Stagiaire en informatique", RoleCategory.GENERAL_CS),
        (
            "Computer Engineering Intern",
            "Stagiaire en génie informatique",
            RoleCategory.GENERAL_CS,
        ),
    ],
)
def test_both_languages_resolve_a_role_to_the_same_category(
    english: str, french: str, role: RoleCategory
) -> None:
    assert classify_role(english).role is role, english
    assert classify_role(french).role is role, french


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Computer Science Intern, Machine Learning", RoleCategory.ML_AI),
        ("Computer Science Intern - Data Science", RoleCategory.DATA),
        ("Computer Science Intern, Cybersecurity", RoleCategory.SECURITY),
        ("Computer Science Intern - DevOps", RoleCategory.INFRA),
        ("Information Technology Intern, Data Analyst", RoleCategory.DATA),
        ("Stagiaire en informatique — cybersécurité", RoleCategory.SECURITY),
        ("Stagiaire en génie informatique, développement logiciel", RoleCategory.SWE),
    ],
)
def test_a_discipline_name_never_outranks_the_specialism_beside_it(
    title: str, role: RoleCategory
) -> None:
    assert classify_role(title).role is role, title


def test_a_discipline_name_loses_even_when_it_is_the_longer_phrase() -> None:
    assert len("computer science") > len("devops"), "the hazard is length, not rarity"
    assert classify_role("Computer Science Intern - DevOps").role is RoleCategory.INFRA


def test_general_cs_is_a_resolved_discipline_not_a_failed_reading() -> None:
    resolved = classify_role("Computer Science Intern")
    assert resolved.role is RoleCategory.GENERAL_CS
    assert resolved.matched, "a resolved category names the phrase it matched"
    assert not classify_role("Summer Analyst").matched, "an unread title matches nothing"


def test_specificity_beats_breadth() -> None:
    assert classify_role("Machine Learning Engineer Intern").role is RoleCategory.ML_AI
    assert classify_role("Data Science Intern").role is RoleCategory.DATA


def test_an_unresolvable_role_is_unknown_and_quarantined() -> None:
    verdict = classify_role("Summer Analyst")
    assert verdict.role is RoleCategory.UNKNOWN
    assert screen_internship("Summer Analyst").is_internship
    assert not _screened("Summer Analyst", "Internship")


def test_a_structured_category_is_used_only_where_it_maps_cleanly() -> None:
    assert classify_role("Intern", source_category="Software").role is RoleCategory.SWE
    assert classify_role("Intern", source_category="Quant").role is RoleCategory.QUANT
    assert classify_role("Intern", source_category="AI/ML/Data").role is RoleCategory.UNKNOWN
    assert classify_role("Intern", source_category="Product").role is RoleCategory.UNKNOWN


def test_the_title_outranks_the_structured_category() -> None:
    resolved = classify_role("Machine Learning Intern", source_category="Software")
    assert resolved.role is RoleCategory.ML_AI


def test_employer_written_french_titles_classify_as_their_english_pair() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    checked = 0
    for pair in pairs:
        english = classify_role(pair["en"]).role
        if english is RoleCategory.UNKNOWN:
            continue
        french = classify_role(pair["fr"]).role
        if french is RoleCategory.UNKNOWN:
            continue
        assert french is english, pair
        checked += 1
    assert checked >= 15, f"only {checked} of {len(pairs)} pairs resolve a role on both sides"


def test_a_retail_trainee_role_is_not_an_internship() -> None:
    for title in (
        "Personal Banking Associate Trainee",
        "Personal Banker Trainee",
        "Branch Manager Trainee - Outremont, Montreal",
        "Assistant Branch Manager Trainee",
        "Manager, Customer Experience Trainee",
        "Management Trainee",
    ):
        assert not screen_internship(title).is_internship, title


def test_a_technical_trainee_role_is_not_an_internship() -> None:
    for title in ("Engineering Trainee", "Graduate Trainee Engineer", "Software Trainee"):
        assert not screen_internship(title).is_internship, title


@pytest.mark.parametrize(
    "title",
    [
        "Full-Time Software Engineering Intern",
        "Senior Software Engineer Intern",
        "Junior Developer Intern",
        "Full Time Data Analyst Intern",
        "Staff Engineer Intern",
        "Stagiaire principal en génie logiciel",
    ],
)
def test_a_seniority_or_hours_word_never_cancels_an_explicit_internship(title: str) -> None:
    assert screen_internship(title).is_internship, title


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Junior Developer",
        "Staff Engineer",
        "Lead Engineer",
        "Principal Engineer",
        "Full-Time Data Analyst",
        "Software Engineer",
        "Développeur logiciel principal",
    ],
)
def test_an_ordinary_job_without_internship_evidence_is_rejected(title: str) -> None:
    assert not screen_internship(title).is_internship, title


@pytest.mark.parametrize(
    "employment_type",
    ["Internship", "Intern", "Co-op", "co op", "Student", "Working Student"],
)
def test_a_trusted_english_employment_type_is_positive_evidence(employment_type: str) -> None:
    verdict = screen_internship("Software Engineer", employment_type)
    assert verdict.is_internship, employment_type
    assert verdict.matched, "structured evidence must name itself"


@pytest.mark.parametrize("employment_type", ["Stage", "Stagiaire", "Alternance", "Étudiant"])
def test_a_trusted_french_employment_type_behaves_identically(employment_type: str) -> None:
    assert screen_internship("Développeur logiciel", employment_type).is_internship


@pytest.mark.parametrize(
    "employment_type",
    [
        "Full-time",
        "FullTime",
        "full",
        "Permanent",
        "Contrat permanent",
        "Part-time",
        "Contract",
        "Temporary",
        "Freelance",
        "",
    ],
)
def test_an_employment_type_that_is_not_an_internship_is_never_evidence(
    employment_type: str,
) -> None:
    assert not screen_internship("Software Engineer", employment_type).is_internship, (
        employment_type
    )


def test_a_structured_signal_cannot_override_an_explicit_disqualifier() -> None:
    for title in ("Internship Program Manager", "New Grad Software Engineer", "Intern Manager"):
        verdict = screen_internship(title, "Internship")
        assert not verdict.is_internship, title
        assert verdict.disqualified_by, title


@pytest.mark.parametrize(
    "title", ["Stage Manager", "Manager Trainee", "Student Services Coordinator"]
)
def test_a_structured_signal_cannot_revive_a_blocked_title_marker(title: str) -> None:
    assert not screen_internship(title, "Internship").is_internship, title


def test_structured_evidence_reaches_the_composed_pipeline() -> None:
    from dataclasses import replace

    from stage.domain import SourceSignals

    generic = _screened("Backend Engineer", "Internship")
    assert generic, "a trusted ATS employment type keeps an otherwise unmarked title"

    permanent = _screened("Backend Engineer", "Full-time")
    assert not permanent, "full-time alone is never internship evidence"

    assert replace(SourceSignals(), employment_type="Internship").employment_type


def _screened(title: str, employment_type: str) -> bool:
    from datetime import UTC, datetime

    from stage.domain import Job, SourceSignals
    from stage.services.sync import normalize_batch

    when = datetime(2026, 8, 8, tzinfo=UTC)
    job = Job(
        id=f"lever:acme:{abs(hash((title, employment_type))) % 10**8}",
        source="lever",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw="",
        description="",
        location_raw="Montreal, QC, Canada",
        first_seen=when,
        last_seen=when,
        signals=SourceSignals(employment_type=employment_type),
    )
    kept, _ = normalize_batch([job])
    return bool(kept)


@pytest.mark.parametrize(
    ("employment_type", "expected"),
    [
        ("Paid Internship | Stage rémunéré", True),
        ("Permanent Full-Time | Permanent temps-plein", False),
        ("Full-time", False),
        ("Contract", False),
        ("fulltime_permanent", False),
        ("fulltime_fixed_term", False),
        ("temporary", False),
        ("freelance", False),
        ("volunteer", False),
        ("fulltime", False),
        ("internship", True),
        ("Stage - temps plein", True),
    ],
)
def test_real_ats_employment_types_are_read_as_the_vendor_writes_them(
    employment_type: str, expected: bool
) -> None:
    assert screen_internship("Backend Engineer", employment_type).is_internship is expected, (
        f"{employment_type!r} is live vendor text, not an enum member"
    )


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Kernel Engineer Intern", RoleCategory.SWE),
        ("Game Networking Engineer Intern", RoleCategory.SWE),
        ("Shader Developer Intern", RoleCategory.SWE),
        ("XR Software Engineer Intern", RoleCategory.SWE),
        ("Endpoint Detection and Response Intern", RoleCategory.SECURITY),
        ("IAM Engineer Intern", RoleCategory.SECURITY),
        ("Quantitative Software Engineer Intern", RoleCategory.QUANT),
        ("Payments Software Engineer Intern", RoleCategory.QUANT),
        ("Data Ingestion Engineer Intern", RoleCategory.DATA),
        ("MLOps Engineer Intern", RoleCategory.INFRA),
        ("Cloud Software Engineer Intern", RoleCategory.INFRA),
        ("GPU Software Engineer Intern", RoleCategory.SWE),
        ("FPGA Developer Intern", RoleCategory.SWE),
        ("Embedded Security Intern", RoleCategory.SECURITY),
        ("Stagiaire ingénieur système", RoleCategory.INFRA),
        ("Stagiaire ingénieure plateforme de données", RoleCategory.DATA),
        ("Stagiaire en cybersécurité automobile", RoleCategory.SECURITY),
        ("Stagiaire ingénieure logicielle quantitative", RoleCategory.QUANT),
        ("Stagiaire développeur de jeux", RoleCategory.SWE),
        ("Stagiaire en rendu temps réel", RoleCategory.SWE),
        ("Stagiaire développeur AR", RoleCategory.SWE),
        ("Stagiaire ingénieure robotique", RoleCategory.EMBEDDED),
        ("Stagiaire en bioinformatique", RoleCategory.DATA),
        ("Stagiaire en calcul parallèle", RoleCategory.INFRA),
        ("Stagiaire en sécurité des conteneurs", RoleCategory.SECURITY),
        ("Stagiaire développeuse de contrats intelligents", RoleCategory.QUANT),
        ("SDE Intern", RoleCategory.SWE),
        ("Distributed Storage Intern", RoleCategory.SWE),
        ("Operations Research Intern", RoleCategory.QUANT),
        ("Scientific Computing Intern", RoleCategory.INFRA),
        ("Vulnerability Research Intern", RoleCategory.SECURITY),
        ("Quant Research Intern", RoleCategory.QUANT),
        ("Simulation Engineer Intern", RoleCategory.SWE),
        ("Augmented Reality Intern", RoleCategory.SWE),
        ("Edge Computing Intern", RoleCategory.INFRA),
        ("Stagiaire en recherche sur les vulnérabilités", RoleCategory.SECURITY),
        ("Stagiaire en calcul scientifique", RoleCategory.INFRA),
        ("Stagiaire en réalité augmentée", RoleCategory.SWE),
    ],
)
def test_expanded_english_and_french_role_families(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


@pytest.mark.parametrize(
    "employment_type",
    ["Apprenticeship", "Trainee", "Contrat d'apprentissage"],
)
def test_apprenticeship_and_trainee_employment_types_are_not_internships(
    employment_type: str,
) -> None:
    assert not screen_internship("Software Developer", employment_type).is_internship


@pytest.mark.parametrize(
    "title",
    ["Contrat de professionnalisation — Développeuse logiciel"],
)
def test_french_work_study_contracts_are_recognized(title: str) -> None:
    assert screen_internship(title).is_internship


@pytest.mark.parametrize(
    "title",
    [
        "Jeune diplômée — Développeuse logiciel",
        "Diplômée récente — Développeuse logiciel",
        "Nouveau diplômé — Ingénieur logiciel",
    ],
)
def test_french_new_grad_titles_are_not_internships(title: str) -> None:
    assert not screen_internship(title).is_internship


@pytest.mark.parametrize(
    "title",
    [
        "Jeune diplômée — Software Engineer Intern",
        "Intern to Entry Level Conversion Intern Program - Engineering",
        "Technical Intern and New Grad",
        "Entry-Level Software Engineer - Internship - Fresh Graduate",
    ],
)
def test_a_cohort_word_never_cancels_an_explicit_internship(title: str) -> None:
    assert screen_internship(title).is_internship, (
        f"{title!r}: a cohort label loses to an explicit marker, as a seniority word already does"
    )


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer - Entry Level 2027",
        "New Graduate Engineer, Software",
        "Software Engineer, New Grad",
        "Recent Graduate - Trading Assistant",
        "Graduate Program Ingenieros",
    ],
)
def test_a_cohort_word_still_rejects_when_nothing_says_internship(title: str) -> None:
    assert not screen_internship(title).is_internship, (
        "moving these off the unconditional list must not admit ordinary new-grad postings"
    )


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Accessibility Engineer Intern", RoleCategory.SWE),
        ("Search Software Engineer Intern", RoleCategory.SWE),
        ("Cloud Security Architect Intern", RoleCategory.SECURITY),
        ("AI Security Engineer Intern", RoleCategory.SECURITY),
        ("Data Streaming Engineer Intern", RoleCategory.DATA),
        ("Data Mesh Engineer Intern", RoleCategory.DATA),
        ("LLM Platform Engineer Intern", RoleCategory.ML_AI),
        ("AI Evaluation Engineer Intern", RoleCategory.ML_AI),
        ("Trading Infrastructure Engineer Intern", RoleCategory.QUANT),
        ("Order Gateway Engineer Intern", RoleCategory.QUANT),
        ("Software Defined Networking Engineer Intern", RoleCategory.INFRA),
        ("Kubernetes Platform Engineer Intern", RoleCategory.INFRA),
        ("Design for Test Engineer Intern", RoleCategory.SWE),
        ("Robotics Perception Engineer Intern", RoleCategory.EMBEDDED),
        ("Firmware Validation Engineer Intern", RoleCategory.EMBEDDED),
        ("Stagiaire ingénieur accessibilité", RoleCategory.SWE),
        ("Stagiaire architecte sécurité infonuagique", RoleCategory.SECURITY),
        ("Stagiaire ingénieure diffusion de données", RoleCategory.DATA),
        ("Stagiaire ingénieure plateforme IA", RoleCategory.ML_AI),
        ("Stagiaire ingénieure infrastructure de négociation", RoleCategory.QUANT),
        ("Stagiaire ingénieure réseaux définis par logiciel", RoleCategory.INFRA),
        ("Stagiaire ingénieure perception robotique", RoleCategory.EMBEDDED),
    ],
)
def test_additional_cs_adjacent_specialties_resolve(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Co-op",
        "Stage coopératif — Développeur logiciel",
        "Programme coopératif — Ingénieure logiciel",
    ],
)
def test_english_and_french_coop_titles_are_internships(title: str) -> None:
    assert screen_internship(title).is_internship


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("DFIR Intern", RoleCategory.SECURITY),
        ("API Gateway Engineer Intern", RoleCategory.INFRA),
        ("Data Migration Engineer Intern", RoleCategory.DATA),
        ("RAG Engineer Intern", RoleCategory.ML_AI),
        ("Trading Infrastructure Intern", RoleCategory.QUANT),
        ("ML Compiler Intern", RoleCategory.SWE),
        ("Industrial IoT Engineer Intern", RoleCategory.EMBEDDED),
        ("Game Server Engineer Intern", RoleCategory.SWE),
        ("Stagiaire en reponse aux incidents et investigation numerique", RoleCategory.SECURITY),
        ("Stagiaire ingenieure passerelle API", RoleCategory.INFRA),
        ("Stagiaire en migration de donnees", RoleCategory.DATA),
        ("Stagiaire ingenieur RAG", RoleCategory.ML_AI),
        ("Stagiaire infrastructure de negociation", RoleCategory.QUANT),
        ("Stagiaire compilateur AA", RoleCategory.SWE),
        ("Stagiaire ingenieure IoT industriel", RoleCategory.EMBEDDED),
        ("Stagiaire systemes multijoueurs", RoleCategory.SWE),
    ],
)
def test_new_specialist_role_families_resolve(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Co-operative Education",
        "Software Developer Co-op Placement",
        "Poste coop - Developpeur logiciel",
        "Programme de formation cooperative - Ingenieure logiciel",
    ],
)
def test_additional_english_and_french_coop_markers_are_internships(title: str) -> None:
    assert screen_internship(title).is_internship


def test_fellowship_without_an_internship_or_coop_marker_is_not_an_internship() -> None:
    assert not screen_internship("Software Engineering Fellowship").is_internship


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Application Development Intern", RoleCategory.SWE),
        ("Forward Deployed Engineering Intern", RoleCategory.SWE),
        ("AI Compiler and Library Engineer Intern", RoleCategory.SWE),
        ("GIS Analyst Intern", RoleCategory.DATA),
        ("AI Inference Intern", RoleCategory.ML_AI),
        ("Hunyuan Multimodal Algorithm Researcher Intern", RoleCategory.ML_AI),
        ("AI Agent Builder Intern", RoleCategory.ML_AI),
        ("AI R&D Engineer Co-op", RoleCategory.ML_AI),
        ("AI Solutions Engineer Intern", RoleCategory.ML_AI),
        ("Supercomputing Intern", RoleCategory.INFRA),
        ("CDN Platform Engineer Intern", RoleCategory.INFRA),
        ("AI RAN Telecommunications Engineer Intern", RoleCategory.INFRA),
        ("Flight Software Intern", RoleCategory.EMBEDDED),
        ("Working Student Interior Sensing ML", RoleCategory.EMBEDDED),
        ("Stagiaire en développement d applications", RoleCategory.SWE),
        ("Stagiaire ingénieure solutions IA", RoleCategory.ML_AI),
        ("Stagiaire en superinformatique", RoleCategory.INFRA),
        ("Stagiaire ingénieure logicielle de vol", RoleCategory.EMBEDDED),
    ],
)
def test_reviewed_live_role_gaps_resolve(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Data Analysis Intern", RoleCategory.DATA),
        ("Data & Analytics Intern", RoleCategory.DATA),
        ("Data & AI Intern - Analyst", RoleCategory.DATA),
        ("Data Integration & Reporting Intern", RoleCategory.DATA),
        ("Development Tools Software Intern", RoleCategory.SWE),
        ("Energy System Optimization Intern - Energy Optimization Software", RoleCategory.SWE),
        ("Enterprise AI Intern", RoleCategory.ML_AI),
        ("Student Researcher Intern - AI Foundation Models Infrastructure", RoleCategory.ML_AI),
        ("Robot Learning Engineer Intern", RoleCategory.ML_AI),
        ("Video Algorithms Intern - Video Coding - Gaussian Splatting", RoleCategory.ML_AI),
        ("Software/ML Engineering Intern", RoleCategory.ML_AI),
        ("Vehicle Software Intern - Vehicle Controls", RoleCategory.EMBEDDED),
        ("Stagiaire en analyse de données", RoleCategory.DATA),
        ("Stagiaire en modèles fondamentaux", RoleCategory.ML_AI),
        ("Stagiaire ingénieure logicielle de véhicule", RoleCategory.EMBEDDED),
    ],
)
def test_additional_live_data_ai_and_vehicle_titles_resolve(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Software Engineer Intern - AI Infrastructure", RoleCategory.SWE),
        ("AI Infrastructure Software Engineer Intern", RoleCategory.INFRA),
    ],
)
def test_tied_role_phrases_choose_the_earliest_title_match(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Java Web Development Intern", RoleCategory.SWE),
        ("Software Analyst Intern", RoleCategory.SWE),
        ("Aerospace Software Apps Engineer Intern", RoleCategory.SWE),
        ("Mission Software Intern", RoleCategory.SWE),
        ("Intern - Engineering - Software & Gaming", RoleCategory.SWE),
        ("AI Agents & Automations Internship", RoleCategory.ML_AI),
        ("AI and SW Development Engineering Intern", RoleCategory.ML_AI),
        ("AI and domain-aware audio Processing Intern", RoleCategory.ML_AI),
        ("Research Intern - AI for Scientific Reasoning", RoleCategory.ML_AI),
        ("Research Intern - Video World Models", RoleCategory.ML_AI),
        ("Intern - Infrared Imaging & Algorithms", RoleCategory.ML_AI),
        ("Database Engineering Intern", RoleCategory.INFRA),
        ("AI for Intent-Based Networking Internship", RoleCategory.INFRA),
        ("Internship - AI for Functional Avionics", RoleCategory.EMBEDDED),
        ("Stagiaire développement web Java", RoleCategory.SWE),
        ("Stagiaire ingénieur d'applications logicielles aérospatiales", RoleCategory.SWE),
        ("Stagiaire agents et automatisations IA", RoleCategory.ML_AI),
        ("Stagiaire ingénierie de bases de données", RoleCategory.INFRA),
        ("Stagiaire IA pour l'avionique fonctionnelle", RoleCategory.EMBEDDED),
        ("Stagiaire plateforme infonuagique sécurisée", RoleCategory.SECURITY),
    ],
)
def test_audited_technical_role_gaps_resolve(title: str, role: RoleCategory) -> None:
    assert classify_role(title).role is role, title


def test_a_publisher_level_category_overrides_a_wrong_employment_type() -> None:
    verdict = screen_internship(
        "Quantitative Systematic Trader - Experienced Hire",
        employment_type="INTERN",
        category="Experienced Professionals",
    )
    assert not verdict.is_internship, "a board that stamps INTERN on every row is not evidence"
    assert verdict.disqualified_by == "experienced professionals"


def test_a_new_graduate_category_is_not_an_internship() -> None:
    verdict = screen_internship("Quantitative Trader - Graduate: 2027", "INTERN", "New Graduates")
    assert not verdict.is_internship


def test_a_seniority_title_outweighs_a_structured_internship_type() -> None:
    verdict = screen_internship("Member of Technical Staff 4- Dev Extension", "Internship")
    assert not verdict.is_internship, "structured-only evidence cannot outrank a seniority grade"


def test_a_seniority_word_never_blocks_a_title_that_says_internship() -> None:
    assert screen_internship("Senior Data Science Intern", "Internship").is_internship, (
        "the title's own internship marker is stronger evidence than a grade word"
    )


def test_a_normal_category_still_leaves_the_structured_type_alone() -> None:
    assert screen_internship(
        "Software Dev Engineer Intern", "FullTime", "Software Development"
    ).is_internship
