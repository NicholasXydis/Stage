from pathlib import Path

import pytest

from stage.companies import load_companies
from stage.domain import Company, Platform
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


def test_selection_is_deterministic_whatever_order_the_rows_arrive_in() -> None:
    companies = [
        _company(Platform.GREENHOUSE, "zulu"),
        _company(Platform.GREENHOUSE, "alpha"),
    ]
    first, _ = select_probes(companies)
    second, _ = select_probes(list(reversed(companies)))
    assert [company.slug for company in first] == ["alpha"]
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


def test_every_custom_json_format_gets_its_own_probe() -> None:
    from stage.companies import load_companies

    selected, _ = select_probes(load_companies())
    formats = {
        company.custom.fmt
        for company in selected
        if company.platform is Platform.CUSTOM_JSON and company.custom is not None
    }
    shipped = {
        company.custom.fmt
        for company in load_companies()
        if company.enabled and company.platform is Platform.CUSTOM_JSON and company.custom
    }
    assert formats == shipped, f"custom_json formats with no probe: {sorted(shipped - formats)}"


def test_a_board_that_refuses_the_request_does_not_fail_the_canary() -> None:
    report = CanaryReport(
        probes=(
            BoardProbe(
                source="oracle_cloud",
                company="Alithya",
                error="ForbiddenError: careers.alithya.com returned 403",
                unreachable=True,
            ),
        )
    )
    assert report.passed, "a publisher's 403 says nothing about whether our parser still works"
    assert report.failures == ()
    assert len(report.unreachable) == 1


def test_a_parse_failure_still_fails_the_canary() -> None:
    report = CanaryReport(
        probes=(
            BoardProbe(
                source="hanzili",
                company="hanzili 2027",
                error="PayloadValidationError: no current internship rows",
            ),
        )
    )
    assert not report.passed, "drift in a payload we parse is exactly what the canary is for"
    assert len(report.failures) == 1
    assert report.unreachable == ()


def test_an_unreachable_board_is_not_counted_as_empty() -> None:
    probe = BoardProbe(source="lever", company="Acme", error="BreakerOpenError", unreachable=True)
    assert probe.is_unreachable and not probe.is_empty and not probe.is_failure


async def test_a_canary_that_runs_out_of_time_keeps_what_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from stage.domain import CompanyFinished, SyncEvent
    from stage.services import sync as sync_module
    from stage.services.canary import canary
    from stage.storage import open_repository

    async def slow(*_: object, **__: object) -> AsyncIterator[SyncEvent]:
        yield CompanyFinished("greenhouse", "Acme", 3, 1.0)
        await asyncio.sleep(30)
        yield CompanyFinished("lever", "Late", 3, 1.0)

    monkeypatch.setattr(sync_module, "sync", slow)
    async with open_repository(tmp_path / "stage.db") as repository:
        report = await canary(repository, [_company(Platform.GREENHOUSE, "acme")], timeout=0.05)

    assert report.timed_out and not report.passed
    assert [(probe.source, probe.company) for probe in report.probes] == [("greenhouse", "Acme")]


def test_a_timed_out_canary_still_prints_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from typer.testing import CliRunner

    from stage.cli.app import app
    from stage.services import canary as canary_module

    async def partial(*_: object, **__: object) -> CanaryReport:
        return CanaryReport(probes=(BoardProbe("greenhouse", "Acme", fetched=3),), timed_out=True)

    monkeypatch.setattr(canary_module, "canary", partial)
    result = CliRunner().invoke(
        app, ["canary", "--json", "--timeout", "1", "--db", str(tmp_path / "stage.db")]
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["timed_out"] is True and payload["passed"] is False
    assert payload["probes"][0]["company"] == "Acme"
    assert "Gave up after 1s with 1 board checked" in result.stderr


def test_a_probe_note_reads_plainly() -> None:
    failed = BoardProbe(source="oracle_cloud", company="A", error="ForbiddenError: x returned 403")
    degraded = BoardProbe(source="lever", company="B", degraded="2 rows skipped")
    assert failed.note == "x returned 403"
    assert degraded.note == "2 rows skipped"
    assert BoardProbe(source="lever", company="C").note == ""
