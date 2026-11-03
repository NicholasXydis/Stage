from dataclasses import replace
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
        "a",
        "greenhouse",
        "Ubisoft",
        "Gameplay Programmer Intern",
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


def test_international_does_not_agree_with_a_city() -> None:
    assert not location_agrees(LocationBucket.INTERNATIONAL, LocationBucket.MONTREAL)
    left = job(
        "a", "greenhouse", "Shopify", "Backend Intern", location=LocationBucket.INTERNATIONAL
    )
    right = job("b", "simplify", "Shopify", "Backend Intern", location=LocationBucket.MONTREAL)
    assert not would_merge(left, right)


@pytest.mark.parametrize("bucket", [LocationBucket.UNKNOWN, LocationBucket.INTERNATIONAL])
def test_unresolved_locations_never_satisfy_the_guardrail(bucket: LocationBucket) -> None:
    left = job("a", "greenhouse", "Acme", "Software Engineer Intern", location=bucket)
    right = job("b", "simplify", "Acme", "Software Engineer Intern", location=bucket)
    assert not would_merge(left, right)


def test_cross_language_requires_company_location_and_term() -> None:
    english = job(
        "a",
        "greenhouse",
        "Ubisoft",
        "Software Engineering Intern",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        language=Language.EN,
    )
    french = job(
        "b",
        "simplify",
        "Ubisoft",
        "Stagiaire en génie logiciel",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        language=Language.FR,
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
        "a",
        "greenhouse",
        "Ubisoft",
        "Software Engineering Intern",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        language=Language.EN,
    )
    french = job(
        "b",
        "simplify",
        "Ubisoft",
        "Stagiaire en génie logiciel",
        location=location,
        term=term,
        language=Language.FR,
    )
    assert not would_merge(english, french)


