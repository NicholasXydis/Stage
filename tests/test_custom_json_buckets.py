import asyncio
from dataclasses import replace

import httpx
import respx

from stage.domain import Company, CustomBoard, Platform
from stage.http import HostBudget, HostBudgetExceededError, HttpClient, RatePosture
from stage.services.sync import _adapter_buckets
from stage.sources import adapter_for_platform


def _row(name: str, host: str) -> Company:
    return Company(
        name=name,
        platform=Platform.CUSTOM_JSON,
        slug=name.lower(),
        custom=CustomBoard(
            url=f"https://{host}/api/jobs",
            jobs_path="data.positions",
            fields={"title": "name"},
        ),
    )


def test_a_custom_json_row_resolves_a_bucket_from_its_own_host() -> None:
    adapter = adapter_for_platform(Platform.CUSTOM_JSON)
    assert adapter is not None
    buckets = _adapter_buckets(adapter, [_row("Eaton", "eaton.eightfold.ai")])

    assert buckets == ("eaton.eightfold.ai",), "a custom_json run dies without a resolvable bucket"


def test_each_custom_json_employer_keeps_its_own_bucket() -> None:
    adapter = adapter_for_platform(Platform.CUSTOM_JSON)
    assert adapter is not None
    rows = [_row("Eaton", "eaton.eightfold.ai"), _row("Acme", "careers.acme.test")]
    buckets = _adapter_buckets(adapter, rows)

    assert buckets == ("careers.acme.test", "eaton.eightfold.ai"), (
        "unrelated custom_json vendors must not share one rate budget"
    )


def test_an_adapter_with_a_bucket_key_still_shares_one_budget() -> None:
    adapter = adapter_for_platform(Platform.WORKDAY)
    assert adapter is not None
    assert _adapter_buckets(adapter, []) == ("workday",), "workday tenants share one bucket"


def test_a_multi_vendor_source_plans_a_bound_per_bucket_not_one_lump() -> None:
    from stage.domain import Company, CustomBoard, Platform
    from stage.services.sync import _plan_bounds
    from stage.sources import adapter_for_platform

    def row(slug: str, host: str) -> Company:
        return Company(
            name=slug,
            platform=Platform.CUSTOM_JSON,
            slug=slug,
            custom=CustomBoard(url=f"https://{host}/api/jobs", fields={"title": "t"}),
        )

    adapter = adapter_for_platform(Platform.CUSTOM_JSON)
    assert adapter is not None
    rows = [row("one", "one.example.test"), row("two", "two.example.test")]
    budgets = {company.registry_key: 200 for company in rows}
    bounds = _plan_bounds(adapter, "custom_json", rows, budgets, 0)

    assert len(bounds) == 2, "vendors that share no host must not share a planned bound"
    assert {bucket for bucket, _, _, _ in bounds} == {
        "one.example.test",
        "two.example.test",
    }
    assert all(worst == 200 for _, _, _, worst in bounds), (
        "a source-wide total charged to one bucket is the wrong key, as with the 24h volume cap"
    )


def test_a_handshake_on_another_host_is_declared_as_a_bucket() -> None:
    adapter = adapter_for_platform(Platform.CUSTOM_JSON)
    assert adapter is not None
    row = Company(
        name="CN",
        platform=Platform.CUSTOM_JSON,
        slug="cn",
        custom=CustomBoard(
            url="https://us.api.csod.com/rec-job-search/external/jobs",
            handshake_url="https://cn360.csod.com/ux/ats/careersite/6/home",
            token_pattern='"token":"([A-Za-z0-9._-]+)"',
            token_header="Authorization",
            fields={"title": "displayJobTitle"},
        ),
    )
    buckets = _adapter_buckets(adapter, [row])

    assert buckets == ("cn360.csod.com", "us.api.csod.com"), (
        "the handshake host is fetched too, so it must carry its own budget"
    )


def test_a_custom_json_row_on_a_platform_host_joins_that_platforms_bucket() -> None:
    adapter = adapter_for_platform(Platform.CUSTOM_JSON)
    assert adapter is not None
    rows = [
        _row("Motorola", "motorolasolutions.wd5.myworkdayjobs.com"),
        _row("Site", "wd3.myworkdaysite.com"),
        _row("Dneg", "jobs.jobvite.com"),
        _row("Wandb", "job-boards.greenhouse.io"),
        _row("Eaton", "eaton.eightfold.ai"),
    ]

    assert _adapter_buckets(adapter, rows) == (
        "boards-api.greenhouse.io",
        "eaton.eightfold.ai",
        "jobvite",
        "workday",
    )


def test_a_platform_bucket_claimed_by_custom_json_keeps_the_platforms_pacing() -> None:
    from stage.services.sync import _bucket_postures

    host = "motorolasolutions.wd5.myworkdayjobs.com"
    workday = adapter_for_platform(Platform.WORKDAY)
    custom = adapter_for_platform(Platform.CUSTOM_JSON)
    assert workday is not None and custom is not None
    tenant = Company(
        name="Motorola",
        platform=Platform.WORKDAY,
        slug="motorola",
        workday_tenant="motorolasolutions",
        workday_site="Careers",
        workday_dc="wd5",
    )
    grouped = {"workday": (workday, [tenant]), "custom_json": (custom, [_row("Motorola", host)])}
    postures = _bucket_postures(grouped, {})

    assert host not in postures, "the tenant's host is not a second bucket"
    assert (postures["workday"].concurrency, postures["workday"].min_interval_s) == (2, 1.5)


@respx.mock
async def test_two_sources_on_one_workday_host_share_its_parallel_limit_and_ceiling() -> None:
    from stage.services.sync import _host_buckets

    host = "motorolasolutions.wd5.myworkdayjobs.com"
    custom = adapter_for_platform(Platform.CUSTOM_JSON)
    assert custom is not None
    flight = {"now": 0, "peak": 0}

    async def answer(request: httpx.Request) -> httpx.Response:
        flight["now"] += 1
        flight["peak"] = max(flight["peak"], flight["now"])
        await asyncio.sleep(0.01)
        flight["now"] -= 1
        return httpx.Response(200, json={})

    respx.get(url__startswith=f"https://{host}/").mock(side_effect=answer)
    wide = RatePosture(concurrency=2, min_interval_s=0.0, max_requests_per_run=50)
    capped = replace(wide, max_requests_per_run=7)
    budgets: dict[str, HostBudget] = {}
    hosts = frozenset({host})
    by_row = HttpClient(
        allowed_hosts=hosts,
        posture=wide,
        jitter=False,
        budgets=budgets,
        postures={"workday": wide},
        host_buckets=_host_buckets(custom, [_row("Motorola", host)]),
    )
    by_tenant = HttpClient(
        allowed_hosts=hosts,
        posture=capped,
        jitter=False,
        budgets=budgets,
        postures={"workday": capped},
        bucket_key="workday",
    )
    async with by_row, by_tenant:
        await by_row.get_json(f"https://{host}/first")
        clients = [by_row, by_tenant] * 4
        results = await asyncio.gather(
            *(client.get_json(f"https://{host}/{index}") for index, client in enumerate(clients)),
            return_exceptions=True,
        )

    assert set(budgets) == {"workday"}
    assert flight["peak"] == 2, "three in flight against a limit of two was the audit's finding"
    assert budgets["workday"].requests == 7, "the tenant source's daily allowance caps both"
    assert sum(isinstance(result, HostBudgetExceededError) for result in results) == 2
