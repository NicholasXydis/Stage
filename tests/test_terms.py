import json
from datetime import UTC, datetime
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
        ("Cybersecurity Internship Summer2027 - Federal", "summer-2027"),
        ("Stagiaire Hiver2027", "winter-2027"),
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
        "posted",
    }
    assert resolve_term(title="Fall Intern", pivot_year=2026).term == UNKNOWN_TERM
    assert resolve_term(title="Fall Intern", posted=datetime(2026, 11, 5, tzinfo=UTC)).season == (
        "fall"
    ), "a posting date only drops a stale label and never names a year"


def test_a_two_digit_year_needs_a_pivot_and_stays_near_it() -> None:
    assert resolve_term(title="Summer '27 Intern", pivot_year=2026).term == "summer-2027"
    assert resolve_term(title="Summer '27 Intern").term == UNKNOWN_TERM
    assert resolve_term(title="Summer '99 Intern", pivot_year=2026).term == UNKNOWN_TERM
    assert resolve_term(title="Spring 30 Hours Intern", pivot_year=2026).term == UNKNOWN_TERM
    booz = "University \u2013 Summer 27, Cybersecurity Analyst Intern"
    assert resolve_term(title=booz, pivot_year=2026).term == "summer-2027"


def test_a_short_year_in_a_description_is_never_read() -> None:
    body = "Spring 26 weeks of hands-on work."
    assert resolve_term(description=body, pivot_year=2026).term == UNKNOWN_TERM


@pytest.mark.parametrize(
    ("posted", "term"),
    [
        (datetime(2026, 11, 5, tzinfo=UTC), UNKNOWN_TERM),
        (datetime(2026, 3, 1, tzinfo=UTC), "summer-2026"),
        (None, "summer-2026"),
    ],
)
def test_a_feed_label_for_a_term_over_before_the_posting_went_up_is_dropped(
    posted: datetime | None, term: str
) -> None:
    resolved = resolve_term(structured_terms=("Summer 2026",), posted=posted)
    assert (resolved.term, resolved.season) == (term, "summer")


def test_a_title_keeps_the_term_it_names_whatever_the_posting_date() -> None:
    posted = datetime(2026, 11, 5, tzinfo=UTC)
    assert resolve_term(title="Summer 2026 Intern", posted=posted).term == "summer-2026"


def test_the_employer_written_bilingual_pair_resolves_identically() -> None:
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    dated = [pair for pair in pairs if resolve_term(title=pair["en"]).term != UNKNOWN_TERM]
    assert dated, "no fixture pair carries a resolvable term"
    for pair in dated:
        assert resolve_term(title=pair["en"]).term == resolve_term(title=pair["fr"]).term, pair


@pytest.mark.parametrize(
    "description",
    [
        "Graduating in Spring 2028 or later, currently pursuing a Bachelor's degree",
        "Expected graduation date: Fall 2028",
        "Diplômé en hiver 2028",
        "Pursuing a BS in Computer Science and enrolled in Fall 2027 classes.",
        "Computer Science or related field (grad date: Dec 2027 \u2013 Spring 2028)",
        "Undergraduate students at North American universities with Spring 2028 graduation dates.",
        "A technical field with an expected graduation date in Winter 2027/Spring 2028",
        "Graduating in December 2027, anytime 2028, or spring 2029",
    ],
)
def test_a_graduation_date_in_the_body_is_not_the_internship_term(description: str) -> None:
    resolved = resolve_term(
        title="Software Engineer, Intern - Campus Recruiting 2027", description=description
    )
    assert resolved.term == UNKNOWN_TERM, "a graduation date is not when the internship runs"


def test_the_body_still_names_a_term_beside_a_graduation_date() -> None:
    body = "This is a Summer 2027 internship for students graduating in Spring 2028."
    assert resolve_term(title="Software Engineer Intern", description=body).term == "summer-2027"


@pytest.mark.parametrize(
    ("title", "season"),
    [
        ("Undergrad Software Engineering Intern (Grad Year: Spring 2028)", ""),
        ("Software Intern - Students Graduating Summer 2028 or December 2027", ""),
        ("Software Intern (Expected graduation: Winter 2027)", ""),
        ("Software Intern Summer 2027 (graduating in Spring 2028)", "summer"),
        ("Intern Rd Graduate Summer Computer Science Research Institute", "summer"),
        ("Software Engineer (New Grad Summer 2027)", "summer"),
        ("Software Engineering Intern (Spring 2028 Graduates)", ""),
        ("Spring 2027 Grads Software Intern", ""),
        ("Software Intern - Summer 2027 (Graduating 2028)", "summer"),
        ("Summer 2027 Graduate Software Intern", "summer"),
    ],
)
def test_a_title_naming_when_students_graduate_is_not_the_term(title: str, season: str) -> None:
    assert resolve_term(title=title, pivot_year=2026).season == season


