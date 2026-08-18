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
