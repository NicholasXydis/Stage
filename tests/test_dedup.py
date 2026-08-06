
from datetime import UTC, datetime

import pytest

from stage.dedup import (
    MatchKind,
    rank,
    resolve_duplicates,
    title_canonical,
    would_merge,
)
from stage.domain import Job, Language, LocationBucket, RoleCategory, location_agrees

WHEN = datetime(2026, 8, 3, tzinfo=UTC)


def job(
    job_id: str,
    source: str,
    company: str,
    title: str,
    *,
    location: LocationBucket = LocationBucket.USA,
    term: str = "unknown",
    language: Language = Language.EN,
    url: str = "",
) -> Job:
    return Job(
        id=job_id,
        source=source,
        company=company,
        title_raw=title,
        title_normalized=title,
        apply_url_raw=url,
        apply_url_canonical=url,
        description="",
        first_seen=WHEN,
        last_seen=WHEN,
        location=location,
        term=term,
        language=language,
        role=RoleCategory.UNKNOWN,
    )


def test_identical_token_sets_across_sources_merge() -> None:
    left = job("a", "greenhouse", "Tesla", "Software Compiler Engineer Intern, AI Inference")
    right = job("b", "simplify", "Tesla", "Software Compiler Engineer Intern - AI Inference")
    assert would_merge(left, right).kind is MatchKind.SAME_LANGUAGE


def test_a_shared_canonical_url_merges_before_anything_else() -> None:
    url = "https://boards.greenhouse.io/a/1"
    left = job("a", "greenhouse", "Acme", "SWE Intern", url=url)
    right = job("b", "simplify", "Totally Different Ltd", "X", url=url)
    assert would_merge(left, right).kind is MatchKind.URL


@pytest.mark.parametrize(
    ("left_title", "right_title", "jaccard"),
    [
        (
            "Launch Engineer, Stage 0 Propellant Generation",
            "Sr. Launch Engineer, Stage 0 Propellant Generation",
            0.88,
        ),
        (
            "Fall 2026 Engineering Internship/Co-op",
            "Fall 2026 Software Engineering Internship/Co-op",
            0.86,
        ),
        (
            "Embedded Software Engineer Intern - AI Platform",
            "Embedded Systems Software Engineer Intern - AI Platform",
            0.86,
        ),
        ("Frontend Software Engineer Intern", "Software Engineer Intern", 0.75),
        (
            "Chassis Validation Engineer Intern, Vehicle",
            "Chassis Integration Engineer Intern, Vehicle",
            0.71,
        ),
        (
            "Machine Learning Engineer Intern - Data Search",
            "Machine Learning Engineer Intern - Search Quality",
            0.86,
        ),
    ],
)
def test_near_miss_titles_must_not_merge(left_title: str, right_title: str, jaccard: float) -> None:
    left = job("a", "greenhouse", "SpaceX", left_title)
    right = job("b", "simplify", "SpaceX", right_title)
    assert not would_merge(left, right), f"J≈{jaccard} is not identity"


def test_two_rows_from_the_same_source_never_fuzzy_merge() -> None:
    left = job("a", "simplify", "Tesla", "Software Engineer Intern, Update Systems")
    right = job("b", "simplify", "Tesla", "Software Engineer Intern, Update Systems")
    assert not would_merge(left, right)


def test_the_same_title_at_different_locations_must_not_merge() -> None:
    left = job(
        "a", "greenhouse", "Ubisoft", "Gameplay Programmer Intern",
        location=LocationBucket.MONTREAL,
    )
    right = job(
        "b", "simplify", "Ubisoft", "Gameplay Programmer Intern", location=LocationBucket.USA
    )
    assert not would_merge(left, right)


def test_a_different_employer_never_merges_on_title_alone() -> None:
    left = job("a", "greenhouse", "Stripe", "Software Engineer Intern")
    right = job("b", "simplify", "Square", "Software Engineer Intern")
    assert not would_merge(left, right)


