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
    terms = {item.id: {item.term} - {"unknown"} for item in jobs}
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
        terms[left_root] |= terms[right_root]

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
                one, two = terms[left_root], terms[right_root]
                if match.kind is not MatchKind.URL and one and two and one.isdisjoint(two):
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


def _listed(job_id: str, source: str, company: str, title: str, place: str, url: str = "") -> Job:
    return replace(job(job_id, source, company, title, url=url), location_raw=place)


@pytest.mark.parametrize(
    ("board", "listed"),
    [
        (
            (
                "HP",
                "Software Engineering Intern, AIML Platform - HP IQ",
                "San Francisco, California",
            ),
            (
                "Hewlett Packard (HP)",
                "Software Engineering Intern, AIML Platform",
                "San Francisco, CA",
            ),
        ),
        (
            (
                "Northrop Grumman",
                "2027 Ground Software Engineer Intern - Redondo Beach CA",
                "Redondo Beach CA",
            ),
            ("Northrop Grumman", "Ground Software Engineer Intern", "Redondo Beach, CA"),
        ),
        (
            ("RTX", "Software Engineering Intern (Summer 2027)", "UT-WEST VALLEY CITY"),
            ("RTX", "Software Engineering Intern", "West Valley City, UT"),
        ),
        (
            (
                "Commonwealth Fusion Systems",
                "Intern - Software Engineer - Summer 2027",
                "Devens, MA",
            ),
            ("CFS Energy", "Software Engineer Intern", "Devens, MA"),
        ),
        (
            ("Element Fleet Management", "Forward Deployed Engineer Intern", "Toronto, ON"),
            ("Element Fleet Management", "Forward Deployed Engineer (FDE) Intern", "Toronto"),
        ),
        (
            ("Nelnet", "Platform Engineer Intern (8 month term)", "CAN - MISSISSAUGA"),
            ("Nelnet", "Platform Engineer Intern", "Mississauga, Canada"),
        ),
        (
            ("Samsung", "Samsung Care AI Data Analytic Intern", "Plano, TX"),
            ("Samsung", "Care AI Data Analytic Intern", "Plano, TX"),
        ),
        (
            (
                "Booz Allen Hamilton",
                "University \u2013 Summer 27, Cybersecurity Analyst Intern",
                "McLean, VA",
            ),
            ("Booz Allen Hamilton", "Cybersecurity Analyst Intern", "McLean, VA"),
        ),
        (
            (
                "Apex Technology",
                "Ground Software Engineering Internship (Spring or Summer 2027)",
                "Los Angeles",
            ),
            ("Apex Technology, Inc.", "Ground Software Engineering Intern", "Los Angeles, CA"),
        ),
        (
            ("Nintendo", "Intern - Software Engineer, CPU Debugger (NTD)", "Redmond, WA"),
            ("Nintendo", "Software Engineer Intern, CPU Debugger", "Redmond, WA"),
        ),
        (
            (
                "Walmart",
                "USA Summer 2027 Corporate Intern: Last Mile Delivery Data Science Intern",
                "USA",
            ),
            ("Walmart", "Data Science Intern, Last Mile Delivery", "Bentonville, AR"),
        ),
    ],
)
def test_a_list_copy_joins_its_board_posting_despite_names_years_and_places(
    board: tuple[str, str, str], listed: tuple[str, str, str]
) -> None:
    one = _listed("workday:a:1", "workday", *board)
    two = _listed("vanshb03:b:2", "vanshb03", *listed)

    assert would_merge(one, two).kind is MatchKind.SAME_LANGUAGE
    assert [link.duplicate_id for link in resolve_duplicates([two], [one])] == [two.id]


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("Software Engineer Intern (Summer 2027)", "Software Engineer Intern (Fall 2027)"),
        ("Software Intern 2026", "Software Intern 2027"),
        ("Software Intern - Backend", "Software Intern"),
        ("Software Intern - HP IQ", "Software Intern - Platform"),
    ],
)
def test_another_term_year_or_team_keeps_two_titles_apart(one: str, two: str) -> None:
    board = _listed("workday:a:1", "workday", "HP", one, "Palo Alto, CA")
    listed = _listed("simplify:b:2", "simplify", "HP", two, "Palo Alto, CA")

    assert not would_merge(board, listed)


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("Software Engineer Intern (MS)", "Software Engineer Intern"),
        ("Software Engineer Intern (NTD)", "Software Engineer Intern (NOA)"),
    ],
)
def test_a_degree_tag_or_another_team_tag_keeps_two_titles_apart(one: str, two: str) -> None:
    board = _listed("workday:a:1", "workday", "Nintendo", one, "Redmond, WA")
    listed = _listed("simplify:b:2", "simplify", "Nintendo", two, "Redmond, WA")

    assert not would_merge(board, listed)


