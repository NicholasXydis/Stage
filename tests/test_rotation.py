
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stage.domain import (
    Company,
    CompanyFinished,
    PlannedRequest,
    Platform,
    Priority,
    RateState,
    RotationMember,
    SourceRotated,
    rotate,
)
from stage.http import HttpClient, profile
from stage.storage import SourceBatch, open_repository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _at(when: datetime) -> Callable[[], datetime]:
    return lambda: when


def _members(count: int, *, always: set[str] | None = None) -> list[RotationMember]:
    high = always or set()
    return [
        RotationMember(key=f"tenant-{index:02d}", always=f"tenant-{index:02d}" in high)
        for index in range(count)
    ]


def test_a_zero_slice_means_no_rotation_at_all() -> None:
    result = rotate(_members(5), budget=0)
    assert len(result.selected) == 5
    assert result.deferred == ()
    assert not result.rotating


def test_a_slice_covers_part_of_the_ring_and_names_where_the_next_run_resumes() -> None:
    result = rotate(_members(10), cursor="", budget=4)
    assert result.selected == ("tenant-00", "tenant-01", "tenant-02", "tenant-03")
    assert len(result.deferred) == 6
    assert result.cursor == "tenant-03"
    assert not result.wrapped


def test_consecutive_runs_walk_the_whole_ring_without_repeating_or_skipping() -> None:
    members = _members(82)
    cursor = ""
    visited: list[str] = []
    runs = 0
    while runs < 10:
        runs += 1
        result = rotate(members, cursor=cursor, budget=40)
        visited.extend(result.selected)
        cursor = result.cursor
        if len(set(visited)) == len(members):
            break

    assert set(visited) == {member.key for member in members}
    assert runs <= 3, "82 tenants at 40 a run must close the cycle in three runs"


def test_high_priority_members_are_covered_every_run_and_never_rotate() -> None:
    members = _members(10, always={"tenant-00", "tenant-09"})
    seen_always = []
    cursor = ""
    for _ in range(3):
        result = rotate(members, cursor=cursor, budget=4)
        assert "tenant-00" in result.selected and "tenant-09" in result.selected
        assert "tenant-00" not in result.deferred and "tenant-09" not in result.deferred
        seen_always.append(len(result.selected))
        cursor = result.cursor

    assert seen_always == [4, 4, 4], "the always-on members count against the same budget"


def test_a_budget_that_covers_everything_reports_a_completed_cycle() -> None:
    result = rotate(_members(3), budget=10)
    assert result.deferred == ()
    assert result.wrapped
    assert result.cursor == "", "a full run leaves the next one starting from the top"


def test_the_ring_order_is_stable_and_never_derived_from_arrival() -> None:
    forwards = rotate(_members(10), budget=3)
    backwards = rotate(list(reversed(_members(10))), budget=3)
    assert forwards.selected == backwards.selected
    assert forwards.cursor == backwards.cursor


def test_a_cursor_naming_a_departed_member_resumes_after_it_rather_than_restarting() -> None:
    members = [member for member in _members(10) if member.key != "tenant-03"]
    result = rotate(members, cursor="tenant-03", budget=3)
    assert result.selected == ("tenant-04", "tenant-05", "tenant-06")


def test_a_cursor_past_the_end_wraps_to_the_start() -> None:
    result = rotate(_members(5), cursor="tenant-99", budget=2)
    assert result.selected == ("tenant-00", "tenant-01")
    assert result.wrapped


class _CountingAdapter:
    name = "greenhouse"
    platform = Platform.GREENHOUSE
    rate_profile = "standard"
    hosts = frozenset({"boards-api.greenhouse.io"})
    bucket_key = ""
    rotation_slice = 2

    max_requests_per_company = 1
    detail_budget = 0

    def hosts_for(self, companies: object) -> frozenset[str]:
        return self.hosts

    def board_key(self, company: Company) -> str:
        return f"{self.name}:{company.slug}"

    def plan(self, company: Company) -> tuple[str, ...]:
        return (f"https://boards-api.greenhouse.io/v1/boards/{company.slug}/jobs?content=true",)

    async def fetch(
        self,
        company: Company,
        client: object,
        now: datetime,
        facets: object = None,
        details: object = (),
    ) -> object:
        from stage.sources.base import FetchResult

        assert isinstance(client, object)
        await client.get_json(self.plan(company)[0])  # type: ignore[attr-defined]
        return FetchResult(jobs=())


def _registry(count: int) -> list[Company]:
    return [
        Company(name=f"tenant-{index:02d}", platform=Platform.GREENHOUSE, slug=f"t{index}")
        for index in range(count)
    ]


