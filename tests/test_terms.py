import json
from pathlib import Path

import pytest

from stage.domain import UNKNOWN_TERM
from stage.normalize import resolve_term

FIXTURE = Path(__file__).parent / "fixtures" / "bilingual_titles.json"


@pytest.mark.parametrize(
    ("title", "term"),
    [
        ("Software Engineer Intern, Summer 2027", "summer-2027"),
        ("Fall 2026 Software Engineering Intern", "fall-2026"),
        ("2027 Summer Analyst Program", "summer-2027"),
        ("Software Engineering Intern (Spring 2027)", "spring-2027"),
        ("Winter 2027 Co-op — Backend", "winter-2027"),
        ("Data Science Intern - Summer of 2026", "summer-2026"),
        ("Intern, Fall term 2026", "fall-2026"),
    ],
)
def test_season_and_year_in_a_title(title: str, term: str) -> None:
    assert resolve_term(title=title).term == term


@pytest.mark.parametrize(
    ("title", "term"),
    [
        ("Stagiaire en Programmation Automne 2026", "fall-2026"),
        ("Stage d'été 2027 — Développement logiciel", "summer-2027"),
        ("Stagiaire hiver 2027", "winter-2027"),
        ("Stage printemps 2027", "spring-2027"),
        ("Stagiaire, session d'hiver 2027", "winter-2027"),
    ],
)
def test_french_seasons(title: str, term: str) -> None:
    assert resolve_term(title=title).term == term


@pytest.mark.parametrize(
    "title",
    [
        "Spring Boot Developer",
        "Senior Spring Framework Engineer",
        "Spring Security Consultant",
        "Backend Engineer (Spring MVC)",
        "Spring Health — Data Scientist",
    ],
)
def test_spring_the_framework_is_not_a_season(title: str) -> None:
    resolved = resolve_term(title=title)
    assert resolved.term == UNKNOWN_TERM
    assert resolved.season == ""


@pytest.mark.parametrize(
    "title",
    ["Summer Analyst", "Fall Intern", "Software Engineer Intern", "Stagiaire en génie logiciel"],
)
def test_a_season_without_a_year_is_never_a_term(title: str) -> None:
    assert resolve_term(title=title).term == UNKNOWN_TERM


def test_an_unpaired_season_is_kept_as_evidence() -> None:
    resolved = resolve_term(title="Summer Analyst")
    assert resolved.term == UNKNOWN_TERM
    assert resolved.season == "summer"
    assert resolved.year is None


@pytest.mark.parametrize(
    ("terms", "expected"),
    [
        (["Summer 2027"], "summer-2027"),
        (["Fall 2026"], "fall-2026"),
        (["N/A"], UNKNOWN_TERM),
        ([], UNKNOWN_TERM),
        (["Summer 2026", "Fall 2026"], UNKNOWN_TERM),
    ],
)
def test_structured_terms(terms: list[str], expected: str) -> None:
    assert resolve_term(structured_terms=terms).term == expected


def test_the_structured_field_wins_over_a_silent_title() -> None:
    resolved = resolve_term(structured_terms=["Summer 2027"], title="Software Engineer Intern")
    assert resolved.term == "summer-2027"


def test_a_structured_title_conflict_resolves_to_unknown_and_is_flagged() -> None:
    resolved = resolve_term(structured_terms=["Summer 2027"], title="Fall 2026 Intern")
    assert resolved.term == UNKNOWN_TERM
    assert resolved.conflict is True


def test_the_description_fills_silence_but_never_contradicts() -> None:
    filled = resolve_term(title="Intern", description="starting in the summer of 2026")
    assert filled.term == "summer-2026"
    assert filled.conflict is False

    unchallenged = resolve_term(
        structured_terms=["Summer 2027"], description="our Summer 2025 cohort delivered a lot"
    )
    assert unchallenged.term == "summer-2027"
    assert unchallenged.conflict is False


def test_a_season_with_no_year_from_a_feed_stays_unknown() -> None:
    resolved = resolve_term(structured_season="Fall")
    assert resolved.term == UNKNOWN_TERM
    assert resolved.season == "fall"


def test_no_clock_is_a_term_source() -> None:
    import inspect

    parameters = set(inspect.signature(resolve_term).parameters)
    assert parameters == {
        "title",
        "description",
        "structured_terms",
        "structured_season",
        "pivot_year",
    }
    assert resolve_term(title="Fall Intern", pivot_year=2026).term == UNKNOWN_TERM


def test_a_two_digit_year_needs_a_pivot_and_stays_near_it() -> None:
    assert resolve_term(title="Summer '27 Intern", pivot_year=2026).term == "summer-2027"
    assert resolve_term(title="Summer '27 Intern").term == UNKNOWN_TERM
    assert resolve_term(title="Summer '99 Intern", pivot_year=2026).term == UNKNOWN_TERM


def test_the_employer_written_bilingual_pair_resolves_identically() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    dated = [pair for pair in pairs if resolve_term(title=pair["en"]).term != UNKNOWN_TERM]
    assert dated, "no fixture pair carries a resolvable term"
    for pair in dated:
        assert resolve_term(title=pair["en"]).term == resolve_term(title=pair["fr"]).term, pair