@pytest.mark.parametrize(
    ("board", "listed"),
    [
        (("L.L.Bean", "Software Developer Intern"), ("Llbean", "Software Developer Intern")),
        (("Spectrum", "Data Science Intern"), ("Invitation-charter", "Data Science Intern")),
        (
            ("Western Alliance", "Summer 2027 Internship - Dallas, TX - Platform Engineering"),
            ("Western Alliance Bancorporation", "Platform Engineering Intern"),
        ),
        (
            ("Riverside Research", "Software Applications Intern"),
            ("Riverside Research Institute", "Software Applications Intern"),
        ),
        (
            ("Alaska Air Group", "Software Engineer Intern"),
            ("Alaska Airlines", "Software Engineer Intern"),
        ),
    ],
)
def test_a_name_written_another_way_is_shown_once_and_joins(
    board: tuple[str, str], listed: tuple[str, str]
) -> None:
    from stage.normalize import shown_company

    one = _listed("workday:a:1", "workday", shown_company(board[0]), board[1], "Dallas, TX")
    two = _listed("vanshb03:b:2", "vanshb03", shown_company(listed[0]), listed[1], "Dallas, TX")

    assert one.company == two.company
    assert would_merge(one, two)


def test_hp_enterprise_is_not_hp() -> None:
    board = _listed("workday:a:1", "workday", "HP", "Software Intern", "Houston, TX")
    listed = _listed(
        "simplify:b:2", "simplify", "Hewlett Packard Enterprise", "Software Intern", "Houston, TX"
    )

    assert not would_merge(board, listed)


def test_a_zapply_phenom_link_names_the_same_workday_opening() -> None:
    from stage.dedup.identity import opening_key

    workday = (
        "https://danaher.wd1.myworkdayjobs.com/danaherjobs/job/USA---Indianapolis/"
        "Computer-Science-Data-AI-Co-op-Supply-Chain_R1320599"
    )
    phenom = "https://zapply.jobs/l/d/phenom-danaher-R1320599?s=gh-internships-2027"
    assert opening_key(phenom) == opening_key(workday) == "danaher:1320599"


GATEWAY = (
    "https://globalhr.wd5.myworkdayjobs.com/REC_RTX_Ext_Gateway/job/"
    "US-UT-WEST-VALLEY-CITY-338/Software-Engineering-Intern--Summer-2027-_01886446"
)


@pytest.mark.parametrize(
    ("link", "merges"),
    [
        ("https://zapply.jobs/l/d/workday-globalhr-rec-rtx-ext-gateway-01886446", True),
        ("https://zapply.jobs/l/d/workday-globalhr-rec-rtx-ext-gateway-01886447", False),
    ],
)
def test_a_zapply_workday_link_names_its_opening(link: str, merges: bool) -> None:
    from stage.normalize import canonical_apply_url

    board = job(
        "workday:a:1",
        "workday",
        "Pratt & Whitney Canada",
        "Software Engineering Intern",
        url=canonical_apply_url(GATEWAY),
    )
    listed = job(
        "zapply:b:2", "zapply", "RTX", "Software Engineering Intern", url=canonical_apply_url(link)
    )

    assert (would_merge(board, listed).kind is MatchKind.URL) is merges
    assert bool(resolve_duplicates([listed], [board])) is merges