@respx.mock
async def test_the_cursor_survives_the_process_and_the_next_run_covers_the_rest(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    companies = _registry(5)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async def run(when: datetime) -> tuple[list[str], str]:
        async with open_repository(db_path) as repository:
            fetched: list[str] = []
            async for event in sync_module.sync(repository, companies, now_fn=_at(when)):
                if isinstance(event, CompanyFinished):
                    fetched.append(event.company)
            stored = await repository.load_rate_state()
        return fetched, stored["boards-api.greenhouse.io"].rotation_cursor

    first, cursor_one = await run(NOW)
    second, cursor_two = await run(NOW + timedelta(hours=1))
    third, _ = await run(NOW + timedelta(hours=2))

    assert sorted(first) == ["tenant-00", "tenant-01"]
    assert cursor_one == "greenhouse:t1", "the cursor names a registry row, not a display name"
    assert sorted(second) == ["tenant-02", "tenant-03"]
    assert cursor_two == "greenhouse:t3"
    assert set(first + second + third) == {company.name for company in companies}
    assert "tenant-04" in third and "tenant-00" in third, "the ring wraps rather than stalling"


@respx.mock
async def test_a_high_priority_registry_row_is_fetched_on_every_run(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    companies = [
        replace(company, priority=Priority.HIGH) if company.name == "tenant-04" else company
        for company in _registry(5)
    ]

    seen: list[set[str]] = []
    for offset in range(3):
        when = NOW + timedelta(hours=offset)
        async with open_repository(db_path) as repository:
            fetched: set[str] = set()
            async for event in sync_module.sync(repository, companies, now_fn=_at(when)):
                if isinstance(event, CompanyFinished):
                    fetched.add(event.company)
            seen.append(fetched)

    assert all("tenant-04" in run for run in seen)
    assert all(len(run) == 2 for run in seen), "the always-on row counts against the budget"


@respx.mock
async def test_a_deferred_board_is_announced_rather_than_silently_absent(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async with open_repository(db_path) as repository:
        events = [
            event
            async for event in sync_module.sync(repository, _registry(5), now_fn=lambda: NOW)
        ]

    announced = [event for event in events if isinstance(event, SourceRotated)]
    assert len(announced) == 1
    assert announced[0].selected == 2
    assert announced[0].deferred == 3
    assert announced[0].cursor == "greenhouse:t1"
    assert not announced[0].wrapped


class _SelectivelyBrokenAdapter(_CountingAdapter):
    broken = "tenant-01"

    async def fetch(
        self,
        company: Company,
        client: object,
        now: datetime,
        facets: object = None,
        details: object = (),
    ) -> object:
        if company.name == self.broken:
            raise RuntimeError("tenant returns 500")
        return await super().fetch(company, client, now)


@respx.mock
async def test_a_persistently_failing_member_is_distinguishable_from_a_deferred_one(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _SelectivelyBrokenAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    for offset in range(4):
        when = NOW + timedelta(hours=offset)
        async with open_repository(db_path) as repository:
            async for _ in sync_module.sync(repository, _registry(5), now_fn=_at(when)):
                pass

    async with open_repository(db_path) as repository:
        stale = await repository.stale_members("greenhouse", NOW + timedelta(days=1))
        bucket = (await repository.load_rate_state())["boards-api.greenhouse.io"]

    assert bucket.consecutive_failures == 0, (
        "the shared bucket is blind to one bad member, which is why visits exist"
    )

    broken = [visit for visit in stale if visit.board == "greenhouse:t1"]
    assert len(broken) == 1
    assert broken[0].never_succeeded
    assert broken[0].label == "tenant-01", (
        "the display name is carried as a caption; the board is the identity"
    )
    assert broken[0].consecutive_failures == 2, (
        "four runs at a slice of two select tenant-01 twice; both failures must count"
    )
    assert "tenant returns 500" in broken[0].last_error

    assert stale[0].board == "greenhouse:t1", "never-succeeded members order first"
    healthy = {visit.board for visit in stale} - {"greenhouse:t1"}
    assert all(
        not visit.never_succeeded for visit in stale if visit.board in healthy
    ), "a member that has been fetched is stale by date, not by never having worked"


@respx.mock
async def test_a_member_not_yet_reached_by_rotation_has_no_visit_row_at_all(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async with open_repository(db_path) as repository:
        async for _ in sync_module.sync(repository, _registry(5), now_fn=_at(NOW)):
            pass
        recorded = {
            visit.board
            for visit in await repository.stale_members("greenhouse", NOW + timedelta(days=1))
        }

    assert recorded == {"greenhouse:t0", "greenhouse:t1"}
    assert "greenhouse:t4" not in recorded


@respx.mock
async def test_a_deferred_board_is_never_marked_closed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import Job, JobStatus
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW - timedelta(days=1),
                jobs=(
                    Job(
                        id="greenhouse-deferred",
                        source="greenhouse",
                        company="tenant-04",
                        title_raw="Software Engineer Intern",
                        title_normalized="software engineer intern",
                        apply_url_raw="https://example.test/1",
                        description="",
                        first_seen=NOW - timedelta(days=1),
                        last_seen=NOW - timedelta(days=1),
                    ),
                ),
            )
        )
        async for _ in sync_module.sync(repository, _registry(5), now_fn=lambda: NOW):
            pass
        stored = await repository.get_job("greenhouse-deferred")

    assert stored is not None
    assert stored.status is JobStatus.OPEN


@respx.mock
async def test_a_dry_run_reports_a_block_and_writes_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import SourceBlocked
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    route = respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                rate_state=(
                    RateState(
                        bucket="boards-api.greenhouse.io",
                        updated_at=NOW,
                        blocked_until=NOW + timedelta(hours=3),
                        rotation_cursor="tenant-02",
                        reason="HTTP 429",
                    ),
                ),
            )
        )
        events = [
            event
            async for event in sync_module.sync(
                repository, _registry(5), dry_run=True, now_fn=_at(NOW)
            )
        ]
        after = (await repository.load_rate_state())["boards-api.greenhouse.io"]

    assert [event for event in events if isinstance(event, SourceBlocked)]
    assert not [event for event in events if isinstance(event, PlannedRequest)]
    assert not route.called

    assert after.rotation_cursor == "tenant-02", "a preview must not advance rotation"
    assert after.blocked_until == NOW + timedelta(hours=3)


@respx.mock
async def test_a_dry_run_on_a_clear_bucket_still_leaves_the_cursor_alone(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)

    async with open_repository(db_path) as repository:
        async for _ in sync_module.sync(
            repository, _registry(5), dry_run=True, now_fn=_at(NOW)
        ):
            pass
        assert await repository.load_rate_state() == {}
        assert await repository.stale_members("greenhouse", NOW + timedelta(days=1)) == []


@respx.mock
async def test_a_rotated_run_records_what_it_deferred_so_history_stays_readable(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    async with open_repository(db_path) as repository:
        async for _ in sync_module.sync(repository, _registry(5), now_fn=_at(NOW)):
            pass

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source, fetched, deferred FROM sync_run_sources WHERE source = 'greenhouse'"
    ).fetchone()
    conn.close()

    assert row["deferred"] == 3


@respx.mock
async def test_a_blocked_source_records_history_rather_than_recording_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)

    async with open_repository(db_path) as repository:
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=NOW,
                rate_state=(
                    RateState(
                        bucket="boards-api.greenhouse.io",
                        updated_at=NOW,
                        blocked_until=NOW + timedelta(hours=3),
                        reason="HTTP 429",
                    ),
                ),
            )
        )
        async for _ in sync_module.sync(repository, _registry(5), now_fn=_at(NOW)):
            pass

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT blocked, deferred, fetched FROM sync_run_sources WHERE source = 'greenhouse'"
    ).fetchone()
    conn.close()

    assert row is not None, "a blocked source must leave a trace in run history"
    assert row["blocked"] == 1
    assert row["deferred"] == 0, "blocking is not rotation and must not borrow its counter"
    assert row["fetched"] == 0


