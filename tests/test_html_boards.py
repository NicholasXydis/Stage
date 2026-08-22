from pathlib import Path
from urllib.parse import urlsplit

import pytest

from stage.domain import Company, CustomBoard, Platform
from stage.sources.custom_json import _project, html_rows, jsonld_rows, sitemap_rows

FIXTURES = Path(__file__).parent / "fixtures"

BLACKROCK = CustomBoard(
    url="https://careers.blackrock.com/search-jobs?p=1",
    fmt="html",
    row_selector="li.section3__search-results-li",
    fields={
        "title": "h2.section3__job-title",
        "id": "a.section3__search-results-a::attr(data-job-id)",
        "url": "a.section3__search-results-a::attr(href)",
        "location": "span.section3__job-location",
    },
)

NBC = CustomBoard(
    url="https://emplois.bnc.ca/en_CA/careers/searchjobs/",
    fmt="html",
    row_selector="table tr:has(a[data-map='job-detail-link'])",
    fields={
        "title": "a[data-map='job-detail-link']",
        "id": "a[data-map='job-detail-link']::attr(href)",
        "url": "a[data-map='job-detail-link']::attr(href)",
        "location": "td:nth-of-type(1)",
    },
)


SHOPIFY = CustomBoard(
    url="https://internships.shopify.com/",
    fmt="html",
    row_selector='a[href*="/careers/"][href*="_"]',
    fields={
        "title": "::slug(href)",
        "id": "::slugid(href)",
        "url": "::attr(href)",
    },
)


LIGHTSPEED = CustomBoard(
    url="https://www.lightspeedhq.com/careers/openings/",
    fmt="html",
    row_selector="li.job",
    fields={
        "title": "span.job-title",
        "location": "span.job-office",
        "department": "span.job-dept",
        "id": "a::slugid(href)",
        "url": "a::attr(href)",
    },
)


TWOSIGMA = CustomBoard(
    url="https://careers.twosigma.com/careers/OpenRoles/?listFilterMode=1&jobRecordsPerPage=10",
    fmt="html",
    row_selector="article.article--result",
    fields={
        "title": "h3.article__header__text__title a",
        "id": "h3.article__header__text__title a::slugid(href)",
        "url": "h3.article__header__text__title a::attr(href)",
        "location": "div.article__header__content__text > span.paragraph_inner-span",
        "department": "div.article__header__content__sub-text > span.paragraph_inner-span",
        "employment_type": (
            "div.article__header__content__sub-text > span.paragraph_inner-span:nth-of-type(2)"
        ),
    },
    page_param="jobOffset",
    page_size=10,
    page_step=10,
    max_pages=12,
)


GILDAN = CustomBoard(
    url="https://gildancorp.com/en/careers/open-positions/",
    fmt="jsonld",
    fields={
        "title": "title",
        "id": "identifier.value",
        "url": "url",
        "location": "jobLocation.name",
        "description": "description",
        "employment_type": "employmentType",
        "category": "jobCategory",
    },
    page_param="page",
    page_size=10,
    page_start=1,
    page_step=1,
    max_pages=10,
)


TALENTBREW_FIELDS = {
    "title": "h2",
    "id": "a::attr(data-job-id)",
    "url": "a::attr(href)",
    "location": ".job-location",
    "category": ".job-category",
}

L3HARRIS = CustomBoard(
    url="https://careers.l3harris.com/en/search-jobs",
    fmt="html",
    row_selector="#search-results-list li",
    fields=TALENTBREW_FIELDS,
    page_param="p",
    page_size=1,
    page_start=1,
    page_step=1,
    max_pages=12,
)

DISNEY = CustomBoard(
    url="https://www.disneycareers.com/en/search-jobs/internship/391/1",
    fmt="html",
    row_selector="#search-results-list li",
    fields=TALENTBREW_FIELDS,
    page_param="p",
    page_size=1,
    page_start=1,
    page_step=1,
    max_pages=12,
)