@pytest.mark.parametrize(
    ("one", "two", "merges"),
    [
        (
            (
                "Microsoft",
                "Software Engineer Intern - Redmond",
                "Redmond, Washington, United States",
            ),
            ("Microsoft", "Software Engineer Intern - Cambridge", "Cambridge, MA"),
            False,
        ),
        (
            ("Visa", "Software Engineer, Intern - Foster City 2027", "US - Foster City, CA"),
            ("Visa", "Software Engineer Intern", "Ashburn, VA"),
            False,
        ),
        (
            ("Visa", "Software Engineer, Intern - Foster City 2027", "US - Foster City, CA"),
            ("Visa", "Software Engineer Intern", "Foster City, CA"),
            True,
        ),
        (
            ("HP", "Software Product Security Intern - HP IQ", "San Francisco, California"),
            ("HP Inc.", "Software Product Security Intern, HP IQ", "San Francisco, CA"),
            True,
        ),
        (
            ("Sherwin-Williams", "R&D Data Visualization Intern", "Brecksville, OH"),
            (
                "Sherwin-Williams",
                "2027 R&D Data Visualization Intern- Cleveland",
                "Brecksville, OH, United States",
            ),
            True,
        ),
        (
            (
                "Peraton",
                "Mission Systems Edge Computing Intern \u2013 Norman, OK",
                "Home, Oklahoma",
            ),
            ("Peraton", "Mission Systems Edge Computing Intern - Norman - OK", "Oklahoma, USA"),
            True,
        ),
        (
            ("Acme", "Data Analyst Intern - New York", "Remote"),
            ("Acme", "Data Analyst Intern - Chicago", "Remote"),
            False,
        ),
        (
            ("Engramme", "Back-End Software Engineer Co-op Intern", "SF"),
            ("Engramme", "Back-End Software Engineer Co-op", "San Francisco, CA"),
            True,
        ),
        (
            ("Engramme", "Back-End Software Engineer Intern", "SF"),
            ("Engramme", "Back-End Software Engineer Co-op", "San Francisco, CA"),
            False,
        ),
        (
            (
                "National Information Solutions Cooperative",
                "Intern - Information Security (Cybersecurity)",
                "Lake St. Louis, MO",
            ),
            (
                "National Information Solutions Cooperative",
                "Information Security (Cybersecurity) Intern",
                "Lake St. Louis, MO",
            ),
            True,
        ),
    ],
)
def test_places_and_team_names_in_titles_are_read_as_such(
    one: tuple[str, str, str], two: tuple[str, str, str], merges: bool
) -> None:
    board = _listed("workday:a:1", "workday", *one)
    listed = _listed("vanshb03:b:2", "vanshb03", *two)

    assert bool(would_merge(board, listed)) is merges


def test_a_lettered_workday_number_matches_its_zapply_link() -> None:
    from stage.normalize import canonical_apply_url

    page = "https://leidos.wd5.myworkdayjobs.com/External/job/Fort-Belvoir-VA/Software-Intern_R-00195576"
    board = job(
        "workday:a:1", "workday", "Leidos", "Software Intern", url=canonical_apply_url(page)
    )
    link = canonical_apply_url("https://zapply.jobs/l/d/workday-leidos-external-R-00195576")
    listed = job("zapply:b:2", "zapply", "Leidos", "Software Developer Intern", url=link)

    assert would_merge(board, listed).kind is MatchKind.URL


@pytest.mark.parametrize(
    ("one", "two", "merges"),
    [
        (
            ("Lawrence Livermore National Laboratory (LLNL)", "Power Systems Analytics Intern"),
            ("LLNL", "Power Systems Analytics Intern"),
            True,
        ),
        (("Cisco (Workday)", "Software Intern"), ("Workday", "Software Intern"), False),
        (
            (
                "LLNL",
                "Cybersecurity Undergraduate Intern - Cyber and Critical Infrastructure Summer...",
            ),
            (
                "LLNL",
                "Cybersecurity Undergraduate Intern - Cyber and Critical Infrastructure "
                "Summer Institute - Summer 2027",
            ),
            True,
        ),
        (("Acme", "Software Engineer..."), ("Acme", "Software Engineer Intern - Backend"), False),
        (
            ("Dow Jones", "Data Analyst Intern"),
            ("Dow Jones", "Summer 2027 Internship Program \u2013 Data Analyst Intern"),
            True,
        ),
    ],
)
def test_acronyms_cut_titles_and_program_names_join_their_posting(
    one: tuple[str, str], two: tuple[str, str], merges: bool
) -> None:
    board = _listed("smartrecruiters:a:1", "smartrecruiters", *one, "Livermore, CA")
    listed = _listed("zapply:b:2", "zapply", *two, "Livermore, CA")

    assert bool(would_merge(board, listed)) is merges
    assert bool(resolve_duplicates([listed], [board])) is merges


