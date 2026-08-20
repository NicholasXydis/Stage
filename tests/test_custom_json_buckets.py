from stage.domain import Company, CustomBoard, Platform
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
