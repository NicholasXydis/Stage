from pathlib import Path

import pytest

from stage.companies import RegistryError, board_identity, load_companies
from stage.domain import (
    BucketPlan,
    Company,
    PlannedRequest,
    Platform,
    Priority,
    RotationMember,
    SourceRotated,
    SourceStarted,
    rotate,
)
from stage.http import profile
from stage.sources.workday import WorkdayAdapter
from stage.storage import open_repository


def test_sibling_workday_sites_on_one_tenant_are_distinct_boards() -> None:
    external = Company(
        name="RBC",
        platform=Platform.WORKDAY,
        slug="rbc",
        workday_tenant="rbc",
        workday_site="External",
        workday_dc="wd3",
    )
    capital_markets = Company(
        name="RBC Capital Markets",
        platform=Platform.WORKDAY,
        slug="rbc",
        workday_tenant="rbc",
        workday_site="CapitalMarkets",
        workday_dc="wd3",
    )
    assert board_identity(external) != board_identity(capital_markets)


def test_the_registry_accepts_sibling_boards_for_one_employer(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "- name: RBC\n"
        "  platform: workday\n"
        "  slug: rbc\n"
        "  workday_tenant: rbc\n"
        "  workday_site: External\n"
        "  workday_dc: wd3\n"
        "- name: RBC Capital Markets\n"
        "  platform: workday\n"
        "  slug: rbc\n"
        "  workday_tenant: rbc\n"
        "  workday_site: CapitalMarkets\n"
        "  workday_dc: wd3\n",
        encoding="utf-8",
    )
    assert len(load_companies(path)) == 2


def test_the_same_board_twice_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "- name: RBC\n  platform: workday\n  slug: rbc\n  workday_site: External\n"
        "- name: RBC Again\n  platform: workday\n  slug: rbc\n  workday_site: External\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="duplicate board"):
        load_companies(path)


def test_the_shipped_registry_loads_and_every_enabled_row_is_verified() -> None:
    companies = load_companies()
    assert len(companies) > 70
    for company in companies:
        if company.enabled:
            assert company.last_verified is not None, company.name


def test_the_shipped_workday_schedule_fits_its_shared_request_ceiling() -> None:
    companies = [
        company
        for company in load_companies()
        if company.enabled and company.platform is Platform.WORKDAY
    ]
    rotation = rotate(
        [RotationMember(key=company.registry_key) for company in companies],
        budget=WorkdayAdapter.rotation_slice,
    )

    assert set(rotation.selected) == {company.registry_key for company in companies}
    assert not rotation.deferred
    pages, details = WorkdayAdapter.crawl_budget(
        len(companies), profile("workday").max_requests_per_run
    )
    assert len(companies) * pages + details + WorkdayAdapter.retry_reserve <= (
        profile("workday").max_requests_per_run
    )


async def test_a_workday_dry_run_touches_every_enabled_board_within_budget(db_path: Path) -> None:
    from stage.services.sync import sync

    companies = [
        company
        for company in load_companies()
        if company.enabled and company.platform is Platform.WORKDAY
    ]
    async with open_repository(db_path) as repository:
        events = [
            event async for event in sync(repository, companies, sources=("workday",), dry_run=True)
        ]

    started = next(event for event in events if isinstance(event, SourceStarted))
    plan = next(event for event in events if isinstance(event, BucketPlan))
    assert started.companies == len(companies)
    assert plan.planned == len(companies)
    assert plan.worst_case == 100
    assert not [event for event in events if isinstance(event, SourceRotated)]


async def test_a_workday_priority_cannot_bypass_fair_rotation(db_path: Path) -> None:
    from stage.services.sync import sync

    companies = [
        Company(
            name=f"board-{index:03d}",
            platform=Platform.WORKDAY,
            slug=f"board-{index:03d}",
            workday_tenant=f"tenant-{index:03d}",
            workday_site="External",
            workday_dc="wd3",
            priority=Priority.HIGH if index == 100 else Priority.NORMAL,
        )
        for index in range(101)
    ]
    async with open_repository(db_path) as repository:
        events = [
            event async for event in sync(repository, companies, sources=("workday",), dry_run=True)
        ]

    planned = {event.company for event in events if isinstance(event, PlannedRequest)}
    rotated = next(event for event in events if isinstance(event, SourceRotated))
    assert len(planned) == 100
    assert "board-100" not in planned
    assert (rotated.selected, rotated.deferred) == (100, 1)