@pytest.mark.parametrize(
    ("board", "listed"),
    [
        (
            ("BorgWarner", "Software Engineering Intern (Year-Round)"),
            ("BorgWarner", "Software Engineering Intern"),
        ),
        (
            ("RTX", "Intern: Summer 2027 - Control/Diagnostic Software (Onsite)"),
            ("RTX", "Control/Diagnostic Software Intern"),
        ),
        (("Mindex", "Software Engineer Co-Op - On-site"), ("Mindex", "Software Engineer Co-op")),
        (
            ("Royal Bank of Canada", "2027 Capital Markets, AI Engineer (16 Months, Co-op)"),
            ("Royal Bank of Canada", "AI Engineer Co-op, Capital Markets"),
        ),
        (
            ("Insulet", "Co-op, Embedded Software Test Engineering: January - June 2027 (Onsite)"),
            ("Insulet", "Embedded Software Test Engineering Co-op"),
        ),
        (
            ("KLA", "Software Engineering Intern (C++, Unix)"),
            ("KLA", "Software Engineering Intern, C++ and Unix"),
        ),
        (("OLG", "Front End Developer Student"), ("OLG", "Front End Developer Intern")),
        (
            ("Amazon", "Frontier AI & Robotics Intern - Undergraduate"),
            ("Amazon.com Services LLC", "Frontier AI & Robotics Intern"),
        ),
        (
            ("Great American Insurance Company", "Emerging Technology Intern - Summer 2027"),
            ("National Interstate Insurance", "Emerging Technology Intern"),
        ),
        (
            ("Leidos", "Software Engineering & AI Intern"),
            ("Kudu Dynamics", "Software Engineering & AI Intern"),
        ),
        (("Shield AI", "Embedded Firmware Intern - 5810"), ("Shield", "Embedded Firmware Intern")),
    ],
)
def test_extra_words_on_one_copy_do_not_hide_the_same_posting(
    board: tuple[str, str], listed: tuple[str, str]
) -> None:
    one = _listed("workday:a:1", "workday", *board, "Milpitas, CA")
    two = _listed("vanshb03:b:2", "vanshb03", *listed, "Milpitas, CA")

    assert would_merge(one, two).kind is MatchKind.SAME_LANGUAGE, board[1]


@pytest.mark.parametrize(
    ("one", "two"),
    [
        ("Software Co-op (4 months)", "Software Co-op (8 months)"),
        ("Software Co-op: January - June 2027", "Software Co-op: June - December 2027"),
        ("Software Intern (Remote)", "Software Intern (Onsite)"),
        ("Software Intern (Undergraduate)", "Software Intern (Graduate)"),
        ("Software Intern (Summer)", "Software Intern (Year-Round)"),
    ],
)
def test_two_copies_naming_different_terms_or_work_modes_stay_apart(one: str, two: str) -> None:
    board = _listed("workday:a:1", "workday", "Acme", one, "Austin, TX")
    listed = _listed("simplify:b:2", "simplify", "Acme", two, "Austin, TX")

    assert not would_merge(board, listed)


def test_two_remote_copies_agree_unless_they_name_different_countries() -> None:
    from stage.domain import RemoteScope

    board = replace(
        _listed("workday:a:1", "workday", "GE Aerospace", "AI Intern", "Remote"),
        location=LocationBucket.UNKNOWN,
        remote_scope=RemoteScope.UNSPECIFIED,
    )
    listed = replace(
        _listed("vanshb03:b:2", "vanshb03", "GE Aerospace", "AI Intern", "Remote / United States"),
        remote_scope=RemoteScope.UNSPECIFIED,
    )
    canada = replace(listed, remote_scope=RemoteScope.CANADA, location=LocationBucket.CANADA)
    usa = replace(listed, remote_scope=RemoteScope.US)

    assert would_merge(board, listed)
    assert not would_merge(canada, usa)
    assert not would_merge(replace(board, remote_scope=None), listed)