@pytest.mark.parametrize(
    ("title", "term"),
    [
        ("2027 Guardian Summer Intern, Digital & Technology - Data Engineering", "summer-2027"),
        ("2027 Cybersecurity Summer Intern", "summer-2027"),
        ("Software Developer Intern, MyAdmin (Winter/January 2027, 8+ Months)", "winter-2027"),
        ("Research Co-op - Winter Term- January 2027 - April", "winter-2027"),
        ("Summer Intern (Class of 2028)", UNKNOWN_TERM),
        ("Summer Intern - 2028 Graduates", UNKNOWN_TERM),
        ("Summer Intern 2026-2027", UNKNOWN_TERM),
        ("Fall and Winter Intern 2027", UNKNOWN_TERM),
    ],
)
def test_one_season_and_one_year_apart_in_a_title_name_the_term(title: str, term: str) -> None:
    assert resolve_term(title=title, pivot_year=2026).term == term


@pytest.mark.parametrize(
    ("title", "term"),
    [
        ("Stage en développement - janvier à avril 2027", "winter-2027"),
        ("Software Intern (May - August 2027)", "summer-2027"),
        ("Software Intern September to December 2027", "fall-2027"),
        ("Software Intern January 2027 - April 2027", "winter-2027"),
        ("Software Intern May 2027 - December 2027", UNKNOWN_TERM),
        ("Stage H27 - Développeur", "winter-2027"),
        ("Co-op É27 developer", "summer-2027"),
        ("Model A26 sensor engineer", UNKNOWN_TERM),
        ("Software Intern Winter 2026-2027", "winter-2027"),
        ("Fall/Winter 2026 Intern", UNKNOWN_TERM),
        ("Spring & Summer 2027 Intern", UNKNOWN_TERM),
        ("Summer 2027 / Été 2027 Intern", "summer-2027"),
        ("Stagiaire développeur logiciel (H27)", "winter-2027"),
        ("Data Intern - Building A25", UNKNOWN_TERM),
        ("Spring 2027 Co-op (Jan - Apr 2027)", "spring-2027"),
        ("Software Co-op - Winter 2027 - 2028 Graduates", "winter-2027"),
        ("Class of 2028 Summer Intern", UNKNOWN_TERM),
        ("Summer Intern (2028 Grads)", UNKNOWN_TERM),
        ("Summer 2027 Intern - Silver Spring, MD", "summer-2027"),
        ("Summer 2027 Intern - Spring, TX", "summer-2027"),
        ("Fall 2027 Intern - Winter Haven, FL", "fall-2027"),
        ("Java/Spring Developer Intern Summer 2027", "summer-2027"),
        ("Summer or Fall 2027 Intern", UNKNOWN_TERM),
    ],
)
def test_titles_name_terms_by_month_range_code_or_season(title: str, term: str) -> None:
    assert resolve_term(title=title, pivot_year=2026).term == term


def test_a_term_ended_before_the_posting_is_not_taken_from_the_body() -> None:
    posted = datetime(2026, 10, 1, tzinfo=UTC)
    body = "Recruiting for the winter 2021 session is complete."
    assert resolve_term(description=body, posted=posted).term == UNKNOWN_TERM


def test_a_stored_term_named_only_as_a_graduation_date_is_found() -> None:
    from stage.normalize.terms import graduation_only

    assert graduation_only("spring-2028", "Software Intern (Grad Year: Spring 2028)", "", 2026)
    assert not graduation_only("summer-2027", "Software Intern Summer 2027", "", 2026)


def test_fall_ends_in_december_and_winter_only_after_the_next_april() -> None:
    from stage.normalize.terms import ended_before

    assert ended_before("fall-2025", datetime(2026, 1, 2, tzinfo=UTC))
    assert not ended_before("winter-2026", datetime(2026, 11, 2, tzinfo=UTC))
    assert ended_before("winter-2021", datetime(2026, 11, 2, tzinfo=UTC))
