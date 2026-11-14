from datetime import UTC, datetime

import pytest

from stage.classify.level import screen_level
from stage.domain import Job, JobLevel, RejectionReason, SourceSignals
from stage.lexicon import LexiconError, level_lexicon
from stage.services.screening import normalize_batch

NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _job(
    title: str,
    description: str = "",
    *,
    location: str = "Montreal, QC, Canada",
    employment_type: str = "",
    url: str = "https://example.test/1",
) -> Job:
    return Job(
        id=f"greenhouse:acme:{abs(hash((title, description)))}",
        source="greenhouse",
        company="Acme",
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=url,
        description=description,
        location_raw=location,
        first_seen=NOW,
        last_seen=NOW,
        signals=SourceSignals(employment_type=employment_type),
    )


@pytest.mark.parametrize(
    ("title", "level", "phrase"),
    [
        ("Software Engineer I, Backend (Continuous Delivery)", JobLevel.ENTRY, "engineer i"),
        ("2027 College Program: Associate Software Engineer", JobLevel.NEW_GRAD, "college program"),
        ("Startup Generalist (New Grad)", JobLevel.NEW_GRAD, "new grad"),
        ("Graduate Software Engineer", JobLevel.NEW_GRAD, "graduate software engineer"),
        ("Software Engineer 1", JobLevel.ENTRY, "engineer 1"),
        ("Junior Software Engineer", JobLevel.ENTRY, "junior"),
        ("New Graduate Engineer, Software Security", JobLevel.NEW_GRAD, "new graduate"),
        ("Quantitative Strategy Developer - New Grad", JobLevel.NEW_GRAD, "new grad"),
        ("Associate Software Engineer", JobLevel.ENTRY, "associate software engineer"),
        ("Software Engineer Level 1", JobLevel.ENTRY, "engineer level 1"),
        ("Software Engineer Grad 2027", JobLevel.NEW_GRAD, "engineer grad"),
        ("Campus Systems Engineer (Full-Time)", JobLevel.NEW_GRAD, "campus systems engineer"),
        ("Entry-Level Software Developer", JobLevel.ENTRY, "entry level"),
        ("Développeur logiciel - nouveau diplômé", JobLevel.NEW_GRAD, "nouveau diplome"),
        ("Software Engineering Intern", JobLevel.INTERNSHIP, "intern"),
    ],
)
def test_early_career_titles_get_their_level(title: str, level: JobLevel, phrase: str) -> None:
    verdict = screen_level(title)

    assert verdict.level is level, title
    assert verdict.matched_phrase == phrase
    assert not verdict.rejected


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer I",
        "Associate Counsel",
        "Associate Director",
        "Campus Recruiter",
        "Graduate Research Assistant",
        "Application Developer Levels 1 - 5",
        "Deli Shift Leader I",
        "Specialist Customs & Trade Compliance (Calgary 1, AB)",
        "Software Engineer",
        "Software Engineer II",
        "Software Engineer 3",
        "Software Engineer L4",
        "Software Engineer I-IV",
        "Software Engineer 1 - 3",
        "Software Engineer I or II",
        "Staff Software Engineer",
        "Associate Account Executive",
        "Wealth Associate",
        "Data Entry Clerk",
        "Area Manager - Graduate Program",
        "Graduate Research Scientist",
        "Campus Security Officer",
        "Lead Software Engineer, New Grad Platform",
    ],
)
def test_experienced_and_lookalike_titles_are_rejected(title: str) -> None:
    verdict = screen_level(title)

    assert verdict.rejected, title
    assert verdict.level is JobLevel.UNKNOWN
    assert verdict.matched_phrase


def test_seniority_beats_an_early_career_marker() -> None:
    assert screen_level("Senior Software Engineer I").matched_phrase == "senior"
    assert screen_level("Junior to Senior Software Engineer").rejected
    assert screen_level("Associate Software Engineer II").matched_phrase == "engineer ii"


def test_a_level_numeral_must_trail_a_role_noun_outside_any_parentheses() -> None:
    assert screen_level("Software Engineer (Calgary 1)").rejected
    assert screen_level("Technician I").rejected
    assert screen_level("Data Scientist I").level is JobLevel.ENTRY
    assert screen_level("SDE 1").level is JobLevel.ENTRY
    assert screen_level("Software Engineer I - Backend").level is JobLevel.ENTRY


def test_an_internship_verdict_comes_first_and_is_untouched() -> None:
    for title in ("Software Engineering Intern", "Senior Engineering Intern", "Stagiaire en génie"):
        assert screen_level(title).level is JobLevel.INTERNSHIP, title