def test_a_visa_style_requisition_names_its_opening() -> None:
    from stage.dedup.identity import opening_key

    early = "https://visa.wd5.myworkdayjobs.com/Visa_Early_Careers/job/US---Foster-City-CA/Data-Science-Intern_REF089883W"
    general = "https://visa.wd5.myworkdayjobs.com/Visa/job/US---Foster-City-CA/Data-Science-Intern_REF089883W-1"

    assert opening_key(early) == opening_key(general) == "visa:089883"


def test_one_list_holding_a_job_twice_folds_into_the_board_posting() -> None:
    from stage.normalize import canonical_apply_url

    early = "https://visa.wd5.myworkdayjobs.com/Visa_Early_Careers/job/US---Foster-City-CA/Software-Engineer-Intern_REF089875W"
    general = "https://visa.wd5.myworkdayjobs.com/Visa/job/US---Foster-City-CA/Software-Engineer-Intern_REF089875W"
    board = _listed(
        "workday:visa:1",
        "workday",
        "Visa",
        "Software Engineer, Intern - Foster City 2027",
        "US - Foster City, CA",
        canonical_apply_url(early),
    )
    first = _listed(
        "simplify:visa:2",
        "simplify",
        "Visa",
        "Software Engineer Intern",
        "Foster City, CA",
        canonical_apply_url(early),
    )
    second = _listed(
        "simplify:visa:3",
        "simplify",
        "Visa",
        "Software Engineer Intern",
        "Foster City, CA",
        canonical_apply_url(general),
    )
    other = _listed(
        "simplify:visa:4",
        "simplify",
        "Visa",
        "Software Engineer Intern 2027",
        "Foster City, CA",
        canonical_apply_url(general.replace("089875", "089919")),
    )

    linked = {link.duplicate_id for link in resolve_duplicates([first, second, other], [board])}

    assert linked == {first.id, second.id}


def _posted(job_id: str, title: str, place: str, term: str = "unknown") -> Job:
    return replace(_listed(job_id, "workday", "ABB", title, place), term=term)


def test_one_board_posting_a_title_twice_in_one_city_shows_it_once() -> None:
    title = "Automation Engineer Intern - Summer 2027"
    first = _posted("workday:abb:1", title, "New Berlin, WI")
    second = _posted("workday:abb:2", title.replace("-", "\u2013"), "New Berlin, WI")

    links = resolve_duplicates([first, second], [])

    assert [(link.duplicate_id, link.canonical_id, link.kind) for link in links] == [
        (second.id, first.id, "repost")
    ]


@pytest.mark.parametrize(
    ("title", "place", "term"),
    [
        ("Automation Engineer Intern", "Auburn Hills, MI", "unknown"),
        ("Automation Engineer Intern", "New Berlin, WI", "fall-2027"),
        ("Automation Engineer Intern II", "New Berlin, WI", "summer-2027"),
    ],
)
def test_another_city_term_or_title_on_one_board_stays_its_own_posting(
    title: str, place: str, term: str
) -> None:
    first = _posted("workday:abb:1", "Automation Engineer Intern", "New Berlin, WI", "summer-2027")
    second = _posted("workday:abb:2", title, place, term)

    assert not resolve_duplicates([first, second], [])


@pytest.mark.parametrize("place", ["2 Locations", ""])
def test_one_title_twice_with_no_place_named_stays_two_postings(place: str) -> None:
    first = _posted("workday:abb:1", "Automation Engineer Intern", place)
    second = _posted("workday:abb:2", "Automation Engineer Intern", place)

    assert not resolve_duplicates([first, second], [])


@pytest.mark.parametrize("place", ["Remote", "CAD Remote \u2013 ON", "US-California-Palo Alto"])
def test_one_title_twice_at_the_same_written_place_shows_it_once(place: str) -> None:
    first = _posted("workday:abb:1", "Full-stack AI Developer Intern", place)
    second = _posted("workday:abb:2", "Full-stack AI Developer Intern", place)

    links = resolve_duplicates([first, second], [])

    assert [(link.duplicate_id, link.kind) for link in links] == [(second.id, "repost")]