def test_remote_does_not_agree_with_a_city() -> None:
    assert not location_agrees(LocationBucket.REMOTE, LocationBucket.MONTREAL)
    left = job(
        "a", "greenhouse", "Shopify", "Backend Intern", location=LocationBucket.REMOTE
    )
    right = job(
        "b", "simplify", "Shopify", "Backend Intern", location=LocationBucket.MONTREAL
    )
    assert not would_merge(left, right)


@pytest.mark.parametrize("bucket", [LocationBucket.UNKNOWN, LocationBucket.OTHER])
def test_unresolved_locations_never_satisfy_the_guardrail(bucket: LocationBucket) -> None:
    left = job("a", "greenhouse", "Acme", "Software Engineer Intern", location=bucket)
    right = job("b", "simplify", "Acme", "Software Engineer Intern", location=bucket)
    assert not would_merge(left, right)


def test_cross_language_requires_company_location_and_term() -> None:
    english = job(
        "a", "greenhouse", "Ubisoft", "Software Engineering Intern",
        location=LocationBucket.MONTREAL, term="summer-2027", language=Language.EN,
    )
    french = job(
        "b", "simplify", "Ubisoft", "Stagiaire en génie logiciel",
        location=LocationBucket.MONTREAL, term="summer-2027", language=Language.FR,
    )
    assert would_merge(english, french).kind is MatchKind.CROSS_LANGUAGE


@pytest.mark.parametrize(
    ("term", "location"),
    [
        ("fall-2026", LocationBucket.MONTREAL),
        ("unknown", LocationBucket.MONTREAL),
        ("summer-2027", LocationBucket.CANADA),
    ],
)
def test_cross_language_is_refused_when_any_guardrail_field_disagrees(
    term: str, location: LocationBucket
) -> None:
    english = job(
        "a", "greenhouse", "Ubisoft", "Software Engineering Intern",
        location=LocationBucket.MONTREAL, term="summer-2027", language=Language.EN,
    )
    french = job(
        "b", "simplify", "Ubisoft", "Stagiaire en génie logiciel",
        location=location, term=term, language=Language.FR,
    )
    assert not would_merge(english, french)


def test_cross_language_needs_a_canonicalizable_title() -> None:
    assert title_canonical("Summer Intern") == ""
    english = job(
        "a", "greenhouse", "Ubisoft", "Summer Intern",
        location=LocationBucket.MONTREAL, term="summer-2027", language=Language.EN,
    )
    french = job(
        "b", "simplify", "Ubisoft", "Stagiaire",
        location=LocationBucket.MONTREAL, term="summer-2027", language=Language.FR,
    )
    assert not would_merge(english, french)


@pytest.mark.parametrize(
    ("english", "french"),
    [
        ("Software Engineering Intern", "Stagiaire en génie logiciel"),
        ("Data Science Intern", "Stagiaire en science des données"),
        ("Machine Learning Intern", "Stagiaire en apprentissage automatique"),
        ("Security Engineer Intern", "Stagiaire en cybersécurité"),
    ],
)
def test_title_canonical_is_language_neutral(english: str, french: str) -> None:
    assert title_canonical(english) == title_canonical(french) != ""


def test_the_survivor_is_the_higher_priority_source() -> None:
    feed = job("b", "simplify", "Acme", "SWE Intern")
    direct = job("a", "greenhouse", "Acme", "SWE Intern")
    assert min((feed, direct), key=rank) is direct
    assert min((direct, feed), key=rank) is direct


def test_survivor_selection_is_stable_across_runs() -> None:
    left = job("a", "simplify", "Acme", "SWE Intern")
    right = job("b", "simplify", "Acme", "SWE Intern")
    assert sorted((left, right), key=rank) == sorted((right, left), key=rank), (
        "order must come from source priority and id, never arrival order"
    )


