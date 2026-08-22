from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.domain import Company, CustomBoard, Platform
from stage.http import HttpClient, RatePosture, ValidatorCache
from stage.sources.base import FetchResult, PayloadValidationError
from stage.sources.custom_json import CustomJsonAdapter

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
HOST = "jobs.example.test"
URL = f"https://{HOST}/openings"

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Software Engineer Intern</title><link>https://jobs.example.test/1</link>
<g:id>R-1</g:id><g:location>Montreal, QC</g:location></item>
<item><title>Data Intern</title><link>https://jobs.example.test/2</link>
<g:id>R-2</g:id><g:location>Toronto, ON</g:location></item>
</channel></rss>"""

JSONLD = """<html><head>
<script type="application/ld+json">{"@type":"Organization","name":"Acme"}</script>
<script type="application/ld+json">{"@type":"JobPosting","title":"Backend Intern",
"identifier":"R-9","url":"https://jobs.example.test/9"}</script>
</head><body></body></html>"""

SITEMAP = """<?xml version="1.0"?><urlset>
<url><loc>https://jobs.example.test/job/backend-intern-R-1</loc></url>
<url><loc>https://jobs.example.test/about-us</loc></url>
</urlset>"""

HTML = """<html><body><ul class="board">
<li class="row"><a href="/job/R-1">Software Intern</a><span class="where">Montreal, QC</span></li>
<li class="row"><a href="/job/R-2">Hardware Intern</a><span class="where">Ottawa, ON</span></li>
</ul></body></html>"""

EMBEDDED = """<html><body><script>
window.__DATA__ = {"positions": [{"id": "R-3", "title": "ML Intern", "city": "Laval, QC"}]};
</script></body></html>"""


def _company(board: CustomBoard) -> Company:
    return Company(name="Acme", platform=Platform.CUSTOM_JSON, slug="acme", custom=board)


def _client() -> HttpClient:
    return HttpClient(
        allowed_hosts=frozenset({HOST}),
        posture=RatePosture(min_interval_s=0.0),
        cache=ValidatorCache(),
    )


RSS_BOARD = CustomBoard(
    url=URL,
    fmt="rss",
    fields={"title": "title", "id": "g:id", "url": "link", "location": "g:location"},
)
JSONLD_BOARD = CustomBoard(
    url=URL, fmt="jsonld", fields={"title": "title", "id": "identifier", "url": "url"}
)
SITEMAP_BOARD = CustomBoard(
    url=URL, fmt="sitemap", fields={"title": "title", "id": "id", "url": "loc"}
)
HTML_BOARD = CustomBoard(
    url=URL,
    fmt="html",
    row_selector="li.row",
    fields={
        "title": "a",
        "id": "a::slugid(href)",
        "url": "a::attr(href)",
        "location": "span.where",
    },
)
EMBEDDED_BOARD = CustomBoard(
    url=URL,
    extract="window.__DATA__",
    jobs_path="positions",
    fields={"title": "title", "id": "id", "location": "city"},
    url_template="https://jobs.example.test/job/{id}",
)


async def _fetch(board: CustomBoard, body: str, *, content_type: str = "text/html") -> FetchResult:
    respx.get(URL).mock(
        return_value=httpx.Response(200, text=body, headers={"Content-Type": content_type})
    )
    async with _client() as client:
        return await CustomJsonAdapter().fetch(_company(board), client, NOW)


@pytest.mark.asyncio
@respx.mock
async def test_an_rss_board_maps_its_items_to_jobs() -> None:
    result = await _fetch(RSS_BOARD, RSS, content_type="application/rss+xml")
    assert [job.title_raw for job in result.jobs] == ["Software Engineer Intern", "Data Intern"]
    assert result.jobs[0].location_raw == "Montreal, QC", "the g:location field stopped mapping"
    assert result.authoritative, "a clean feed must stay authoritative"


@pytest.mark.asyncio
@respx.mock
async def test_a_jsonld_board_keeps_only_the_jobposting_block() -> None:
    result = await _fetch(JSONLD_BOARD, JSONLD)
    assert [job.title_raw for job in result.jobs] == ["Backend Intern"], (
        "the Organization block must not win over the JobPosting block"
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_sitemap_board_reads_its_url_entries() -> None:
    result = await _fetch(SITEMAP_BOARD, SITEMAP, content_type="application/xml")
    assert len(result.jobs) == 2, "the sitemap rows stopped being read"


@pytest.mark.asyncio
@respx.mock
async def test_a_sitemap_row_filter_keeps_only_the_matching_entries() -> None:
    board = CustomBoard(
        url=SITEMAP_BOARD.url,
        fmt="sitemap",
        row_filter=r"/job/",
        fields=dict(SITEMAP_BOARD.fields),
    )
    result = await _fetch(board, SITEMAP, content_type="application/xml")
    assert len(result.jobs) == 1, "row_filter stopped narrowing the sitemap to postings"
    assert "about-us" not in result.jobs[0].apply_url_raw, "a content page survived the filter"


@pytest.mark.asyncio
@respx.mock
async def test_an_html_board_resolves_relative_hrefs() -> None:
    result = await _fetch(HTML_BOARD, HTML)
    assert [job.title_raw for job in result.jobs] == ["Software Intern", "Hardware Intern"]
    for job in result.jobs:
        assert job.apply_url_raw.startswith(f"https://{HOST}/job/"), (
            "a relative href stayed relative"
        )


@pytest.mark.asyncio
@respx.mock
async def test_an_embedded_payload_is_extracted_from_the_page() -> None:
    result = await _fetch(EMBEDDED_BOARD, EMBEDDED)
    assert [job.title_raw for job in result.jobs] == ["ML Intern"]
    assert result.jobs[0].apply_url_raw == "https://jobs.example.test/job/R-3", (
        "the url template stopped filling from the row id"
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_with_no_items_raises_rather_than_reading_as_empty() -> None:
    with pytest.raises(PayloadValidationError, match="no <item>"):
        await _fetch(RSS_BOARD, "<rss><channel></channel></rss>")


@pytest.mark.asyncio
@respx.mock
async def test_a_page_with_no_jobposting_block_raises() -> None:
    with pytest.raises(PayloadValidationError, match="JobPosting"):
        await _fetch(JSONLD_BOARD, "<html><head></head><body>no data</body></html>")


@pytest.mark.asyncio
@respx.mock
async def test_a_sitemap_with_no_url_entries_raises() -> None:
    with pytest.raises(PayloadValidationError, match="no <url>"):
        await _fetch(SITEMAP_BOARD, "<urlset></urlset>")


@pytest.mark.asyncio
@respx.mock
async def test_a_selector_that_matches_nothing_raises_rather_than_reading_as_empty() -> None:
    with pytest.raises(PayloadValidationError, match="matched no"):
        await _fetch(HTML_BOARD, "<html><body><p>rebuilt page</p></body></html>")


@pytest.mark.asyncio
@respx.mock
async def test_a_page_without_the_embedded_object_raises() -> None:
    with pytest.raises(PayloadValidationError, match="object in the page"):
        await _fetch(EMBEDDED_BOARD, "<html><body><script>var other = 1;</script></body></html>")


@pytest.mark.asyncio
@respx.mock
async def test_a_later_page_answering_304_keeps_its_rows_but_loses_authority() -> None:
    board = CustomBoard(
        url=URL,
        fmt="html",
        row_selector="li.row",
        fields=dict(HTML_BOARD.fields),
        page_param="page",
        page_size=2,
        page_step=1,
    )
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, text=HTML, headers={"Content-Type": "text/html"}),
        httpx.Response(304),
    ]
    async with _client() as client:
        result = await CustomJsonAdapter().fetch(_company(board), client, NOW)

    assert len(result.jobs) == 2, "the rows read before the stale page were dropped"
    assert not result.authoritative, "a walk that ended on a 304 cannot close postings"
    assert "304" in result.degraded, "the stale page was not reported"