def test_a_copy_with_no_place_joins_the_same_title_from_the_same_employer() -> None:
    board = replace(
        _listed(
            "custom_json:crim:91547", "custom_json", "Luqia", "Stagiaire \u2013 IA de Confiance", ""
        ),
        location=LocationBucket.UNKNOWN,
    )
    listed = replace(
        _listed(
            "espresso-jobs:search:245824",
            "espresso-jobs",
            "Luqia",
            "Stagiaire \u2013 IA de Confiance",
            "Montr\u00e9al, Qu\u00e9bec, Canada",
        ),
        location=LocationBucket.MONTREAL,
    )
    other = replace(listed, id="espresso-jobs:search:1", title_raw="Stagiaire \u2013 Vision")

    assert would_merge(board, listed)
    assert not would_merge(board, other)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            (
                "MITRE",
                "Internships in Computer Science or Software Engineering",
                "McLean, Virginia",
            ),
            ("MITRE", "Computer Science or Software Engineering Intern", "McLean, VA"),
        ),
        (
            ("Valon Tech", "Software Engineer Intern", "New York City, NY +1"),
            ("Valon", "Software Engineer Intern", "New York, NY / San Francisco, CA"),
        ),
    ],
)
def test_internships_in_a_field_and_a_shortened_employer_name_match(
    first: tuple[str, str, str], second: tuple[str, str, str]
) -> None:
    left = _listed("zapply:mitre:1", "zapply", *first)
    right = _listed("vanshb03:mitre:2", "vanshb03", *second)

    assert would_merge(left, right)


def test_a_season_glued_to_its_year_still_reads_as_one() -> None:
    board = _listed(
        "lever:tevora:1",
        "lever",
        "Tevora",
        "Cybersecurity Internship Summer2027 - Federal",
        "Fairfax, VA / Irvine, CA",
    )
    listed = _listed(
        "vanshb03:tevora:2",
        "vanshb03",
        "Tevora",
        "Cybersecurity Intern, Federal",
        "Fairfax, VA / Irvine, CA",
    )

    assert would_merge(board, listed).kind is MatchKind.SAME_LANGUAGE


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            ("Teledyne", "Software Engineering Coop", "Waterloo, ON"),
            ("Teledyne Technologies Incorporated", "Software Engineering Co-op", "Waterloo, ON"),
        ),
        (
            (
                "Qualcomm",
                "FY27 Intern - Machine Learning Engineering Internship - Canada (4 months)",
                "Markham, ON",
            ),
            ("Qualcomm", "Machine Learning Engineering Intern", "Markham, ON"),
        ),
        (
            (
                "RTX",
                "Internship \u2013 Raytheon Systems Security Engineer -Onsite",
                "Tewksbury, MA",
            ),
            ("RTX", "Systems Security Engineer Intern", "Tewksbury, MA"),
        ),
        (
            (
                "Federal Reserve System",
                "2027 Summer Internship Program - Information Security Intern",
                "Atlanta, GA",
            ),
            ("The Federal Reserve System", "Information Security Intern", "Atlanta, GA"),
        ),
    ],
)
def test_one_posting_written_two_ways_is_one(
    first: tuple[str, str, str], second: tuple[str, str, str]
) -> None:
    board = _listed("workday:a:1", "workday", *first)
    listed = _listed("vanshb03:a:2", "vanshb03", *second)

    assert would_merge(board, listed).kind is MatchKind.SAME_LANGUAGE


def test_two_titles_naming_different_countries_stay_apart() -> None:
    canada = _listed(
        "simplify:a:1",
        "simplify",
        "Tenstorrent",
        "Systems Intern - Validation (Canada)",
        "Toronto, ON",
    )
    usa = _listed(
        "vanshb03:a:2",
        "vanshb03",
        "Tenstorrent",
        "Systems Intern - Validation (USA)",
        "Toronto, ON",
    )

    assert not would_merge(canada, usa)


def test_a_repost_reads_internship_as_intern() -> None:
    from stage.dedup import repost_key

    first = repost_key("Motorola", "2027 Software Engineering Summer Internship", "Plantation, FL")
    second = repost_key("Motorola", "Software Engineering Intern - Summer 2027", "Plantation, FL")

    assert first and first == second


def _proven(index: int, company: str, source: str) -> Job:
    url = f"https://acme.wd1.myworkdayjobs.com/External/job/Austin-TX/Intern_R{index:05d}"
    return _listed(f"{source}:x:{index}", source, company, f"Intern {index}", "Austin, TX", url)


