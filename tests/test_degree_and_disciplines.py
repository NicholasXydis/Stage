from datetime import UTC, datetime

import pytest

from stage.classify import screen_degree_scope, screen_is_cs_role
from stage.domain import Job, RejectionReason

NOW = datetime(2026, 8, 15, tzinfo=UTC)


def _job(title: str, *, role: str = "unknown") -> Job:
    from stage.domain import RoleCategory

    return Job(
        id=f"greenhouse:acme:{abs(hash(title))}",
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw="https://boards.greenhouse.io/acme/jobs/1",
        description="",
        first_seen=NOW,
        last_seen=NOW,
        role=RoleCategory(role),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Electrical Engineering Intern",
        "Mechanical Engineer Intern",
        "Civil Engineering Internship",
        "R&D Materials Engineer Intern",
        "CNC Machine Park Intern",
    ],
)
def test_a_non_cs_engineering_discipline_is_rejected(title: str) -> None:
    rejection = screen_is_cs_role(_job(title))
    assert rejection is not None, f"{title} is not a CS internship"
    assert rejection.reason is RejectionReason.NOT_A_CS_ROLE


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering | Mechanical Engineering internship",
    ],
)
def test_a_discipline_title_that_also_names_a_cs_role_survives(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is None, f"{title} is reachable by a CS student"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Masters Intern",
        "Analog IC Design Intern - Master's Degree",
        "Computing Graduate Student Intern",
        "Software Developer Intern, MS, Summer 2027",
        "Embedded Software Intern - MSc Graduation Project",
    ],
)
def test_a_graduate_only_internship_is_rejected(title: str) -> None:
    rejection = screen_degree_scope(_job(title))
    assert rejection is not None, f"{title} is closed to an undergraduate"
    assert rejection.reason is RejectionReason.OUT_OF_SCOPE_DEGREE


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern - Security-Data - BS/MS",
        "Campus Quantitative Researcher, UG/MS (Intern)",
        "Quantitative Research Analyst Bachelor's or Master's",
        "2026 BSc/MSc/PhD Quantitative Research Intern",
    ],
)
def test_a_posting_that_also_admits_undergraduates_survives(title: str) -> None:
    assert screen_degree_scope(_job(title)) is None, f"{title} names an undergraduate path"


@pytest.mark.parametrize(
    "title",
    [
        "Business Analyst (BA) Junior",
        "Agile Scrum Master - Krakow",
        "Master Data Intern - Service & Support",
        "Production Master Scheduler",
    ],
)
def test_a_degree_lookalike_is_not_a_degree(title: str) -> None:
    assert screen_degree_scope(_job(title, role="swe")) is None, (
        f"{title} carries no degree requirement; BA is a job title and Master is not a degree here"
    )


def _with(title: str, *, description: str = "", category: str = "") -> Job:
    from dataclasses import replace

    from stage.domain import SourceSignals

    return replace(
        _job(title),
        description=description,
        signals=SourceSignals(category=category),
    )


SOFTWARE_BODY = "You will build backend services in Python and ship software daily."


def test_a_description_alone_does_not_make_a_posting_a_cs_role() -> None:
    rejection = screen_is_cs_role(_with("Sales Intern", description=SOFTWARE_BODY))
    assert rejection is not None, "every employer's body text mentions software"
    assert rejection.reason is RejectionReason.UNKNOWN_CS_ROLE


def test_a_title_still_confers_a_cs_role() -> None:
    assert screen_is_cs_role(_with("Software Engineer Intern")) is None


def test_a_source_category_still_confers_a_cs_role() -> None:
    assert screen_is_cs_role(_with("Backend Intern", category="Software")) is None, (
        "a structured category is the publisher's own statement, unlike prose"
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Robotics Intern", "embedded"),
        ("Stagiaire en robotique", "embedded"),
        ("Web Development Intern", "swe"),
        ("Stagiaire en développement web", "swe"),
        ("AI R&D Engineering Co-op", "ml-ai"),
        ("Stagiaire en algorithme", "swe"),
    ],
)
def test_the_masked_title_gaps_are_closed(title: str, expected: str) -> None:
    from stage.classify import classify_role

    assert classify_role(title, "").role.value == expected, title


@pytest.mark.parametrize(
    "title",
    [
        "Analog/Mixed-Signal IC Design Co-Op/Intern",
        "ASIC Design Engineer Intern",
        "Hardware Engineer Co-op",
        "RFIC Design Intern",
        "Physical Design Engineer Intern",
    ],
)
def test_a_role_a_cs_undergraduate_cannot_hold_is_rejected(title: str) -> None:
    from stage.classify import classify_role

    assert classify_role(title, "").role.value == "unknown", (
        f"{title} is chip or circuit work, not a CS internship"
    )


@pytest.mark.parametrize("title", ["Thermal Controls Engineer Intern", "Sales and Trading Intern"])
def test_a_business_or_thermal_title_is_screened_out(title: str) -> None:
    assert screen_is_cs_role(_job(title)) is not None, f"{title} is not a CS internship"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Embedded Software Engineer Intern", "embedded"),
        ("Firmware Engineering Intern", "embedded"),
        ("Robotics Software Intern", "embedded"),
        ("AI Infra Intern", "infra"),
        ("Stagiaire en infrastructure IA", "infra"),
        ("Research Intern - Model Shaping", "ml-ai"),
        ("Product Engineer Intern, Agent Systems", "ml-ai"),
    ],
)
def test_embedded_software_and_the_new_phrases_survive(title: str, expected: str) -> None:
    from stage.classify import classify_role

    assert classify_role(title, "").role.value == expected, (
        f"{title} is software a CS undergraduate can hold"
    )
