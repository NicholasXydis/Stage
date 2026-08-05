from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.domain import Company, Platform
from stage.http import HttpClient, RatePosture
from stage.sources import get_adapter, get_adapters
from stage.sources.base import PayloadValidationError

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
UNPACED = RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=50)

CASES = {
    "ashby": (
        Platform.ASHBY,
        "https://api.ashbyhq.com/posting-api/job-board/acme",
        {
            "jobs": [
                {
                    "id": "a1",
                    "title": "Software Engineer Intern",
                    "location": "Montréal, QC",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
                    "descriptionPlain": "Build things.",
                },
                {"title": "no id"},
            ]
        },
    ),
    "workable": (
        Platform.WORKABLE,
        "https://apply.workable.com/api/v1/widget/accounts/acme?details=true",
        {
            "name": "Acme",
            "jobs": [
                {
                    "shortcode": "AB12",
                    "title": "Stagiaire en génie logiciel",
                    "city": "Montréal",
                    "country": "Canada",
                    "url": "https://apply.workable.com/acme/j/AB12",
                    "description": "<p>Build things.</p>",
                },
                {"title": "no shortcode"},
            ],
        },
    ),
    "recruitee": (
        Platform.RECRUITEE,
        "https://acme.recruitee.com/api/offers/",
        {
            "offers": [
                {
                    "id": 77,
                    "title": "Data Science Intern",
                    "location": "Montreal, QC",
                    "careers_url": "https://acme.recruitee.com/o/data-science-intern",
                    "description": "<p>Build things.</p>",
                },
                {"title": "no id"},
            ]
        },
    ),
    "breezy": (
        Platform.BREEZY,
        "https://acme.breezy.hr/json",
        [
            {
                "id": "b1",
                "name": "Security Intern",
                "location": {"name": "Montreal, Quebec"},
                "url": "https://acme.breezy.hr/p/b1",
                "description": "<p>Build things.</p>",
            },
            {"name": "no id"},
        ],
    ),
    "bamboohr": (
        Platform.BAMBOOHR,
        "https://acme.bamboohr.com/careers/list",
        {
            "result": [
                {
                    "id": 5,
                    "jobOpeningName": "Intern, Software",
                    "location": {"city": "Montreal", "state": "QC", "country": "Canada"},
                },
                {"jobOpeningName": "no id"},
            ]
        },
    ),
}


def _company(platform: Platform) -> Company:
    return Company(name="Acme", platform=platform, slug="acme")


def _client(name: str) -> HttpClient:
    adapter = get_adapter(name)
    return HttpClient(
        allowed_hosts=adapter.hosts_for([_company(CASES[name][0])]),
        posture=UNPACED,
        jitter=False,
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_adapter_self_registers(name: str) -> None:
    assert name in get_adapters()
    assert get_adapter(name).platform is CASES[name][0]


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_board_key_is_the_prefix_of_the_ids_it_mints(name: str) -> None:
    adapter = get_adapter(name)
    company = _company(CASES[name][0])
    assert adapter.board_key(company) == f"{name}:acme"


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_slug_that_could_rewrite_the_host_is_refused(name: str) -> None:
    from stage.sources.platforms import SlugRejectedError

    adapter = get_adapter(name)
    hostile = Company(name="Evil", platform=CASES[name][0], slug="evil.com/../x")
    with pytest.raises(SlugRejectedError):
        adapter.plan(hostile)


@pytest.mark.parametrize("name", sorted(CASES))
@respx.mock
async def test_one_bad_row_is_dropped_and_costs_the_board_its_authority(name: str) -> None:
    platform, url, payload = CASES[name]
    respx.get(url.split("?")[0]).mock(return_value=httpx.Response(200, json=payload))

    async with _client(name) as client:
        result = await get_adapter(name).fetch(_company(platform), client, NOW)

    assert len(result.jobs) == 1, "the good row survives its malformed neighbour"
    assert "1 posting(s) failed validation" in result.degraded
    assert not result.authoritative, "an incomplete listing must close nothing"

    job = result.jobs[0]
    assert job.source == name
    assert job.company == "Acme"
    assert job.id.startswith(f"{name}:acme:")
    assert "Montr" in job.location_raw or "Montreal" in job.location_raw
    assert job.title_raw


@pytest.mark.parametrize("name", sorted(CASES))
@respx.mock
async def test_a_wrong_shape_still_raises(name: str) -> None:
    platform, url, _ = CASES[name]
    wrong = {"unexpected": True} if name != "breezy" else {"not": "a list"}
    respx.get(url.split("?")[0]).mock(return_value=httpx.Response(200, json=wrong))

    async with _client(name) as client:
        with pytest.raises(PayloadValidationError):
            await get_adapter(name).fetch(_company(platform), client, NOW)


@respx.mock
async def test_ashby_hides_an_unlisted_posting() -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": "a1", "title": "Live", "isListed": True},
                    {"id": "a2", "title": "Draft", "isListed": False},
                ]
            },
        )
    )
    async with _client("ashby") as client:
        result = await get_adapter("ashby").fetch(_company(Platform.ASHBY), client, NOW)

    assert [job.title_raw for job in result.jobs] == ["Live"]
    assert result.authoritative, "an unlisted posting is not a dropped row"