@pytest.mark.parametrize(
    "title",
    [
        "New Grad Software Engineer",
        "Entry Level Software Engineer",
        "New Graduate Software Engineer",
        "Graduate Program Software Engineer",
    ],
)
def test_titles_the_internship_screen_disqualifies_still_reach_the_level_screen(
    title: str,
) -> None:
    from stage.classify import screen_internship

    assert screen_internship(title).disqualified_by, "premise: internship.yaml names these"
    assert not screen_level(title).rejected, title


def test_a_rejection_keeps_the_internship_disqualifier_when_nothing_better_explains_it() -> None:
    assert screen_level("Returning Intern").matched_phrase == "returning intern"
    assert screen_level("Internal Auditor").matched_phrase == "internal auditor"
    assert screen_level("Software Engineer").matched_phrase == "no early-career marker in title"


def test_screening_keeps_new_grad_and_entry_postings_with_their_level() -> None:
    kept, rejected = normalize_batch(
        [
            _job("Software Engineer Intern"),
            _job("Software Engineer, New Grad", url="https://example.test/2"),
            _job("Junior Software Engineer", url="https://example.test/3"),
            _job("Senior Software Engineer", url="https://example.test/4"),
        ]
    )

    assert {entry.title_raw: entry.level for entry in kept} == {
        "Software Engineer Intern": JobLevel.INTERNSHIP,
        "Software Engineer, New Grad": JobLevel.NEW_GRAD,
        "Junior Software Engineer": JobLevel.ENTRY,
    }
    assert [(entry.reason, entry.matched_phrase) for entry in rejected] == [
        (RejectionReason.NOT_EARLY_CAREER, "senior")
    ]


def test_a_new_grad_title_still_needs_a_cs_role() -> None:
    kept, rejected = normalize_batch([_job("Startup Generalist (New Grad)")])

    assert not kept
    assert rejected[0].reason is RejectionReason.UNKNOWN_CS_ROLE


def test_the_location_screen_still_applies_to_every_level() -> None:
    kept, rejected = normalize_batch(
        [_job("Software Engineer, New Grad", location="Bengaluru, Karnataka, India")]
    )

    assert not kept
    assert rejected[0].reason is RejectionReason.OUT_OF_SCOPE_LOCATION


def test_only_an_internship_is_rejected_for_an_ended_term() -> None:
    kept, rejected = normalize_batch(
        [
            _job("Software Engineer Intern, Summer 2025"),
            _job("Software Engineer, New Grad, 2025", url="https://example.test/2"),
        ]
    )

    assert [(entry.title_raw, entry.level) for entry in kept] == [
        ("Software Engineer, New Grad, 2025", JobLevel.NEW_GRAD)
    ]
    assert [(entry.reason, entry.matched_phrase) for entry in rejected] == [
        (RejectionReason.NOT_AN_INTERNSHIP, "summer 2025 has ended")
    ]


@pytest.mark.parametrize(
    "description",
    [
        "You are currently pursuing a PhD.",
        "Currently enrolled in a master's program.",
        "PhD required.",
        "A graduate degree required.",
        "Master's required for this role.",
    ],
)
def test_a_graduate_degree_role_is_rejected_at_every_level(description: str) -> None:
    kept, rejected = normalize_batch(
        [
            _job("Software Engineer, New Grad", description),
            _job("Junior Software Engineer", description, url="https://example.test/2"),
        ]
    )

    assert not kept
    assert {entry.reason for entry in rejected} == {RejectionReason.OUT_OF_SCOPE_DEGREE}


def test_graduate_in_a_new_grad_title_is_not_a_graduate_degree() -> None:
    kept, _ = normalize_batch(
        [
            _job(
                "Graduate Software Engineer",
                url="https://example.test/jobs/graduate-software-engineer-2027",
            )
        ]
    )

    assert [entry.level for entry in kept] == [JobLevel.NEW_GRAD]


def test_graduate_in_an_internship_title_still_means_a_graduate_degree() -> None:
    _, rejected = normalize_batch([_job("Graduate Software Engineering Intern")])

    assert rejected[0].reason is RejectionReason.OUT_OF_SCOPE_DEGREE


def test_the_level_lexicon_rejects_a_marker_that_is_also_a_seniority_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stage import lexicon

    payload = {
        "seniority": ["senior", "junior"],
        "blocked": ["campus recruiter"],
        "new_grad": ["new grad"],
        "entry": ["junior"],
        "structured_new_grad": ["new grad"],
        "non_level_words": ["team"],
        "role_nouns": ["engineer"],
        "associate_words": ["associate"],
        "new_grad_words": ["graduate"],
        "level_words": ["level"],
        "entry_levels": ["i"],
        "seniority_levels": ["ii"],
    }
    monkeypatch.setattr(lexicon, "_load", lambda _: (payload, "levels.yaml"))
    level_lexicon.cache_clear()
    try:
        with pytest.raises(LexiconError, match="junior"):
            level_lexicon()
    finally:
        level_lexicon.cache_clear()


