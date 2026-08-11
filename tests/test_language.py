import json
from pathlib import Path

import pytest

from stage.domain import Language
from stage.normalize import detect_language

FIXTURE = Path(__file__).parent / "fixtures" / "bilingual_titles.json"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Intern",
        "Full Stack Developer Intern",
        "Data Analyst Intern - Montréal",
        "Software Engineer Intern, Montréal",
        "Machine Learning Intern, Summer 2027",
    ],
)
def test_english_titles(title: str) -> None:
    assert detect_language(title).language is Language.EN


def test_a_french_place_name_does_not_make_a_posting_french() -> None:
    assert detect_language("Data Analyst Intern - Montréal").language is Language.EN
    assert detect_language("Software Engineer Intern, Québec City").language is Language.EN


@pytest.mark.parametrize(
    "title",
    [
        "Stagiaire en Développement Logiciel",
        "Concepteur de niveaux",
        "Stage de 4 mois en informatique",
        "Développeur·euse Java Senior",
        "Analyste financier·e Sénior·e",
        "Stagiaire en science des données",
    ],
)
def test_french_titles(title: str) -> None:
    assert detect_language(title).language is Language.FR


@pytest.mark.parametrize(
    "title",
    [
        "Software Developer Intern - Stagiaire en Développement Logiciel",
        "Programming Intern Fall 2026 / Stagiaire en Programmation Automne 2026",
    ],
)
def test_bilingual_titles_are_neither_english_nor_french(title: str) -> None:
    assert detect_language(title).language is Language.BILINGUAL
    assert detect_language("Supercomputing Intern").language is Language.UNKNOWN


@pytest.mark.parametrize(
    "title",
    [
        "Stagiaire",
        "Supercomputing Intern",
        "FPGA Intern",
        "Intern",
        "Architecture Intern",
        "Backend Engineer",
        "Level Designer",
    ],
)
def test_thin_titles_stay_unknown_rather_than_guess(title: str) -> None:
    assert detect_language(title).language is Language.UNKNOWN


def test_evidence_is_reported_for_every_verdict() -> None:
    detected = detect_language("Stagiaire en Développement Logiciel")
    assert "stagiaire" in detected.french_hits
    assert detected.english_hits == ()


def test_loanwords_do_not_make_a_french_posting_bilingual() -> None:
    detected = detect_language("Développeur Full Stack sénior, équipe de données")
    assert detected.language is Language.FR


def test_the_description_does_not_outvote_the_title() -> None:
    detected = detect_language(
        "Stagiaire en Développement Logiciel",
        "We are an equal opportunity employer and value diversity at our company.",
    )
    assert detected.language is Language.FR


def test_every_harvested_pair_detects_as_expected() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    for pair in pairs:
        english = detect_language(pair["en"]).language
        french = detect_language(pair["fr"]).language
        assert english in (Language.EN, Language.UNKNOWN), pair["en"]
        assert french in (Language.FR, Language.UNKNOWN), pair["fr"]


def test_the_full_bilingual_title_beats_either_half() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    bilingual = [
        pair for pair in pairs if detect_language(pair["title_raw"]).language is Language.BILINGUAL
    ]
    assert len(bilingual) >= 4, f"only {len(bilingual)} of {len(pairs)} titles resolve bilingual"
