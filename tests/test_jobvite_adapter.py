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
NO_OPEN_JOBS = (FIXTURES / "jobvite_capcomusa_empty.html").read_text(encoding="utf-8")
OUTAGE = (
    "<html><body><h1>Jobvite Page Unavailable</h1><p>Job listings are currently unavailable "
    "but they will return shortly</p></body></html>"
)
NOTICE_ELSEWHERE = "<html><body><div>There are currently no open jobs.</div></body></html>"
PLAIN_NOTICE = "<html><body><p>There are currently no open jobs.</p></body></html>"
OTHER_NOTICE = '<html><body><p class="jv-text-center">Search returned 4 jobs.</p></body></html>'
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


def _search_page(*names: str, shown: str = "") -> str:
    rows = "".join(
        f'<tr><td class="jv-job-list-name"><a href="/acme/job/{name}">{name}</a></td>'
        '<td class="jv-job-list-location">Montreal</td></tr>'
        for name in names
    )
    pages = f'<div class="jv-pagination-text"> {shown} </div>' if shown else ""
    table = f'<table class="jv-job-list"><tbody>{rows}</tbody></table>'
    return f"<html><body>{table}{pages}</body></html>"


def _show_more(response: httpx.Response, page: int = 0) -> respx.Route:
    return respx.get(f"https://jobs.jobvite.com/acme/search?c=IT&p={page}").mock(
        return_value=response
    )


@respx.mock
async def test_a_show_more_page_is_read_and_the_listing_stays_whole() -> None:
    route = _show_more(httpx.Response(200, text=_search_page("one", "two", shown="1-2 of 2")))
    result = await _fetch(SHOW_MORE)
    assert route.called
    assert sorted(job.title_raw for job in result.jobs) == ["Software Intern", "two"]
    assert result.authoritative
    assert not result.degraded


@respx.mock
async def test_a_failed_show_more_page_keeps_the_listing_partial() -> None:
    _show_more(httpx.Response(404))
    result = await _fetch(SHOW_MORE)
    assert len(result.jobs) == 1, "a Show More link is navigation, not a posting"
    assert not result.authoritative, "a truncated category listing must close nothing"
    assert "Show More" in result.degraded, "the truncation was not reported"


@respx.mock
async def test_a_long_show_more_category_is_read_page_by_page() -> None:
    _show_more(httpx.Response(200, text=_search_page("two", shown="1-1 of 2")))
    _show_more(httpx.Response(200, text=_search_page("three", shown="2-2 of 2")), page=1)
    result = await _fetch(SHOW_MORE)
    assert sorted(job.title_raw for job in result.jobs) == ["Software Intern", "three", "two"]
    assert result.authoritative


@respx.mock
async def test_a_show_more_page_that_never_ends_stops_at_the_budget() -> None:
    from stage.sources.jobvite import MAX_SHOW_MORE

    route = respx.get(url__startswith="https://jobs.jobvite.com/acme/search").mock(
        return_value=httpx.Response(200, text=_search_page("two", shown="1-1 of 2"))
    )
    result = await _fetch(SHOW_MORE)
    assert route.call_count == MAX_SHOW_MORE
    assert len(result.jobs) == 2
    assert not result.authoritative


@respx.mock
async def test_a_page_without_the_listing_table_fails_loudly() -> None:
    with pytest.raises(PayloadValidationError):
        await _fetch(GONE)


@respx.mock
@pytest.mark.parametrize(
    "page",
    [
        NO_OPEN_JOBS,
        '<html><body><p class="jv-text-center">Thanking you for visiting our Jobs page. We '
        "currently do not have any positions available.</p></body></html>",
    ],
)
async def test_a_board_saying_it_has_no_open_jobs_is_empty_not_broken(page: str) -> None:
    result = await _fetch(page)
    assert result.jobs == ()
    assert result.declared_empty
    assert not result.authoritative
    assert "no open jobs" in result.degraded


@respx.mock
@pytest.mark.parametrize("page", [OUTAGE, NOTICE_ELSEWHERE, PLAIN_NOTICE, OTHER_NOTICE])
async def test_only_jobvites_own_empty_notice_counts_as_empty(page: str) -> None:
    with pytest.raises(PayloadValidationError):
        await _fetch(page)


@respx.mock
async def test_a_listing_wins_over_an_empty_notice_on_the_same_page() -> None:
    page = PAGE.replace(
        "<body>", '<body><p class="jv-text-center">There are currently no open jobs.</p>', 1
    )
    result = await _fetch(page)
    assert len(result.jobs) == 12
    assert not result.declared_empty


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