def test_the_shipped_level_lexicon_loads() -> None:
    lexicon = level_lexicon()

    assert "senior" in lexicon.seniority
    assert "new grad" in lexicon.new_grad
    assert lexicon.entry_levels.isdisjoint(lexicon.seniority_levels)


def test_a_new_grad_feed_stands_in_for_a_title_with_no_marker() -> None:
    verdict = screen_level("Software Engineer", employment_type="new grad")

    assert verdict.level is JobLevel.NEW_GRAD
    assert verdict.matched_phrase == "new grad"
    assert screen_level("Software Engineer", employment_type="Early Career").level is (
        JobLevel.NEW_GRAD
    )
    assert screen_level("Software Engineer").rejected, "no hint, no marker"
    assert screen_level("Software Engineer", employment_type="Full-time").rejected


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Software Engineer II",
        "Staff Software Engineer",
        "Campus Recruiter",
        "Associate Director, Engineering",
        "Returning Intern Program Manager",
    ],
)
def test_the_new_grad_hint_never_overrides_seniority_or_a_blocked_phrase(title: str) -> None:
    assert screen_level(title, employment_type="new grad").rejected, title


def test_a_title_marker_beats_the_new_grad_hint() -> None:
    assert screen_level("Junior Software Engineer", employment_type="new grad").level is (
        JobLevel.ENTRY
    )
    assert screen_level("Software Engineering Intern", employment_type="new grad").level is (
        JobLevel.INTERNSHIP
    )


def test_a_new_grad_feed_is_never_read_as_an_internship_feed() -> None:
    from stage.classify import screen_internship

    assert not screen_internship("Software Engineer", employment_type="new grad").is_internship


def test_screening_keeps_a_markerless_title_from_a_new_grad_feed() -> None:
    kept, rejected = normalize_batch(
        [
            _job("Software Engineer", employment_type="new grad"),
            _job("Software Engineer", url="https://example.test/2"),
        ]
    )

    assert [(entry.title_raw, entry.level) for entry in kept] == [
        ("Software Engineer", JobLevel.NEW_GRAD)
    ]
    assert [entry.reason for entry in rejected] == [RejectionReason.NOT_EARLY_CAREER]


@pytest.mark.parametrize(
    ("title", "phrase"),
    [
        ("Software Engineer Full Stack / Backend II - Full-Time - United States", "backend ii"),
        ("Software Engineer Data & AI II - Full Time - United States", "ai ii"),
        ("Graduate Software Engineer - State Estimation & Prediction - III", "prediction iii"),
        ("Software Engineer, Backend III (Remote)", "backend iii"),
        ("Software Engineer: Backend IV", "backend iv"),
        ("Platform Engineer | Infrastructure II", "infrastructure ii"),
        ("New Grad Software Engineer II", "engineer ii"),
    ],
)
def test_a_roman_numeral_closing_a_phrase_is_a_seniority_grade(title: str, phrase: str) -> None:
    for hint in ("", "new grad"):
        verdict = screen_level(title, employment_type=hint)

        assert verdict.rejected, title
        assert verdict.matched_phrase == phrase


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer I / II",
        "Software Engineer I to III - Digital Engineering",
        "Software Engineer I, II",
        "Research Engineer I or II - Embedded Development Engineer",
    ],
)
def test_a_range_that_starts_at_the_entry_grade_is_not_a_seniority_grade(title: str) -> None:
    from stage.classify.level import classify_early_career

    assert classify_early_career(title).matched_phrase == ""
    assert screen_level(title, employment_type="new grad").level is JobLevel.NEW_GRAD


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer, Team 2",
        "Software Engineer - Phase II",
        "Software Engineer - Cohort 3",
        "Software Engineer Full Stack / Backend 2",
        "Program II",
        "Henry III",
    ],
)
def test_a_numeral_that_does_not_close_a_graded_role_phrase_is_left_alone(title: str) -> None:
    from stage.classify.level import classify_early_career

    verdict = classify_early_career(title)

    assert verdict.matched_phrase == "", title
    assert screen_level(title, employment_type="new grad").level is JobLevel.NEW_GRAD


def test_the_entry_grade_still_reads_as_entry_next_to_a_separator() -> None:
    assert screen_level("Software Engineer I - Backend").level is JobLevel.ENTRY
    assert screen_level("Software Engineer I (Remote)").level is JobLevel.ENTRY
    assert screen_level("Data Scientist I | Core").level is JobLevel.ENTRY