def test_two_proven_postings_teach_stage_that_two_names_are_one_employer() -> None:
    from stage.dedup.resolve import learned_employers

    once = [_proven(1, "Initech", "workday"), _proven(1, "Initrode Labs", "simplify")]
    twice = [*once, _proven(2, "Initech", "workday"), _proven(2, "Initrode Labs", "simplify")]

    assert learned_employers(once, frozenset()) == {}
    learned = learned_employers(twice, frozenset())
    assert learned["initech"] == learned["initrode labs"]


def test_a_learned_name_joins_a_copy_that_matches_only_by_title() -> None:
    proofs = [
        _proven(index, company, source)
        for index in (1, 2)
        for company, source in (("Initech", "workday"), ("Initrode Labs", "simplify"))
    ]
    board = _listed("workday:x:9", "workday", "Initech", "Data Engineer Intern", "Austin, TX")
    listed = _listed(
        "simplify:x:9", "simplify", "Initrode Labs", "Data Engineer Intern", "Austin, TX"
    )

    links = resolve_duplicates([listed], [board, *proofs])

    assert (listed.id, board.id) in {(link.duplicate_id, link.canonical_id) for link in links}
    assert not resolve_duplicates([listed], [board]), "without proof the names stay apart"


def test_a_repost_with_no_term_joins_but_two_named_terms_never_do() -> None:
    title, place = "Automation Engineer Intern", "New Berlin, WI"
    summer = _posted("workday:abb:1", title, place, "summer-2027")
    unnamed = _posted("workday:abb:2", title, place)
    fall = _posted("workday:abb:3", title, place, "fall-2027")

    alone = {link.duplicate_id for link in resolve_duplicates([summer, unnamed], [])}
    linked = {
        (link.duplicate_id, link.canonical_id)
        for link in resolve_duplicates([summer, unnamed, fall], [])
    }

    assert alone == {unnamed.id}
    assert (fall.id, summer.id) not in linked and (unnamed.id, fall.id) not in linked


@pytest.mark.parametrize(
    ("board", "listed", "merges"),
    [
        (
            ("2027 Software Engineering Intern - Roy UT", "United States-Utah-Roy"),
            ("Software Engineer Intern", "Linthicum, MD"),
            False,
        ),
        (
            (
                "Software Engineering Intern (Summer 2027)",
                "US-CO-AURORA-S77 ~ 16470 E Hughes Dr ~ BLDG S77",
            ),
            ("Software Engineer Intern", "Cambridge, MA"),
            False,
        ),
        (
            ("2027 Software Engineering Intern - Roy UT", "United States-Utah-Roy"),
            ("Software Engineering Intern", "Roy, UT"),
            True,
        ),
        (
            (
                "Software Engineering Intern (Summer 2027)",
                "US-CO-AURORA-S77 ~ 16470 E Hughes Dr ~ BLDG S77",
            ),
            ("Software Engineer Intern", "Aurora, CO"),
            True,
        ),
    ],
)
def test_a_city_in_the_title_or_a_site_code_keeps_two_cities_apart(
    board: tuple[str, str], listed: tuple[str, str], merges: bool
) -> None:
    one = _listed("workday:a:1", "workday", "RTX", *board)
    two = _listed("vanshb03:b:2", "vanshb03", "RTX", *listed)

    assert bool(would_merge(one, two)) is merges


def test_postings_that_name_different_terms_never_merge() -> None:
    board = job("lever:acme:1", "lever", "Acme", "Cybersecurity Internship", term="winter-2027")
    listed = job(
        "simplify:feed:1", "simplify", "Acme", "Cybersecurity Internship", term="summer-2027"
    )
    assert not would_merge(board, listed)


def test_a_copy_with_no_term_never_joins_two_named_terms_into_one_posting() -> None:
    summer = job("greenhouse:acme:1", "greenhouse", "Acme", "SWE Intern", term="summer-2027")
    plain = job("simplify:feed:1", "simplify", "Acme", "SWE Intern")
    fall = job("zapply:feed:1", "zapply", "Acme", "SWE Intern", term="fall-2027")

    links = resolve_duplicates([summer, plain, fall], [])

    mains = {link.duplicate_id: link.canonical_id for link in links}
    assert mains.get("zapply:feed:1") != "greenhouse:acme:1"
    assert mains.get("greenhouse:acme:1") != "zapply:feed:1"