def test_cross_language_needs_a_canonicalizable_title() -> None:
    assert title_canonical("Summer Intern") == ""
    english = job(
        "a",
        "greenhouse",
        "Ubisoft",
        "Summer Intern",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        language=Language.EN,
    )
    french = job(
        "b",
        "simplify",
        "Ubisoft",
        "Stagiaire",
        location=LocationBucket.MONTREAL,
        term="summer-2027",
        language=Language.FR,
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
    a = job("greenhouse:tower-en:1", "greenhouse", "Tower Research", "Quantitative Trader Intern")
    c = job("greenhouse:tower-fr:2", "greenhouse", "Tower Research", "Quantitative Trader Intern")
    b = job("b", "simplify", "Tower Research", "Quantitative Trader Intern")
    links = resolve_duplicates([a, b, c], [])
    canonical_for = {link.duplicate_id: link.canonical_id for link in links}
    assert c.id not in canonical_for or canonical_for[c.id] != a.id
    by_id = {a.id: a, b.id: b, c.id: c}
    for duplicate, canonical in canonical_for.items():
        assert by_id[duplicate].source != by_id[canonical].source


def test_a_url_shared_by_several_requisitions_is_not_an_identity_key() -> None:
    shared = "https://boards.greenhouse.io/tower/jobs/1"
    left = job("a", "greenhouse", "Tower", "Quantitative Trader Intern", url=shared)
    right = job("b", "greenhouse", "Tower", "Junior Execution Trader Intern", url=shared)
    feed = job("c", "simplify", "Tower", "Quantitative Trader Intern", url=shared)
    assert resolve_duplicates([left, right, feed], []) == ()


def test_a_generic_url_is_detected_across_boards_not_only_within_one() -> None:
    shared = "https://job-boards.greenhouse.io/embed/job_app"
    rows = [
        job("vanshb03:aquatic:1", "vanshb03", "Aquatic Capital", "SWE Intern", url=shared),
        job("vanshb03:cubist:2", "vanshb03", "Cubist Systematic", "Quant Dev Intern", url=shared),
        job("vanshb03:gemini:3", "vanshb03", "Gemini", "Software Engineering Intern", url=shared),
    ]
    assert len({row.board_key for row in rows}) == 3, "one row per board, or it is blind"
    assert resolve_duplicates(rows, []) == (), "a URL shared inside one source is generic"


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


def test_two_boards_on_one_platform_never_merge_on_a_title_alone() -> None:
    left = job(
        "greenhouse:tower-en:1", "greenhouse", "Tower Research", "Quantitative Trader Intern"
    )
    right = job(
        "greenhouse:tower-fr:2", "greenhouse", "Tower Research", "Quantitative Trader Intern"
    )
    assert resolve_duplicates([left, right], []) == (), (
        "a title match inside one platform is too soft to merge two boards"
    )


@pytest.mark.parametrize(
    ("left_name", "right_name"),
    [
        ("Medpace", "Medpace, Inc."),
        ("Verition Fund Management", "Verition Fund Management LLC"),
        ("Jump Trading", "Jump Trading Group"),
        ("Old Mission", "Old Mission Capital"),
    ],
)
def test_a_legal_suffix_or_division_word_is_not_a_different_employer(
    left_name: str, right_name: str
) -> None:
    left = job("greenhouse:a:1", "greenhouse", left_name, "Software Engineer Intern")
    right = job("lever:b:2", "lever", right_name, "Software Engineer Intern")
    assert would_merge(left, right), f"{left_name!r} and {right_name!r} are one employer"


@pytest.mark.parametrize(
    ("left_name", "right_name"),
    [
        ("Apex", "Apex Capital"),
        ("Meta", "Nvidia"),
        ("Jump Trading", "Jump Crypto Holdings"),
    ],
)
def test_a_shared_generic_word_does_not_make_two_employers_one(
    left_name: str, right_name: str
) -> None:
    left = job("greenhouse:a:1", "greenhouse", left_name, "Software Engineer Intern")
    right = job("lever:b:2", "lever", right_name, "Software Engineer Intern")
    assert not would_merge(left, right), f"{left_name!r} and {right_name!r} are separate employers"


def test_a_chained_duplicate_never_demotes_the_higher_priority_canonical() -> None:
    from dataclasses import replace

    canonical = job(
        "greenhouse:acme:1",
        "greenhouse",
        "Acme",
        "Software Engineering Intern",
        url="https://acme.example/1",
    )
    existing = replace(
        job(
            "speedyapply:acme:2",
            "speedyapply",
            "Acme",
            "Software Engineering Intern",
            url="https://acme.example/1",
        ),
        duplicate_of=canonical.id,
    )
    incoming = job(
        "lever:acme:3",
        "lever",
        "Acme",
        "Software Engineering Intern",
        url="https://acme.example/1",
    )

    links = resolve_duplicates([incoming], [canonical, existing])

    assert links
    for link in links:
        assert link.duplicate_id != canonical.id
        assert link.canonical_id == canonical.id, (link.duplicate_id, link.canonical_id)
    assert any(link.duplicate_id == incoming.id and link.evidence for link in links)


def test_a_feed_copy_with_a_blank_place_joins_the_same_workday_posting() -> None:
    from stage.services.screening import normalize_batch
    from stage.sources.speedyapply import SpeedyApplyFeed, SpeedyApplyListing

    path = "job/Des-Moines/Software-Development-Intern_REQ00471"
    listed = Job(
        id="simplify:dee-zee:1",
        source="simplify",
        company="Dee Zee",
        title_raw="Software Development Intern",
        title_normalized="Software Development Intern",
        apply_url_raw=f"https://deezee.wd108.myworkdayjobs.com/DeeZee_Careers/{path}",
        description="",
        location_raw="Des Moines, IA",
        first_seen=WHEN,
        last_seen=WHEN,
    )
    blank = SpeedyApplyListing(
        company="Dee Zee",
        title="Software Development Intern",
        url=f"https://deezee.wd108.myworkdayjobs.com/en-US/deezee_careers/{path}",
    )
    kept, rejected = normalize_batch([listed, SpeedyApplyFeed()._to_job(blank, WHEN)])

    assert not rejected
    assert {entry.location for entry in kept} == {LocationBucket.USA}
    links = resolve_duplicates(kept, ())
    assert [(link.canonical_id, link.kind) for link in links] == [(listed.id, "url")]


def test_a_multi_site_workday_posting_is_screened_with_its_first_place() -> None:
    from stage.services.screening import normalize_batch

    posting = Job(
        id="workday:desjardins-external:r1",
        source="workday",
        company="Desjardins",
        title_raw="Software Developer Intern",
        title_normalized="software developer intern",
        apply_url_raw="https://desjardins.wd10.myworkdayjobs.com/External/job/Montral/Intern_R1",
        description="",
        location_raw="2 Locations",
        first_seen=WHEN,
        last_seen=WHEN,
    )

    kept, rejected = normalize_batch([posting])

    assert not rejected and kept[0].location is LocationBucket.MONTREAL
    assert kept[0].location_raw == "Montréal / 1 more"


def test_rescreen_gives_a_stored_multi_site_posting_its_first_place() -> None:
    from stage.services.maintenance import _reclassifications

    stored = job("workday:sunlife-external:r2", "workday", "Sun Life", "Software Intern")
    stored = replace(
        stored,
        location=LocationBucket.UNKNOWN,
        location_raw="2 Locations",
        apply_url_raw="https://sunlife.wd3.myworkdayjobs.com/External/job/Toronto-Ontario/I_R2",
    )

    [candidate], _ = _reclassifications([stored])

    assert candidate.location is LocationBucket.CANADA
    assert candidate.location_raw == "Toronto Ontario / 1 more"


@pytest.mark.parametrize(
    ("posted", "kept"),
    [
        (datetime(1, 1, 1, tzinfo=UTC), None),
        (datetime(9999, 12, 31, tzinfo=UTC), None),
        (datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None), datetime(2026, 8, 1, tzinfo=UTC)),
        (datetime(2026, 7, 30, tzinfo=UTC), datetime(2026, 7, 30, tzinfo=UTC)),
    ],
)
def test_an_implausible_posting_date_is_dropped_and_a_naive_one_read_as_utc(
    posted: datetime, kept: datetime | None
) -> None:
    from stage.services.screening import normalize_batch

    posting = replace(
        job("greenhouse:acme:1", "greenhouse", "Acme", "Software Engineer Intern"),
        source_posted_at=posted,
        location_raw="Toronto, ON",
    )

    kept_jobs, rejected = normalize_batch([posting])

    assert not rejected and kept_jobs[0].source_posted_at == kept