SFRMK_SEARCH = CustomBoard(
    url="https://careers.ey.com/search/?q=internship",
    fmt="html",
    row_selector="tr.data-row",
    authoritative=False,
    fields={
        "title": "a.jobTitle-link",
        "id": "a.jobTitle-link::slugid(href)",
        "url": "a.jobTitle-link::attr(href)",
        "location": ".jobLocation",
    },
    page_param="startrow",
    page_size=25,
    page_step=25,
    max_pages=8,
)


PUBLICIS = CustomBoard(
    url="https://careers.publicissapient.com/job-details-sitemap",
    fmt="html",
    row_selector='.careersJobsSitemap a[href*="job-details/"]',
    fields={"title": "::slug(href)", "id": "::attr(href)", "url": "::attr(href)"},
)


PARSONS = CustomBoard(
    url="https://jobs.parsons.com/career-search",
    fmt="html",
    row_selector=".career20_top-wrapper",
    authoritative=False,
    fields={
        "title": ".heading-style-h4",
        "category": ".tag",
        "id": 'a[data-action="view_job"]::attr(href)',
        "url": 'a[data-action="view_job"]::attr(href)',
    },
)


LOBLAW = CustomBoard(
    url="https://careers.loblaw.ca/jobs",
    fmt="html",
    row_selector=".results-list__item",
    fields={
        "title": ".results-list__item-title--link",
        "id": ".results-list__item-title--link::attr(href)",
        "url": ".results-list__item-title--link::attr(href)",
        "location": ".results-list__item-street--label",
    },
    authoritative=False,
)


CROESUS = CustomBoard(
    url="https://cezanneondemand.intervieweb.it/croesus/en/career",
    fmt="html",
    row_selector="div.vacancy__header",
    fields={
        "title": "div.vacancy__title h3",
        "url": "div.vacancy__title a::attr(href)",
        "location": 'span.subtitle__informations[title="Location"]',
        "category": 'span.subtitle__informations[title="Functional Area"]',
    },
)


SEGA = CustomBoard(
    url="https://careers.sega.co.uk/vacancies",
    fmt="html",
    row_selector="div.job-summary.views-row",
    fields={
        "title": "div.views-field-title a",
        "id": "div.views-field-title a::slugid(href)",
        "url": "div.views-field-title a::attr(href)",
        "location": "div.views-field-field-country",
        "category": "div.views-field-field-department",
        "description": "div.views-field-field-description-brief",
    },
)


RENTEC = CustomBoard(
    url="https://www.rentec.com/Careers.action?jobs=true",
    fmt="html",
    row_selector="div.flex-auto:not(.flex)",
    fields={
        "title": "a",
        "url": "a::attr(href)",
    },
)


def _rows(name: str, board: CustomBoard) -> list[dict[str, str]]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return [_project(block, board) for block in html_rows(text, board.row_selector)]


def test_the_blackrock_selectors_still_match_its_saved_page() -> None:
    rows = _rows("blackrock_search.html", BLACKROCK)
    assert len(rows) == 3, "BlackRock row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a BlackRock row lost its title"
        assert row["id"].isdigit(), f"BlackRock id is not a job id: {row['id']!r}"
        assert row["url"].startswith("https://careers.blackrock.com/job/"), row["url"]
        assert row["location"], "a BlackRock row lost its location"


def test_the_national_bank_selectors_still_match_its_saved_page() -> None:
    rows = _rows("nbc_search.html", NBC)
    assert len(rows) == 3, "National Bank row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a National Bank row lost its title"
        assert "/careers/JobDetail/" in row["url"], row["url"]
        assert row["location"], "a National Bank row lost its location"


def test_a_relative_href_is_resolved_against_the_board_url() -> None:
    rows = _rows("blackrock_search.html", BLACKROCK)
    assert all(row["url"].startswith("https://") for row in rows), "relative href left unresolved"