def test_a_copy_with_no_term_still_joins_a_posting_whose_copies_disagree() -> None:
    url = "https://job-boards.greenhouse.io/castelion/jobs/4386047009"
    board = job(
        "greenhouse:castelion:1",
        "greenhouse",
        "Castelion",
        "SWE Intern",
        term="spring-2027",
        url=url,
    )
    labelled = job(
        "simplify:feed:1", "simplify", "Castelion", "SWE Intern", term="fall-2026", url=url
    )
    plain = job("vanshb03:feed:1", "vanshb03", "Castelion", "SWE Intern")

    links = resolve_duplicates([board, labelled, plain], [])

    mains = {link.duplicate_id: link.canonical_id for link in links}
    assert mains.get("simplify:feed:1") is not None
    assert mains.get("vanshb03:feed:1") == mains.get("simplify:feed:1")


def test_a_list_link_with_an_apply_tail_matches_the_lever_posting() -> None:
    from stage.normalize import canonical_apply_url

    posting = "https://jobs.lever.co/acme/1234abcd-1234-1234-1234-123456789abc"
    board = job(
        "lever:acme:1", "lever", "Acme", "Software Intern", url=canonical_apply_url(posting)
    )
    linked = canonical_apply_url(f"{posting}/apply?lever-source=Simplify")
    listed = job("simplify:feed:1", "simplify", "Acme", "Intern, Software", url=linked)
    assert would_merge(board, listed).kind is MatchKind.URL


def test_a_greenhouse_id_on_the_employer_site_matches_the_board_posting() -> None:
    board = job(
        "greenhouse:acme:4567890",
        "greenhouse",
        "Acme",
        "Software Intern",
        url="https://job-boards.greenhouse.io/acme/jobs/4567890",
    )
    listed = job(
        "simplify:feed:2",
        "simplify",
        "Acme Corp",
        "SWE Intern",
        url="https://www.acme.com/careers?gh_jid=4567890",
    )
    assert would_merge(board, listed).kind is MatchKind.URL


@pytest.mark.parametrize(
    ("url", "named"),
    [
        (
            "https://jobs.lever.co/acme/8b0c1a2e-1234-4abc-9def-0123456789ab",
            ("lever:*", "8b0c1a2e-1234-4abc-9def-0123456789ab"),
        ),
        (
            "https://jobs.ashbyhq.com/acme/8b0c1a2e-1234-4abc-9def-0123456789ab?utm_source=x",
            ("ashby:*", "8b0c1a2e-1234-4abc-9def-0123456789ab"),
        ),
        (
            "https://jobs.smartrecruiters.com/Acme/744000123456789-software-intern",
            ("smartrecruiters:*", "744000123456789"),
        ),
        (
            "https://careers.smartrecruiters.com/Acme/744000123456789",
            ("smartrecruiters:*", "744000123456789"),
        ),
        (
            "https://zapply.jobs/l/d/workday-acme-software-intern-r12345",
            ("workday:acme-*", "12345"),
        ),
        ("https://boards.greenhouse.io/acme/jobs/4012345", ("greenhouse:*", "4012345")),
        ("https://job-boards.greenhouse.io/acme/jobs/4012345", ("greenhouse:*", "4012345")),
        ("https://notgreenhouse.io/acme/jobs/4012345", ("", "")),
        ("https://example.com/careers?gh_jid=4012345", ("greenhouse:*", "4012345")),
        ("https://example.com/jobs/1", ("", "")),
        ("not a url", ("", "")),
    ],
)
def test_a_link_names_the_board_posting_it_points_at(url: str, named: tuple[str, str]) -> None:
    from stage.normalize.urls import named_posting

    assert named_posting(url) == named


@pytest.mark.parametrize(
    ("identifier", "tail", "names"),
    [
        ("greenhouse:acme:4012345", "4012345", True),
        ("workday:acme-ext:software-intern_r12345", "12345", True),
        ("workday:acme-ext:r112345", "12345", False),
        ("lever:acme:abc", "abc", True),
        ("x:1234567", "4567", False),
    ],
)
def test_a_job_number_names_a_posting_only_on_a_digit_boundary(
    identifier: str, tail: str, names: bool
) -> None:
    from stage.normalize.urls import names_posting

    assert names_posting(identifier, tail) is names
