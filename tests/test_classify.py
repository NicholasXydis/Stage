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
        "Alternance Développeur Full Stack",
        "Apprentice Software Engineer",
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


def test_a_genuine_french_apprenticeship_is_still_recognized() -> None:
    assert screen_internship("Apprenti développeur logiciel").is_internship
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
    }, (
        "only 17 of 8,944 described rows carry a body-only internship phrase, so a "
        "description parameter would advertise a capability that does not exist"
    )


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
        ("FPGA Design Intern", RoleCategory.HARDWARE),
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
            "Electrical Engineering Intern",
            "Stagiaire en génie électrique",
            RoleCategory.HARDWARE,
        ),
        ("Robotics Engineer Intern", "Stagiaire en robotique", RoleCategory.EMBEDDED),
        ("Controls Engineer Intern", "Stagiaire en systèmes de contrôle", RoleCategory.EMBEDDED),
        ("Computer Science Intern", "Stagiaire en informatique", RoleCategory.GENERAL_CS),
        (
            "Computer Engineering Intern",
            "Stagiaire en génie informatique",
            RoleCategory.GENERAL_CS,
        ),
        (
            "Information Technology Intern",
            "Stagiaire en technologies de l'information",
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


def test_an_unresolvable_role_is_unknown_and_the_posting_is_kept() -> None:
    verdict = classify_role("Summer Analyst")
    assert verdict.role is RoleCategory.UNKNOWN
    assert screen_internship("Summer Analyst").is_internship, "still carried"


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
    assert checked >= 15, (
        f"only {checked} of {len(pairs)} pairs exercised the cross-language path; the floor "
        "was 3 while the lexicon resolved 3, which let a parity gap read as coverage"
    )


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


def test_a_technical_trainee_role_still_counts() -> None:
    for title in ("Engineering Trainee", "Graduate Trainee Engineer", "Software Trainee"):
        assert screen_internship(title).is_internship, title


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
    ["Internship", "Intern", "Co-op", "co op", "Student", "Apprentice", "Working Student"],
)
def test_a_trusted_english_employment_type_is_positive_evidence(employment_type: str) -> None:
    verdict = screen_internship("Software Engineer", employment_type)
    assert verdict.is_internship, employment_type
    assert verdict.matched, "structured evidence must name itself"


@pytest.mark.parametrize(
    "employment_type", ["Stage", "Stagiaire", "Alternance", "Apprenti", "Étudiant"]
)
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
        f"{employment_type!r} is a value observed on a live board; an exact-match rule read "
        "Lever's bilingual free text as no evidence at all"
    )
