
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


def test_the_internship_screen_reads_only_the_title() -> None:
    import inspect

    assert set(inspect.signature(screen_internship).parameters) == {"title"}


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
    assert checked >= 3, f"only {checked} pairs exercised the cross-language path"