def _pairwise(incoming: list[Job], existing: list[Job]) -> list[tuple[str, str, str, str]]:
    pool = {item.id: item for item in [*existing, *incoming]}
    jobs = sorted(pool.values(), key=rank)
    seen: dict[tuple[str, str], int] = {}
    for item in jobs:
        if item.apply_url_canonical:
            key = (item.source, item.apply_url_canonical)
            seen[key] = seen.get(key, 0) + 1
    ambiguous = {url for (_, url), count in seen.items() if count > 1}
    parent = {item.id: item.id for item in jobs}
    boards = {item.id: {item.board_key} for item in jobs}
    sources = {item.id: {item.source} for item in jobs}
    order = {item.id: index for index, item in enumerate(jobs)}
    reason: dict[str, tuple[str, str]] = {}

    def find(ident: str) -> str:
        while parent[ident] != ident:
            parent[ident] = parent[parent[ident]]
            ident = parent[ident]
        return ident

    def union(left_id: str, right_id: str) -> None:
        left_root, right_root = find(left_id), find(right_id)
        if left_root == right_root:
            return
        if order[right_root] < order[left_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        boards[left_root] |= boards[right_root]
        sources[left_root] |= sources[right_root]

    for item in jobs:
        if item.duplicate_of and item.duplicate_of in parent:
            union(item.id, item.duplicate_of)
    touched = {item.id for item in incoming}
    for by_link in (True, False):
        for index, left in enumerate(jobs):
            for right in jobs[index + 1 :]:
                if left.id not in touched and right.id not in touched:
                    continue
                left_root, right_root = find(left.id), find(right.id)
                if left_root == right_root:
                    continue
                if (
                    boards[left_root] & boards[right_root]
                    or sources[left_root] & sources[right_root]
                ):
                    continue
                match = would_merge(left, right)
                if not match or (match.kind is MatchKind.URL) is not by_link:
                    continue
                if match.kind is MatchKind.URL and left.apply_url_canonical in ambiguous:
                    continue
                union(left.id, right.id)
                demoted = right.id if find(right.id) != right_root else left.id
                reason[demoted] = (match.kind.value, match.evidence)
    return [
        (item.id, find(item.id), *reason.get(item.id, ("", "")))
        for item in jobs
        if find(item.id) != item.id
    ]


def _realistic_batch(seed: int, size: int) -> tuple[list[Job], list[Job]]:
    import random

    chance = random.Random(seed)
    names = [
        ("Acme", "Acme Inc.", "ACME Canada", "Acme Corp"),
        ("The Trade Desk", "Trade Desk", "The Trade Desk Inc"),
        ("Ubisoft", "Ubisoft Montréal", "Ubisoft Divertissements"),
        ("Banque Nationale", "National Bank", "Banque Nationale du Canada"),
        ("RBC", "Royal Bank of Canada", "RBC Capital Markets"),
        ("Inc", "LLC", "—"),
        *((f"Vendor {number}", f"Vendor {number} Ltd") for number in range(40)),
    ]
    titles = [
        ("Software Engineer Intern", Language.EN),
        ("Intern, Software Engineer", Language.EN),
        ("Software Developer Co-op", Language.EN),
        ("Stagiaire en développement logiciel", Language.FR),
        ("Data Science Intern", Language.EN),
        ("Stagiaire en science des données", Language.FR),
        ("Machine Learning Intern - Summer 2027", Language.EN),
    ]
    places = [LocationBucket.MONTREAL, LocationBucket.CANADA, LocationBucket.USA]
    boards = ["greenhouse", "workday", "lever", "simplify", "vanshb03", "negar", "speedyapply"]
    rows: list[Job] = []
    for number in range(size):
        family = chance.choice(names)
        company = chance.choice(family)
        source = chance.choice(boards)
        title, language = chance.choice(titles)
        posting = chance.randrange(size // 3)
        url = f"https://jobs.example/{family[0]}/{posting}" if chance.random() < 0.6 else ""
        slug = company.lower().replace(" ", "-") or "x"
        rows.append(
            job(
                f"{source}:{slug}:{number}",
                source,
                company,
                title,
                location=chance.choice(places),
                term=chance.choice(["summer 2027", "fall 2026", "unknown"]),
                language=language,
                url=url,
            )
        )
    existing = [
        replace(item, duplicate_of=chance.choice(rows).id) if chance.random() < 0.05 else item
        for item in rows[: size // 2]
    ]
    return rows[size // 2 :], existing


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_blocking_by_company_and_url_links_exactly_what_the_pairwise_pass_did(seed: int) -> None:
    incoming, existing = _realistic_batch(seed, 700)

    links = resolve_duplicates(incoming, existing)

    expected = _pairwise(incoming, existing)
    assert len(expected) > 100, "the batch must actually exercise merging"
    assert [(x.duplicate_id, x.canonical_id, x.kind, x.evidence) for x in links] == expected


OPENING = "https://cibc.wd3.myworkdayjobs.com/job/toronto-on/2642721"


def test_one_workday_opening_on_two_sites_of_one_source_is_one_posting() -> None:
    search = job("workday:cibc-search:1", "workday", "CIBC", "AI Co-op", url=OPENING)
    campus = job("workday:cibc-campus:2", "workday", "CIBC (campus)", "AI Co-op", url=OPENING)
    listed = job("simplify:cibc:3", "simplify", "CIBC", "AI Co-op", url=OPENING)

    links = resolve_duplicates((search, campus, listed), ())

    assert len({link.canonical_id for link in links}) == 1
    assert {link.duplicate_id for link in links} | {links[0].canonical_id} == {
        search.id,
        campus.id,
        listed.id,
    }


def test_a_list_naming_one_opening_twice_counts_it_once() -> None:
    first = job("zshah101:kla:1", "zshah101", "KLA", "Software Engineer Intern", url=OPENING)
    second = job("zshah101:kla:2", "zshah101", "KLA", "Software Engineering Intern", url=OPENING)

    assert [
        (link.duplicate_id, link.canonical_id) for link in resolve_duplicates((first, second), ())
    ] in (
        [(second.id, first.id)],
        [(first.id, second.id)],
    )


def test_a_shared_opening_never_joins_two_openings_from_one_list() -> None:
    other = OPENING.replace("2642721", "2642722")
    first = job("simplify:acme:1", "simplify", "Acme", "Quant Developer Intern", url=OPENING)
    second = job("simplify:acme:2", "simplify", "Acme", "Quant Developer Intern", url=other)
    named = job("speedyapply:acme:1", "speedyapply", "Acme", "Quant Developer Intern", url=OPENING)
    again = job("speedyapply:acme:2", "speedyapply", "Acme", "Quant Developer Intern", url=other)

    for incoming, existing in (
        ((again,), (first, second, named)),
        ((second, again), (first, named)),
    ):
        root = {x.duplicate_id: x.canonical_id for x in resolve_duplicates(incoming, existing)}
        assert root.get(first.id, first.id) != root.get(second.id, second.id)


def test_two_openings_from_one_source_stay_apart() -> None:
    one = job("workday:acme:1", "workday", "Acme", "Software Intern", url=OPENING)
    two = job(
        "workday:acme:2",
        "workday",
        "Acme",
        "Software Intern",
        url=OPENING.replace("2642721", "2642722"),
    )

    assert resolve_duplicates((one, two), ()) == ()


@pytest.mark.parametrize(
    ("place", "merges"),
    [
        ("Palm Bay, FL", False),
        ("Raleigh, North Carolina", False),
        ("Melbourne, Florida", True),
        ("Remote", True),
        ("2 Locations", True),
        ("United States", True),
    ],
)
def test_a_title_match_never_joins_two_different_cities(place: str, merges: bool) -> None:
    board = replace(
        job("greenhouse:l3:1", "greenhouse", "L3Harris", "Software Intern"),
        location_raw="Melbourne, FL",
    )
    listed = replace(
        job("simplify:l3:2", "simplify", "L3Harris", "Software Intern"), location_raw=place
    )

    assert bool(would_merge(board, listed)) is merges


def test_comparing_places_reads_each_place_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.dedup import identity

    board = replace(
        job("greenhouse:k:1", "greenhouse", "Kla", "Intern"), location_raw="Ann Arbor, MI"
    )
    listed = replace(job("simplify:k:2", "simplify", "Kla", "Intern"), location_raw="Milpitas, CA")
    assert identity._different_cities(board, listed)

    folded: list[str] = []

    def counted(text: str) -> str:
        folded.append(text)
        return text

    monkeypatch.setattr(identity, "fold", counted)

    assert identity._different_cities(board, listed)
    assert folded == []


def test_a_shared_link_wins_over_a_same_title_opening() -> None:
    one, two = "https://e.example/?gh_jid=1", "https://e.example/?gh_jid=2"
    title = "Intern: Engineering - Embedded"
    listed = job(
        "simplify:e:1", "simplify", "EquipmentShare", "Embedded Engineering Intern", url=one
    )
    twin = job("speedyapply:e:9", "speedyapply", "EquipmentShare", title, url=one)
    other = job("speedyapply:e:2", "speedyapply", "EquipmentShare", title, url=two)

    links = {link.duplicate_id: link for link in resolve_duplicates((other, twin, listed), ())}

    assert links["speedyapply:e:9"].kind == MatchKind.URL.value
    assert "speedyapply:e:2" not in links


def test_one_link_still_joins_two_cities() -> None:
    board = replace(
        job("greenhouse:a:1", "greenhouse", "Acme", "Intern", url="https://a.example/1"),
        location_raw="Austin, TX",
    )
    listed = replace(
        job("simplify:a:2", "simplify", "Acme", "Intern", url="https://a.example/1"),
        location_raw="Dallas, TX",
    )

    assert would_merge(board, listed).kind is MatchKind.URL


@pytest.mark.parametrize(
    ("board_name", "listed_name", "merges"),
    [
        ("NVIDIA", "NVIDIA (Eightfold)", True),
        ("Toshiba Global Commerce", "Toshiba Commerce Solutions", True),
        ("SK hynix", "SK Hynix Memory Solutions", True),
        (
            "Carnegie Mellon University",
            "Carnegie Mellon Software Engineering Institute (SEI)",
            True,
        ),
        ("Boston Consulting Group", "Boston Scientific", False),
        ("CIBC (campus)", "CIBC", True),
        ("Vestmark", "Vestmark Internship Program", True),
        ("Shopify", "Spotify", False),
    ],
)
def test_registry_labels_and_list_wording_do_not_split_one_employer(
    board_name: str, listed_name: str, merges: bool
) -> None:
    board = job("greenhouse:x:1", "greenhouse", board_name, "Software Intern")
    listed = job("simplify:y:2", "simplify", listed_name, "Software Intern")

    assert bool(would_merge(board, listed)) is merges


@pytest.mark.parametrize(
    ("one", "two", "merges"),
    [
        ("Software Engineering Intern", "Software Engineer Intern", True),
        ("Software Development Internship", "Software Developer Intern", True),
        ("Software Engineer Intern", "Hardware Engineer Intern", False),
    ],
)
def test_word_forms_of_one_title_still_match(one: str, two: str, merges: bool) -> None:
    board = job("greenhouse:t:1", "greenhouse", "Twitch", one)
    listed = job("simplify:t:2", "simplify", "Twitch", two)

    assert bool(would_merge(board, listed)) is merges


def test_the_apply_page_of_a_workday_opening_is_the_same_opening() -> None:
    from stage.normalize import canonical_apply_url

    page = "https://acme.wd1.myworkdayjobs.com/External/job/Austin-TX/Software-Intern_R123456"

    assert canonical_apply_url(f"{page}/apply") == canonical_apply_url(page)


def test_two_different_workday_openings_never_merge_on_title() -> None:
    site = "https://acme.wd1.myworkdayjobs.com/job/austin-tx"
    one = replace(
        job("simplify:a:1", "simplify", "Acme", "Software Intern"),
        apply_url_canonical=f"{site}/r123456",
    )
    two = replace(
        job("speedyapply:a:2", "speedyapply", "Acme", "Software Intern"),
        apply_url_canonical=f"{site}/r123457",
    )

    assert not would_merge(one, two)