def test_shopify_takes_its_title_from_the_href_slug_not_the_link_text() -> None:
    rows = _rows("shopify_internships.html", SHOPIFY)
    assert rows, "Shopify internship anchors stopped matching the saved microsite"
    for row in rows:
        assert "Sign up" not in row["title"], "title fell back to the anchor text"
        assert row["title"] == "Software Engineering Internships Winter 2027", row["title"]
        assert row["id"] == "404bb82e-37f3-4a78-b0f3-12923a7c4856", row["id"]
        assert row["url"].startswith("https://www.shopify.com/careers/"), row["url"]


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/careers/software-engineer-intern_abc123</loc></url>
  <url><loc>https://example.test/careers/data-analyst_def456</loc></url>
</urlset>"""


def test_a_sitemap_yields_a_title_and_id_derived_from_each_url() -> None:
    rows = sitemap_rows(SITEMAP)
    assert [row["title"] for row in rows] == ["Software Engineer Intern", "Data Analyst"], rows
    assert [row["id"] for row in rows] == ["abc123", "def456"], rows
    assert rows[0]["loc"].startswith("https://example.test/careers/"), rows[0]


def test_a_sitemap_entry_without_a_trailing_id_still_keeps_its_slug() -> None:
    rows = sitemap_rows(
        "<urlset><url><loc>https://example.test/careers/lead-designer</loc></url></urlset>"
    )
    assert rows[0]["title"] == "Lead Designer", rows
    assert rows[0]["id"] == "lead-designer", rows


def test_a_document_with_no_url_entries_yields_no_rows() -> None:
    assert sitemap_rows("<urlset></urlset>") == [], "sitemap parser invented rows"


def test_the_lightspeed_selectors_still_match_its_saved_page() -> None:
    rows = _rows("lightspeed_openings.html", LIGHTSPEED)
    assert len(rows) == 3, "Lightspeed row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a Lightspeed row lost its title"
        assert row["location"], "a Lightspeed row lost its office"
        assert len(row["id"]) == 36, f"Lightspeed id is not the posting uuid: {row['id']!r}"
        assert row["url"].startswith("https://www.lightspeedhq.com/careers/job/"), row["url"]


def test_a_selector_that_matches_nothing_returns_no_rows_rather_than_guessing() -> None:
    text = (FIXTURES / "blackrock_search.html").read_text(encoding="utf-8")
    assert html_rows(text, "li.does-not-exist") == [], "a dead selector invented rows"


def test_the_two_sigma_selectors_still_match_its_saved_page() -> None:
    rows = _rows("twosigma_openroles.html", TWOSIGMA)
    assert len(rows) == 3, "Two Sigma row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a Two Sigma row lost its title"
        assert row["id"].isdigit(), f"Two Sigma id is not a job id: {row['id']!r}"
        assert "/careers/JobDetail/" in row["url"], row["url"]
        assert row["location"], "a Two Sigma row lost its location"
        assert row["department"], "a Two Sigma row lost its function"


def test_two_sigma_keeps_location_and_function_in_separate_fields() -> None:
    rows = _rows("twosigma_openroles.html", TWOSIGMA)
    for row in rows:
        assert row["location"] != row["department"], (
            "the location and function spans share a class, so a loose selector merges them"
        )


def _jsonld(name: str, board: CustomBoard) -> list[dict[str, str]]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return [_project(entry, board) for entry in jsonld_rows(text)]


def test_the_gildan_jsonld_still_matches_its_saved_page() -> None:
    rows = _jsonld("gildan_openpositions.html", GILDAN)
    assert len(rows) == 3, "Gildan ld+json stopped yielding its JobPosting blocks"
    for row in rows:
        assert row["title"], "a Gildan row lost its title"
        assert row["id"], "a Gildan row lost its requisition id"
        host = urlsplit(row["url"]).hostname or ""
        assert host == "icims.com" or host.endswith((".icims.com", ".dayforcehcm.com")), host
        assert row["location"], "a Gildan row lost its location"


def test_gildan_ignores_the_organization_and_breadcrumb_blocks() -> None:
    rows = _jsonld("gildan_openpositions.html", GILDAN)
    titles = [row["title"] for row in rows]

    assert "Gildan" not in titles, (
        "the Organization block was read as a posting, so the first ld+json script wins"
    )


def test_gildan_carries_the_body_its_visible_cards_omit() -> None:
    rows = _jsonld("gildan_openpositions.html", GILDAN)

    assert any(row["description"] for row in rows), (
        "ld+json was chosen over selectors for the body; losing it removes the reason"
    )


def test_gildan_ids_stay_distinct_across_its_two_backing_boards() -> None:
    rows = _jsonld("gildan_openpositions.html", GILDAN)
    assert len({row["id"] for row in rows}) == len(rows), (
        "Gildan mixes icims requisition numbers with dayforce ids, and they must not collide"
    )


def test_the_loblaw_selectors_still_match_its_saved_page() -> None:
    rows = _rows("loblaw_jobs.html", LOBLAW)
    assert len(rows) == 3, "Loblaw row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a Loblaw row lost its title"
        assert row["location"], "a Loblaw row lost its location"


def test_every_loblaw_row_carries_a_distinct_id() -> None:
    rows = _rows("loblaw_jobs.html", LOBLAW)
    assert len({row["id"] for row in rows}) == len(rows), (
        "Loblaw posting ids collapsed, so every posting would share one job_id"
    )


def test_the_parsons_selectors_still_match_its_saved_page() -> None:
    rows = _rows("parsons_search.html", PARSONS)
    assert len(rows) == 3, "Parsons row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a Parsons row lost its title"
        assert "/jobs/" in row["url"], row["url"]
    assert len({row["id"] for row in rows}) == len(rows), "Parsons posting ids collapsed"


def test_parsons_keeps_its_title_out_of_the_category_tag() -> None:
    for row in _rows("parsons_search.html", PARSONS):
        assert row["title"] != row["category"], (
            "the title and the category tag share a wrapper, so a loose selector merges them"
        )


def test_publicis_derives_its_title_from_the_href_not_the_anchor_text() -> None:
    rows = _rows("publicissapient_sitemap.html", PUBLICIS)
    assert len(rows) == 3, "the Publicis sitemap selector stopped matching its saved page"
    for row in rows:
        assert "careers.publicissapient.com" not in row["title"], (
            "the anchor text is the url itself, so the title must come from the slug"
        )
        assert "/job-details/" in row["url"], row["url"]
    assert len({row["id"] for row in rows}) == len(rows), "Publicis posting ids collapsed"


def test_the_successfactors_search_selectors_still_match_their_saved_page() -> None:
    rows = _rows("sfrmk_search.html", SFRMK_SEARCH)
    assert len(rows) == 3, "the SuccessFactors search row selector stopped matching"
    for row in rows:
        assert row["title"], "a SuccessFactors search row lost its title"
        assert row["id"].isdigit(), f"id is not a posting number: {row['id']!r}"
        assert "/job/" in row["url"], row["url"]
        assert row["location"], "a SuccessFactors search row lost its location"


def test_the_successfactors_search_board_is_declared_partial() -> None:
    from stage.companies import load_companies

    rows = {c.name: c for c in load_companies()}
    for name in ("EY", "Capgemini"):
        board = rows[name].custom
        assert board is not None and not board.authoritative, (
            f"{name} is a keyword slice of a larger board and must not close what it cannot see"
        )


@pytest.mark.parametrize(
    "board,name",
    [(L3HARRIS, "l3harris_search.html"), (DISNEY, "disney_search.html")],
)
def test_the_talentbrew_selectors_still_match_their_saved_pages(
    board: CustomBoard, name: str
) -> None:
    rows = _rows(name, board)
    assert len(rows) == 3, f"{name}: the talentbrew row selector stopped matching"
    for row in rows:
        assert row["title"], f"{name}: a row lost its title"
        assert row["id"].isdigit(), f"{name}: id is not a talentbrew job id: {row['id']!r}"
        assert "/job/" in row["url"], row["url"]
        assert row["location"], f"{name}: a row lost its location"


@pytest.mark.parametrize(
    "board,name",
    [(BLACKROCK, "blackrock_search.html"), (NBC, "nbc_search.html")],
)
def test_every_html_row_carries_the_identity_the_pipeline_requires(
    board: CustomBoard, name: str
) -> None:
    for row in _rows(name, board):
        assert row.get("title"), f"{name}: a row reached the pipeline without a title"
        assert row.get("id"), f"{name}: a row reached the pipeline without an id"


def test_the_croesus_selectors_still_match_its_saved_page() -> None:
    rows = _rows("croesus_career.html", CROESUS)
    assert len(rows) == 3, "Croesus row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a Croesus row lost its title"
        assert "/croesus/jobs/" in row["url"], row["url"]
        assert row["location"], "a Croesus row lost its location"


def test_croesus_keeps_location_and_functional_area_in_separate_fields() -> None:
    for row in _rows("croesus_career.html", CROESUS):
        assert row["location"] != row["category"], (
            "the location and functional-area spans share a class, so a loose selector merges them"
        )


def test_croesus_cannot_take_its_id_from_the_href_slug() -> None:
    slugged = dict(CROESUS.fields) | {"id": "div.vacancy__title a::slugid(href)"}
    rows = _rows(
        "croesus_career.html",
        CustomBoard(
            url=CROESUS.url,
            fmt="html",
            row_selector=CROESUS.row_selector,
            fields=slugged,
        ),
    )
    idents = {row["id"] for row in rows}
    assert idents == {"en"}, (
        "every Croesus href ends in a locale segment, so slugid collapses the board onto one id"
    )


def test_the_sega_selectors_still_match_its_saved_page() -> None:
    rows = _rows("sega_vacancies.html", SEGA)
    assert len(rows) == 25, "the SEGA row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "a SEGA row lost its title"
        assert row["url"].startswith("https://careers.sega.co.uk/vacancies/"), (
            "a SEGA row lost its vacancy href"
        )
        assert row["id"], "a SEGA row lost the id its href slug carries"
    assert any(row["location"] for row in rows), "SEGA rows lost the country field"


def test_the_rentec_selectors_skip_the_footer_block_that_carries_no_role() -> None:
    rows = _rows("rentec_careers.html", RENTEC)
    assert len(rows) == 12, "the Renaissance Technologies row selector stopped matching"
    for row in rows:
        assert row["title"], "a Renaissance Technologies row lost its title"
        assert "selectedPosition=" in row["url"], "a row lost the query param that identifies it"


def test_the_registry_rows_still_use_the_selectors_these_fixtures_pin() -> None:
    from stage.companies import load_companies

    rows = {company.name: company for company in load_companies()}
    for name, board in (
        ("BlackRock", BLACKROCK),
        ("National Bank of Canada", NBC),
        ("Shopify", SHOPIFY),
        ("Lightspeed", LIGHTSPEED),
        ("Two Sigma", TWOSIGMA),
        ("Gildan", GILDAN),
        ("Loblaw", LOBLAW),
        ("Parsons", PARSONS),
        ("L3Harris", L3HARRIS),
        ("Disney", DISNEY),
        ("Publicis Sapient", PUBLICIS),
        ("Croesus", CROESUS),
        ("SEGA Europe", SEGA),
        ("Renaissance Technologies", RENTEC),
    ):
        company: Company = rows[name]
        assert company.platform is Platform.CUSTOM_JSON, f"{name} left custom_json"
        assert company.custom is not None, f"{name} lost its custom block"
        assert company.custom.row_selector == board.row_selector, (
            f"{name} row_selector drifted from the one these fixtures pin"
        )
        assert dict(company.custom.fields) == dict(board.fields), (
            f"{name} field selectors drifted from the ones these fixtures pin"
        )


def test_job_bank_keeps_only_postings_the_board_itself_flags() -> None:
    from stage.sources.jobbank import declared_term, field, posting_id

    text = (FIXTURES / "jobbank_search.html").read_text(encoding="utf-8")
    rows = html_rows(text, "article")
    assert len(rows) == 3, "the Job Bank article selector stopped matching its saved page"
    for row in rows:
        assert posting_id(row).isdigit(), f"id is not a posting number: {posting_id(row)!r}"
        assert field(row, ".noctitle"), "a Job Bank row lost its title"
        assert field(row, ".business"), "a Job Bank row lost its employer"
        assert declared_term(row), "a Job Bank row lost the badge that makes it an internship"


def test_job_bank_never_builds_an_apply_url_from_the_session_bearing_href() -> None:
    from stage.sources.jobbank import POSTING, posting_id

    text = (FIXTURES / "jobbank_search.html").read_text(encoding="utf-8")
    row = html_rows(text, "article")[0]
    built = POSTING.format(id=posting_id(row))

    assert "jsessionid" not in built, (
        "the href carries a rotating jsessionid, so the url must be rebuilt from the numeric id"
    )
    assert built.endswith(posting_id(row)), built


def test_the_muse_query_covers_the_intended_cities() -> None:
    from stage.sources.themuse import LOCATIONS, TheMuseFeed

    url = TheMuseFeed()._url(1)

    assert url.count("location=") == len(LOCATIONS), "a location was dropped from the query"
    assert "level=Internship" in url, "the internship level filter left the query"
    for city in ("Montreal", "Toronto", "New%20York%20City", "San%20Francisco"):
        assert city in url, f"{city} missing from the Muse query"


def test_espresso_keeps_only_the_postings_the_board_badges_as_stage() -> None:
    from stage.sources.espresso import ROW_SELECTOR, badge, field

    text = (FIXTURES / "espresso_search.html").read_text(encoding="utf-8")
    rows = html_rows(text, ROW_SELECTOR)
    assert len(rows) == 3, "the Espresso-Jobs row selector stopped matching its saved page"
    badged = [row for row in rows if badge(row).lower() == "stage"]
    assert len(badged) == 2, "the fixture must hold both badged and unbadged rows to be evidence"
    for row in rows:
        assert field(row, "h2.job_index-content_list_item-title"), "a row lost its title"
        assert field(row, "p.job_index-content_list_item-company"), "a row lost its employer"


def test_espresso_files_each_posting_under_its_own_employer() -> None:
    from stage.sources.espresso import ROW_SELECTOR, field

    text = (FIXTURES / "espresso_search.html").read_text(encoding="utf-8")
    employers = {
        field(row, "p.job_index-content_list_item-company") for row in html_rows(text, ROW_SELECTOR)
    }
    assert len(employers) > 1, (
        "an aggregator must carry each posting's own employer, not one registry caption"
    )


def test_espresso_builds_its_apply_url_from_the_id_and_slug() -> None:
    from stage.sources.espresso import POSTING, ROW_SELECTOR

    row = html_rows((FIXTURES / "espresso_search.html").read_text(encoding="utf-8"), ROW_SELECTOR)[
        0
    ]
    built = POSTING.format(id=row.get("id"), slug=row.get("data-slug"))

    assert "/emploi/appliquer" not in built, (
        "robots.txt disallows the apply endpoint, so the posting url must be built instead"
    )
    assert str(row.get("id")) in built and str(row.get("data-slug")) in built, built


def test_espresso_reads_its_location_from_the_row_data_attributes() -> None:
    from stage.sources.espresso import ROW_SELECTOR, where

    text = (FIXTURES / "espresso_search.html").read_text(encoding="utf-8")
    for row in html_rows(text, ROW_SELECTOR):
        assert where(row).endswith("Canada"), where(row)


def test_a_sitemap_names_each_path_segment_from_the_end() -> None:
    loc = "https://careers.arm.com/job/cambridge/senior-tools-engineer/33099/98803698976"
    row = sitemap_rows(f"<urlset><url><loc>{loc}</loc></url></urlset>")[0]

    assert row["path1"] == "98803698976", "path1 must be the last segment"
    assert row["path3"] == "senior-tools-engineer", row["path3"]
    assert row["path3_title"] == "Senior Tools Engineer", row["path3_title"]
    assert row["path4_title"] == "Cambridge", row["path4_title"]


def test_a_sitemap_title_is_not_guessed_when_the_id_comes_last() -> None:
    loc = "https://careers.arm.com/job/cambridge/senior-tools-engineer/33099/98803698976"
    row = sitemap_rows(f"<urlset><url><loc>{loc}</loc></url></urlset>")[0]

    assert row["title"] == "98803698976", (
        "the default title takes the last segment, which is why these rows name a path segment"
    )


def test_the_sitemap_row_filter_drops_everything_that_is_not_a_posting() -> None:
    from stage.companies import load_companies

    rows = {company.name: company for company in load_companies()}
    for name in ("Arm", "Synopsys", "Siemens Digital Industries Software"):
        board = rows[name].custom
        assert board is not None and board.row_filter, (
            f"{name} reads a whole-site sitemap, so it needs a filter or it invents postings"
        )
        assert not board.authoritative, f"{name} is a sitemap slice and must never close postings"


def test_the_sitemap_boards_name_a_segment_rather_than_trusting_the_default_title() -> None:
    from stage.companies import load_companies

    rows = {company.name: company for company in load_companies()}
    for name in ("Arm", "Synopsys", "Siemens Digital Industries Software"):
        board = rows[name].custom
        assert board is not None
        assert board.mapped("title").startswith("path"), (
            f"{name} must name the path segment holding its title, not the trailing id"
        )


EA = CustomBoard(
    url="https://jobs.ea.com/en_US/careers/SearchJobs/?listFilterMode=1&jobRecordsPerPage=20",
    fmt="html",
    row_selector="article.article--result",
    fields={
        "title": "h3.article__header__text__title a",
        "id": "h3.article__header__text__title a::slugid(href)",
        "url": "h3.article__header__text__title a::attr(href)",
        "location": "span.list-item-location",
        "department": "span.list-item-department",
        "employment_type": "span.list-item-workerType",
    },
    page_param="jobOffset",
    page_size=20,
    max_pages=20,
)


def test_the_ea_selectors_still_match_its_saved_page() -> None:
    rows = _rows("ea_searchjobs.html", EA)
    assert len(rows) == 3, "the EA row selector stopped matching its saved page"
    for row in rows:
        assert row["title"], "an EA row lost its title"
        assert row["id"].isdigit(), f"EA id is not the role id: {row['id']!r}"
        assert "/JobDetail/" in row["url"], row["url"]
        assert row["location"], "an EA row lost its location"


def test_ea_keeps_location_department_and_worker_type_apart() -> None:
    for row in _rows("ea_searchjobs.html", EA):
        assert row["location"] != row["department"] != row["employment_type"], (
            "the EA subtitle spans share a parent, so a loose selector merges them"
        )


def test_nutanix_reads_its_own_feed_rather_than_the_sitemap() -> None:
    from stage.companies import load_companies

    board = {company.name: company for company in load_companies()}["Nutanix"].custom
    assert board is not None and board.rss, "Nutanix should read its feed, not its sitemap"
    assert board.item_tag == "job", (
        "the feed wraps rows in <job>, so a default <item> scan finds nothing"
    )


def test_a_sitemap_segment_keeps_a_subdivision_code_uppercase() -> None:
    from stage.normalize import resolve_location

    loc = "https://sandia.jobs/albuquerque-nm/intern-rd-graduate/ABC123/job/"
    row = sitemap_rows(f"<urlset><url><loc>{loc}</loc></url></urlset>")[0]

    assert row["path4_title"] == "Albuquerque NM", row["path4_title"]
    assert resolve_location(row["path4_title"]).bucket.value == "usa", (
        "the resolver needs the uppercase code, so title-casing the whole segment loses the state"
    )


def test_only_a_trailing_subdivision_code_is_uppercased() -> None:
    for loc, segment, expected in (
        ("https://x.test/job/rio-de-janeiro/analyst/1/2", "path4_title", "Rio De Janeiro"),
        ("https://x.test/job/seongnam-si/manager/1/2", "path4_title", "Seongnam Si"),
        ("https://x.test/job/livermore-ca/postdoc/1/2", "path4_title", "Livermore CA"),
    ):
        row = sitemap_rows(f"<urlset><url><loc>{loc}</loc></url></urlset>")[0]
        assert row[segment] == expected, f"{loc} -> {row[segment]!r}"
