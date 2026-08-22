from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from stage.companies import load_companies
from stage.domain import Company, Platform
from stage.domain.priority import SOURCE_PRIORITY
from stage.http import HttpClient, profile
from stage.sources.base import FetchResult, PayloadValidationError
from stage.sources.jobvite import JobviteAdapter, JobviteRow

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "jobvite_resolver.html").read_text(encoding="utf-8")
ONCLICK_PAGE = (FIXTURES / "jobvite_agscareer.html").read_text(encoding="utf-8")
SHOW_MORE = (
    '<html><body><table class="jv-job-list"><tbody>'
    '<tr><td class="jv-job-list-name"><a href="/acme/job/one">Software Intern</a></td>'
    '<td class="jv-job-list-location">Montreal</td></tr>'
    '<tr><td colspan="2"><a href="/acme/search?c=IT&p=0"><strong>Show More</strong></a></td></tr>'
    "</tbody></table></body></html>"
)
EMPTY = '<html><body><table class="jv-job-list"><tbody></tbody></table></body></html>'
GONE = "<html><body><h1>Job seeker support</h1></body></html>"


def _company(slug: str = "resolver") -> Company:
    return Company(name="Resolver", platform=Platform.JOBVITE, slug=slug)


def _adapter() -> JobviteAdapter:
    return JobviteAdapter()


async def _fetch(body: str) -> FetchResult:
    adapter = _adapter()
    company = _company()
    respx.get(f"https://jobs.jobvite.com/{company.slug}/jobs").mock(
        return_value=httpx.Response(200, text=body, headers={"Content-Type": "text/html"})
    )
    async with HttpClient(
        allowed_hosts=adapter.hosts, posture=profile(adapter.rate_profile), jitter=False
    ) as client:
        return await adapter.fetch(company, client, datetime(2026, 8, 21, tzinfo=UTC))


def test_jobvite_is_ranked_with_the_other_direct_boards() -> None:
    assert "jobvite" in SOURCE_PRIORITY, "an unranked source loses every duplicate to a feed"
    assert SOURCE_PRIORITY.index("jobvite") < SOURCE_PRIORITY.index("custom_json"), (
        "a direct employer board must outrank the generic contract"
    )


def test_the_board_key_names_the_tenant_not_the_caption() -> None:
    adapter = _adapter()
    assert adapter.board_key(_company()) != adapter.board_key(_company("gigamon"))


@respx.mock
async def test_the_saved_page_yields_every_posting_with_its_own_url() -> None:
    result = await _fetch(PAGE)
    assert len(result.jobs) == 12, "the Jobvite row selector stopped matching its saved page"
    assert result.authoritative, "a clean page must stay authoritative"
    urls = {job.apply_url_raw for job in result.jobs}
    assert len(urls) == 12, "postings collapsed onto one apply url"
    for job in result.jobs:
        assert job.apply_url_raw.startswith("https://jobs.jobvite.com/resolver/job/"), (
            "a relative href was left unresolved"
        )
        assert job.title_raw, "a posting lost its title"
        assert job.company == "Resolver", "the caption must come from the registry row"
    assert any(job.location_raw for job in result.jobs), "the location cell stopped being read"


@respx.mock
async def test_a_tenant_that_links_by_onclick_still_yields_postings() -> None:
    result = await _fetch(ONCLICK_PAGE)
    assert len(result.jobs) == 15, "the onclick row layout stopped being read"
    for job in result.jobs:
        assert "/job/" in job.apply_url_raw, "an onclick row lost the href it carries"


@respx.mock
async def test_an_open_board_with_no_postings_is_not_an_error() -> None:
    result = await _fetch(EMPTY)
    assert result.jobs == (), "an empty table should yield no postings"
    assert result.authoritative, "an empty board is still a complete answer"


@respx.mock
async def test_a_show_more_row_is_navigation_and_makes_the_listing_partial() -> None:
    result = await _fetch(SHOW_MORE)
    assert len(result.jobs) == 1, "a Show More link is navigation, not a posting"
    assert not result.authoritative, "a truncated category listing must close nothing"
    assert "Show More" in result.degraded, "the truncation was not reported"


@respx.mock
async def test_a_page_without_the_listing_table_fails_loudly() -> None:
    with pytest.raises(PayloadValidationError):
        await _fetch(GONE)


def test_every_enabled_jobvite_row_uses_a_tenant_the_adapter_can_reach() -> None:
    rows = [c for c in load_companies() if c.platform is Platform.JOBVITE and c.enabled]
    assert rows, "the adapter ships with no live row and is therefore unproven"
    adapter = _adapter()
    for company in rows:
        assert adapter.url_for(company).startswith("https://jobs.jobvite.com/"), (
            f"{company.name} would leave the jobvite host"
        )


@respx.mock
async def test_an_unchanged_board_is_reported_as_unchanged_not_as_empty() -> None:
    adapter = _adapter()
    company = _company()
    respx.get(f"https://jobs.jobvite.com/{company.slug}/jobs").mock(
        return_value=httpx.Response(304)
    )
    async with HttpClient(
        allowed_hosts=adapter.hosts, posture=profile(adapter.rate_profile), jitter=False
    ) as client:
        result = await adapter.fetch(company, client, datetime(2026, 8, 21, tzinfo=UTC))

    assert result.not_modified, "a 304 board must not read as a board with no postings"
    assert result.jobs == (), "an unchanged answer carries no rows"


def test_a_posting_id_survives_a_query_string_on_the_href() -> None:
    row = JobviteRow(title="Software Intern", url="https://jobs.jobvite.com/acme/job/oX1?src=rss")
    assert row.posting_id() == "oX1", "a tracking parameter was folded into the posting id"
