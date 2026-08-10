from stage.companies import load_companies
from stage.domain import Company, Platform, Priority
from stage.services.canary import AKAMAI_PLATFORMS, BoardProbe, CanaryReport, select_probes


def _company(platform: Platform, slug: str, **kwargs: object) -> Company:
    return Company(name=slug.title(), platform=platform, slug=slug, **kwargs)  # type: ignore[arg-type]


def test_one_board_is_probed_per_platform() -> None:
    companies = [
        _company(Platform.GREENHOUSE, "acme"),
        _company(Platform.GREENHOUSE, "beta"),
        _company(Platform.LEVER, "gamma"),
    ]
    selected, _ = select_probes(companies)
    assert [company.platform for company in selected] == [Platform.GREENHOUSE, Platform.LEVER]
    assert len(selected) == 2, "one board per platform is the whole sample"


def test_workday_is_never_probed_on_a_schedule() -> None:
    companies = [
        _company(Platform.WORKDAY, "cae", workday_tenant="cae", workday_site="career"),
        _company(Platform.GREENHOUSE, "acme"),
    ]
    selected, skipped = select_probes(companies)
    assert [company.platform for company in selected] == [Platform.GREENHOUSE]
    assert skipped == ("workday",), "Workday is behind Akamai and is never scheduled"
    assert Platform.WORKDAY in AKAMAI_PLATFORMS


def test_a_disabled_row_is_never_probed() -> None:
    companies = [
        _company(Platform.GREENHOUSE, "off", enabled=False),
        _company(Platform.GREENHOUSE, "on"),
    ]
    selected, _ = select_probes(companies)
    assert [company.slug for company in selected] == ["on"]


def test_selection_is_deterministic_and_prefers_a_high_priority_row() -> None:
    companies = [
        _company(Platform.GREENHOUSE, "zulu", priority=Priority.HIGH),
        _company(Platform.GREENHOUSE, "alpha"),
    ]
    first, _ = select_probes(companies)
    second, _ = select_probes(list(reversed(companies)))
    assert [company.slug for company in first] == ["zulu"]
    assert first == second, "selection must not vary between runs"


def test_the_shipped_registry_yields_a_probe_for_every_built_platform() -> None:
    selected, skipped = select_probes(load_companies())
    platforms = {company.platform.value for company in selected}
    assert "workday" not in platforms
    assert platforms >= {"greenhouse", "lever", "smartrecruiters"}, (
        f"the three operational adapters must each get a probe; got {sorted(platforms)}"
    )
    assert skipped == ("workday",)


def test_a_board_answering_with_no_postings_fails_the_canary() -> None:
    report = CanaryReport(probes=(BoardProbe(source="greenhouse", company="Acme", fetched=0),))
    assert report.empties, "a 200 with zero jobs is not evidence the board exists"
    assert not report.passed


def test_an_unchanged_board_is_not_an_empty_board() -> None:
    report = CanaryReport(probes=(BoardProbe(source="greenhouse", company="Acme", unchanged=True),))
    assert not report.empties, "a 304 says the board did not change, not that it is empty"
    assert report.passed


def test_a_failed_probe_fails_the_canary() -> None:
    report = CanaryReport(
        probes=(BoardProbe(source="lever", company="Acme", error="PayloadValidationError"),)
    )
    assert [probe.company for probe in report.failures] == ["Acme"]
    assert not report.passed