def test_every_adapter_declares_a_budget_matching_whether_it_carries_bodies() -> None:
    carries_bodies = {"ashby", "workable", "recruitee", "breezy"}
    for name in CASES:
        adapter = get_adapter(name)
        if name in carries_bodies:
            assert adapter.detail_budget == 0, (
                f"{name} returns descriptions inline, so a detail fetch would re-request them"
            )
        else:
            assert adapter.detail_budget == 0, (
                "bamboohr has no recorded detail fixture yet"
            )


def test_an_adapter_whose_host_embeds_the_slug_shares_one_bucket() -> None:
    from stage.sources import get_adapters

    for adapter in get_adapters().values():
        hosts = adapter.hosts_for(
            [
                Company(name="A", platform=adapter.platform, slug="acme"),
                Company(name="B", platform=adapter.platform, slug="globex"),
            ]
        )
        if len(hosts) < 2:
            continue
        assert adapter.bucket_key, (
            f"{adapter.name} builds a hostname per company, so it needs a shared bucket_key"
        )


def test_the_shared_bucket_is_one_budget_across_companies() -> None:
    from stage.http import HttpClient
    from stage.sources import get_adapter

    adapter = get_adapter("bamboohr")
    companies = [
        Company(name="A", platform=Platform.BAMBOOHR, slug="acme"),
        Company(name="B", platform=Platform.BAMBOOHR, slug="globex"),
    ]
    client = HttpClient(
        allowed_hosts=adapter.hosts_for(companies),
        posture=UNPACED,
        bucket_key=adapter.bucket_key,
    )
    first = client._budget_for(client.bucket_for("acme.bamboohr.com"))
    second = client._budget_for(client.bucket_for("globex.bamboohr.com"))
    assert first is second
    assert first.semaphore is second.semaphore


NULLED = {
    "ashby": (
        Platform.ASHBY,
        "https://api.ashbyhq.com/posting-api/job-board/acme",
        {
            "jobs": [
                {
                    "id": "a1",
                    "title": "Intern",
                    "location": "Montreal",
                    "isRemote": None,
                    "department": None,
                    "employmentType": None,
                    "descriptionPlain": None,
                    "jobUrl": None,
                }
            ]
        },
    ),
    "workable": (
        Platform.WORKABLE,
        "https://apply.workable.com/api/v1/widget/accounts/acme",
        {
            "jobs": [
                {
                    "shortcode": "AB12",
                    "title": "Intern",
                    "city": "Montreal",
                    "department": None,
                    "telecommuting": None,
                    "description": None,
                }
            ]
        },
    ),
    "bamboohr": (
        Platform.BAMBOOHR,
        "https://acme.bamboohr.com/careers/list",
        {
            "result": [
                {
                    "id": 5,
                    "jobOpeningName": "Intern",
                    "location": {"city": "Montreal", "state": None, "country": None},
                    "atsLocation": {"country": None, "state": None, "city": None},
                    "isRemote": None,
                    "departmentLabel": None,
                }
            ]
        },
    ),
    "recruitee": (
        Platform.RECRUITEE,
        "https://acme.recruitee.com/api/offers/",
        {
            "offers": [
                {
                    "id": 1,
                    "title": "Intern",
                    "city": "Montreal",
                    "description": None,
                    "department": None,
                    "published_at": "2021-11-09 22:03:23 UTC",
                }
            ]
        },
    ),
    "breezy": (
        Platform.BREEZY,
        "https://acme.breezy.hr/json",
        [
            {
                "id": "b1",
                "name": "Intern",
                "location": {"name": "Montreal", "city": None},
                "description": None,
                "department": None,
            }
        ],
    ),
}


@pytest.mark.parametrize("name", sorted(NULLED))
@respx.mock
async def test_an_explicit_null_on_an_optional_field_is_absence_not_drift(name: str) -> None:
    platform, url, payload = NULLED[name]
    respx.get(url).mock(return_value=httpx.Response(200, json=payload))

    async with _client(name) as client:
        result = await get_adapter(name).fetch(_company(platform), client, NOW)

    assert len(result.jobs) == 1, (
        "a real board sends null for optional fields; a plain default drops the whole row"
    )
    assert result.authoritative
    assert result.jobs[0].title_raw == "Intern"


@respx.mock
async def test_a_non_iso_timestamp_does_not_drop_a_recruitee_row() -> None:
    respx.get("https://acme.recruitee.com/api/offers/").mock(
        return_value=httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": 1,
                        "title": "Intern",
                        "published_at": "2021-11-09 22:03:23 UTC",
                    }
                ]
            },
        )
    )
    async with _client("recruitee") as client:
        result = await get_adapter("recruitee").fetch(
            _company(Platform.RECRUITEE), client, NOW
        )

    assert len(result.jobs) == 1
    posted = result.jobs[0].source_posted_at
    assert posted is not None and posted.year == 2021, (
        "an unparseable date must cost the field, never the posting"
    )