@respx.mock
async def test_two_sources_on_one_bucket_pace_as_one(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    class _Twin(_CountingAdapter):
        name = "greenhouse-twin"
        platform = Platform.LEVER
        rotation_slice = 0

    adapters = {Platform.GREENHOUSE: _CountingAdapter(), Platform.LEVER: _Twin()}
    monkeypatch.setattr(sync_module, "adapter_for_platform", adapters.get)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    seen: list[int] = []
    class _Probe(HttpClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            seen.append(id(self._budgets))

    monkeypatch.setattr(sync_module, "HttpClient", _Probe)

    companies = [
        Company(name="a", platform=Platform.GREENHOUSE, slug="a"),
        Company(name="b", platform=Platform.LEVER, slug="b"),
    ]
    async with open_repository(db_path) as repository:
        async for _ in sync_module.sync(repository, companies, now_fn=_at(NOW)):
            pass

    assert len(seen) == 2
    assert seen[0] == seen[1], "one bucket must mean one budget dict across both clients"


def test_the_shipped_feeds_really_do_share_a_bucket() -> None:
    from stage.services.sync import _bucket_keys
    from stage.sources import get_feeds as real_feeds

    feeds = real_feeds()
    buckets = {
        name: _bucket_keys(feed.hosts, feed.bucket_key) for name, feed in feeds.items()
    }
    assert buckets["simplify"] == buckets["vanshb03"] == ("raw.githubusercontent.com",)


def test_a_shared_bucket_takes_the_strictest_posture_claimed_for_it() -> None:
    from stage.services.sync import _bucket_postures
    from stage.sources import get_feeds as real_feeds

    postures = _bucket_postures({}, real_feeds())
    shared = postures["raw.githubusercontent.com"]
    assert shared.concurrency == 2
    assert shared.max_requests_per_run == 20


def test_a_disabled_row_cannot_constrain_a_bucket_nothing_is_fetching() -> None:
    from stage.services.sync import _bucket_postures, _select

    companies = [
        Company(name="live", platform=Platform.GREENHOUSE, slug="live"),
        Company(
            name="off",
            platform=Platform.GREENHOUSE,
            slug="off",
            enabled=False,
            rate_profile="conservative",
        ),
    ]
    grouped, feeds, _ = _select(companies, ["greenhouse"])
    postures = _bucket_postures(grouped, {})

    assert postures["boards-api.greenhouse.io"] == profile("standard")


def test_a_source_excluded_by_the_source_flag_does_not_constrain_a_bucket_either() -> None:
    from stage.services.sync import _bucket_postures, _select

    grouped, feeds, _ = _select(
        [Company(name="live", platform=Platform.GREENHOUSE, slug="live")], ["greenhouse"]
    )
    assert feeds == {}
    assert "raw.githubusercontent.com" not in _bucket_postures(grouped, feeds)


@respx.mock
async def test_each_run_gets_fresh_budgets_so_a_second_sync_is_not_pre_spent(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.services import sync as sync_module

    adapter = _CountingAdapter()
    monkeypatch.setattr(sync_module, "adapter_for_platform", lambda _: adapter)
    monkeypatch.setattr(sync_module, "get_feeds", dict)
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )

    captured: list[Mapping[str, object]] = []
    class _Probe(HttpClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            captured.append(self._budgets)

    monkeypatch.setattr(sync_module, "HttpClient", _Probe)

    async with open_repository(db_path) as repository:
        for offset in range(2):
            when = NOW + timedelta(hours=offset)
            async for _ in sync_module.sync(repository, _registry(5), now_fn=_at(when)):
                pass

    assert len(captured) == 2
    first, second = captured
    assert first is not second, "a run must not inherit another run's budget dict"

    spent = [budget.requests for budget in second.values()]  # type: ignore[attr-defined]
    assert spent == [2], "the second run's ceiling starts from zero, not from the first's"


def test_high_priority_members_come_out_of_the_slice_never_on_top_of_it() -> None:
    from stage.sources.workday import WorkdayAdapter

    members = _members(82, always={"tenant-00", "tenant-81"})
    cursor = ""
    covered: set[str] = set()
    runs = 0
    while runs < 6:
        runs += 1
        result = rotate(members, cursor=cursor, budget=WorkdayAdapter.rotation_slice)
        assert len(result.selected) == WorkdayAdapter.rotation_slice, (
            "a run must cost exactly the slice, never the slice plus the always-on rows"
        )
        assert {"tenant-00", "tenant-81"} <= set(result.selected)
        covered |= set(result.selected)
        cursor = result.cursor
        if len(covered) == len(members):
            break

    assert covered == {member.key for member in members}
    assert runs == 3, "82 tenants with 2 always-on at slice 40 still closes in three runs"


def test_two_registry_rows_sharing_a_name_are_two_ring_members() -> None:
    from stage.services.sync import _bucket_keys

    assert _bucket_keys(frozenset({"a"}), "") == ("a",)

    first = Company(
        name="Mastercard",
        platform=Platform.WORKDAY,
        slug="mastercard",
        workday_tenant="mastercard",
        workday_site="CUOReqSite",
        workday_dc="wd1",
    )
    second = replace(first, workday_site="CorporateCareers")

    assert first.registry_key != second.registry_key
    members = [RotationMember(key=row.registry_key) for row in (first, second)]
    result = rotate(members, budget=1)
    assert len(result.selected) == 1, "one budget unit selects one board, never two"
    assert len(result.deferred) == 1