def test_a_cluster_never_contains_two_rows_from_one_source() -> None:
    a = job("a", "greenhouse", "Tower Research", "Quantitative Trader Intern")
    c = job("c", "greenhouse", "Tower Research", "Quantitative Developer Intern")
    b = job("b", "simplify", "Tower Research", "Quantitative Trader Intern")
    links = resolve_duplicates([a, b, c], [])
    canonical_for = {link.duplicate_id: link.canonical_id for link in links}
    assert "c" not in canonical_for or canonical_for["c"] != "a"
    by_id = {"a": a, "b": b, "c": c}
    for duplicate, canonical in canonical_for.items():
        assert by_id[duplicate].source != by_id[canonical].source


def test_a_url_shared_by_several_requisitions_is_not_an_identity_key() -> None:
    shared = "https://boards.greenhouse.io/tower/jobs/1"
    left = job("a", "greenhouse", "Tower", "Quantitative Trader Intern", url=shared)
    right = job("b", "greenhouse", "Tower", "Junior Execution Trader Intern", url=shared)
    feed = job("c", "simplify", "Tower", "Quantitative Trader Intern", url=shared)
    assert resolve_duplicates([left, right, feed], []) == ()


def test_the_canonical_row_does_not_depend_on_arrival_order() -> None:
    direct = job("a", "greenhouse", "Acme", "Software Engineer Intern")
    feed = job("z", "simplify", "Acme", "Software Engineer Intern")
    feed_first = resolve_duplicates([feed], [direct])
    direct_first = resolve_duplicates([direct], [feed])
    assert feed_first == direct_first
    assert feed_first[0].duplicate_id == "z"
    assert feed_first[0].canonical_id == "a"


def test_a_higher_priority_arrival_demotes_the_stored_row() -> None:
    feed = job("z", "simplify", "Acme", "Software Engineer Intern")
    direct = job("a", "greenhouse", "Acme", "Software Engineer Intern")
    links = resolve_duplicates([direct], [feed])
    assert [(link.duplicate_id, link.canonical_id) for link in links] == [("z", "a")]


def test_three_sources_converge_on_one_canonical_row() -> None:
    direct = job("a", "greenhouse", "Acme", "Software Engineer Intern")
    mid = job("m", "simplify", "Acme", "Software Engineer Intern")
    low = job("z", "vanshb03", "Acme", "Software Engineer Intern")
    links = resolve_duplicates([direct, mid, low], [])
    assert {link.canonical_id for link in links} == {"a"}
    assert {link.duplicate_id for link in links} == {"m", "z"}


def test_two_greenhouse_boards_of_one_employer_can_merge_across_languages() -> None:
    from stage.domain import job_id as build_id

    english = job(
        build_id("greenhouse", "lightspeedhq", "101"),
        "greenhouse",
        "Lightspeed",
        "Software Developer Intern",
    )
    french = job(
        build_id("greenhouse", "lightspeedhqfr", "202"),
        "greenhouse",
        "Lightspeed",
        "Stagiaire en developpement logiciel",
    )

    assert english.board_key != french.board_key
    assert english.board_key == "greenhouse:lightspeedhq"
    assert french.board_key == "greenhouse:lightspeedhqfr"

    same_board = job(
        build_id("greenhouse", "lightspeedhq", "303"),
        "greenhouse",
        "Lightspeed",
        "Software Developer Intern",
    )
    assert same_board.board_key == english.board_key
    assert not would_merge(english, same_board), "one board, two requisitions, never merged"


def test_a_malformed_id_degrades_to_the_stricter_rule_not_the_looser_one() -> None:
    bare = job("not-a-composite-id", "greenhouse", "Acme", "Software Engineer Intern")
    other = job("also-bare", "greenhouse", "Acme", "Software Engineer Intern")
    assert bare.board_key == other.board_key == "greenhouse"
    assert not would_merge(bare, other)
