from datetime import UTC, datetime
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
    WorkdayCrawlStep,
    rotate,
)
from stage.http import profile
from stage.sources.workday import WorkdayAdapter
from stage.storage import SourceBatch, open_repository


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


MAX_WORKDAY_RUNS_PER_CYCLE = 2


def test_the_shipped_workday_schedule_covers_every_board_within_a_bounded_cycle() -> None:
    companies = [
        company
        for company in load_companies()
        if company.enabled and company.platform is Platform.WORKDAY
    ]
    runs = -(-len(companies) // WorkdayAdapter.rotation_slice)
    assert runs <= MAX_WORKDAY_RUNS_PER_CYCLE, (
        f"{len(companies)} boards at a slice of {WorkdayAdapter.rotation_slice} needs {runs} runs"
    )

    rotation = rotate(
        [RotationMember(key=company.registry_key) for company in companies],
        budget=WorkdayAdapter.rotation_slice,
    )
    selected = set(rotation.selected)
    ceiling = profile("workday").max_requests_per_run
    budgets, details = WorkdayAdapter.crawl_budgets(
        [company for company in companies if company.registry_key in selected] or companies,
        {},
        {},
        ceiling,
    )
    assert min(budgets.values()) >= 1, "a selected board must always get at least one request"
    assert sum(budgets.values()) + details + WorkdayAdapter.retry_reserve <= ceiling


def test_the_shipped_workday_rotation_cycle_reaches_every_board() -> None:
    companies = [
        company
        for company in load_companies()
        if company.enabled and company.platform is Platform.WORKDAY
    ]
    members = [RotationMember(key=company.registry_key) for company in companies]
    seen: set[str] = set()
    cursor = ""
    for _ in range(MAX_WORKDAY_RUNS_PER_CYCLE):
        rotation = rotate(members, cursor=cursor, budget=WorkdayAdapter.rotation_slice)
        seen.update(rotation.selected)
        cursor = rotation.cursor
    assert seen == {company.registry_key for company in companies}, (
        "a board the cycle never reaches is a board whose postings quietly go stale"
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
    slice_size = min(len(companies), WorkdayAdapter.rotation_slice)
    assert started.companies == slice_size
    assert plan.planned == slice_size
    from stage.services.sync import _reserve_for

    sized = profile("workday").sized_for(len(companies), _reserve_for(WorkdayAdapter()))
    assert plan.worst_case <= sized.max_requests_per_run, (
        "the planned worst case must fit under the ceiling or the tail of the slice is skipped"
    )
    assert plan.ceiling == sized.max_requests_per_run, (
        "the reported ceiling is the derived one, so growth raises it instead of truncating"
    )


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
            priority=Priority.HIGH if index == WorkdayAdapter.rotation_slice else Priority.NORMAL,
        )
        for index in range(WorkdayAdapter.rotation_slice + 1)
    ]
    async with open_repository(db_path) as repository:
        events = [
            event async for event in sync(repository, companies, sources=("workday",), dry_run=True)
        ]

    slice_size = WorkdayAdapter.rotation_slice
    planned = {event.company for event in events if isinstance(event, PlannedRequest)}
    rotated = next(event for event in events if isinstance(event, SourceRotated))
    assert len(planned) == slice_size
    assert f"board-{slice_size:03d}" not in planned
    assert (rotated.selected, rotated.deferred) == (slice_size, 1)


async def test_a_workday_resume_never_starves_the_boards_without_a_crawl(db_path: Path) -> None:
    from stage.services.sync import sync

    complete = Company(
        name="Complete",
        platform=Platform.WORKDAY,
        slug="complete",
        workday_tenant="complete",
        workday_site="External",
        workday_dc="wd3",
    )
    unfinished = Company(
        name="Unfinished",
        platform=Platform.WORKDAY,
        slug="unfinished",
        workday_tenant="unfinished",
        workday_site="External",
        workday_dc="wd3",
    )
    now = datetime(2026, 8, 13, tzinfo=UTC)
    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="workday",
                run_started_at=now,
                workday_crawls=(
                    WorkdayCrawlStep(
                        board=WorkdayAdapter().board_key(unfinished), next_offset=20, total=100
                    ),
                ),
            )
        )
        events = [
            event
            async for event in sync(
                repository, [complete, unfinished], sources=("workday",), dry_run=True
            )
        ]

    started = next(event for event in events if isinstance(event, SourceStarted))
    planned = [event.company for event in events if isinstance(event, PlannedRequest)]
    assert started.companies == 2
    assert sorted(planned) == ["Complete", "Unfinished"], (
        "an open crawl resumes inside a normal run; it must never make the run skip other boards"
    )


def test_oracle_boards_on_a_shared_slug_remain_distinct() -> None:
    first = Company(
        name="First",
        platform=Platform.ORACLE_CLOUD,
        slug="eeho",
        oracle_host="eeho.fa.us2.oraclecloud.com",
        oracle_site="jobsearch",
    )
    second = Company(
        name="Second",
        platform=Platform.ORACLE_CLOUD,
        slug="eeho",
        oracle_host="eeho.fa.uk2.oraclecloud.com",
        oracle_site="external",
    )
    assert board_identity(first) != board_identity(second)


def test_oracle_registry_rows_need_a_safe_host_and_site(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "- name: Oracle\n"
        "  platform: oracle_cloud\n"
        "  slug: eeho\n"
        "  oracle_host: eeho.fa.us2.oraclecloud.com\n"
        "  oracle_site: jobsearch\n",
        encoding="utf-8",
    )
    company = load_companies(path)[0]
    assert (company.oracle_host, company.oracle_site) == (
        "eeho.fa.us2.oraclecloud.com",
        "jobsearch",
    )

    path.write_text(
        "- name: Oracle\n  platform: oracle_cloud\n  slug: eeho\n  oracle_host: evil.example\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="oracle_host and oracle_site"):
        load_companies(path)
