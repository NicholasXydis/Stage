from pathlib import Path

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
        assert "icims.com" in row["url"] or "dayforce" in row["url"], row["url"]
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
