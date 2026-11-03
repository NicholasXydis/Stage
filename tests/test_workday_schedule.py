import json
import re
import types
import zlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import pytest
import respx

import stage.http.breaker as breaker_module
import stage.http.client as client_module
from stage.companies import load_companies
from stage.domain import Company, Platform, SourceCapped
from stage.http import REFRESH_SLACK_H, profile
from stage.http.profiles import NORMAL_REFRESH_H
from stage.services import screening
from stage.services.sync import _quiet_day, _reserve_for, sync
from stage.sources.workday import (
    BROAD_TEXT,
    NARROW_TEXT,
    PAGE_SIZE,
    WHOLE_READ_BOARDS,
    WorkdayAdapter,
)
from stage.storage import AsyncRepository, open_repository

START = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
TICK = timedelta(minutes=30)
RUNS = 13
CXS = re.compile(r"https://[^/]+\.myworkdayjobs\.com/wday/cxs/([^/]+)/([^/]+)/jobs")


def _share(key: str, salt: str) -> float:
    return zlib.crc32(f"{salt}:{key}".encode()) / 0xFFFFFFFF


def _kind(company: Company) -> str:
    if WorkdayAdapter().board_key(company) in WHOLE_READ_BOARDS:
        return "whole"
    if company.workday_facet:
        return "facet"
    roll = _share(company.registry_key, "kind")
    return "facet" if roll < 0.63 else "small" if roll < 0.84 else "huge"


def _row(tenant: str, index: int, title: str, age: int) -> dict[str, object]:
    return {
        "title": title,
        "externalPath": f"/job/Toronto/{title.replace(' ', '-')}_R{index:05d}",
        "locationsText": "Toronto, ON, Canada",
        "postedOn": "Posted Today" if not age else f"Posted {age} Days Ago",
        "bulletFields": [f"{tenant[:3].upper()}{index:05d}"],
    }


def _answer(tenant: str, kind: str, body: dict[str, object]) -> httpx.Response:
    offset = int(str(body["offset"]))
    if body["searchText"] == NARROW_TEXT:
        return httpx.Response(200, json={"total": 0, "jobPostings": []})
    if body["searchText"]:
        assert body["searchText"] == BROAD_TEXT
        found = 3 + int(_share(tenant, "found") * 30)
        rows = [_row(tenant, 5000 + i, f"Software Intern {i}", i // 4) for i in range(found)]
        page = rows[offset : offset + PAGE_SIZE]
        return httpx.Response(200, json={"total": 0 if offset else found, "jobPostings": page})
    facets = [{"facetParameter": "timeType", "values": [{"id": "f-full", "descriptor": "Full"}]}]
    if kind == "facet":
        intern = {"id": "f-intern", "descriptor": "Intern", "count": 2}
        facets.append({"facetParameter": "workerSubType", "values": [intern]})
    if body["appliedFacets"]:
        rows = [_row(tenant, i, f"Data Intern {i}", i) for i in range(2)]
        return httpx.Response(200, json={"total": 2, "jobPostings": rows, "facets": facets})
    sizes = {"facet": 300, "small": 50, "whole": 500, "huge": 300 + int(_share(tenant, "n") * 2000)}
    size = sizes[kind]
    newest = _share(tenant, "order") < 0.77
    page = [
        _row(tenant, i, f"Analyst {i}", i // 40 if newest else (i * 7) % 5)
        for i in range(offset, min(offset + PAGE_SIZE, size))
    ]
    listed = {
        "total": 0 if offset else size,
        "jobPostings": page,
        "facets": [] if offset else facets,
    }
    return httpx.Response(200, json=listed)


def _at(moment: datetime) -> datetime:
    return moment


def _virtual_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    clock = [1_000_000.0]
    pause = asyncio.sleep

    async def sleep(seconds: float, result: object = None) -> object:
        clock[0] += max(0.0, seconds)
        return await pause(0, result)

    paced = types.SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    paced.sleep = sleep
    now = types.SimpleNamespace(monotonic=lambda: clock[0], perf_counter=lambda: clock[0])
    monkeypatch.setattr(client_module, "asyncio", paced)
    monkeypatch.setattr(client_module, "time", now)
    monkeypatch.setattr(breaker_module, "time", now)


async def test_every_shipped_workday_board_is_read_within_its_window_and_the_day_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _virtual_time(monkeypatch)
    monkeypatch.setattr(screening, "POOL_THRESHOLD", 10**9)
    companies = [c for c in load_companies() if c.enabled and c.platform is Platform.WORKDAY]
    adapter = WorkdayAdapter()
    kinds = {
        ((c.workday_tenant or "").lower(), (c.workday_site or "").lower()): _kind(c)
        for c in companies
    }
    busy = {adapter.board_key(c): (3, 1) for c in companies if _share(c.registry_key, "busy") < 0.1}

    async def activity(*_: object) -> dict[str, tuple[int, int]]:
        return dict(busy)

    async def pushback(*_: object) -> frozenset[str]:
        return frozenset({"workday"})

    monkeypatch.setattr(AsyncRepository, "board_activity", activity)
    monkeypatch.setattr(AsyncRepository, "pushback_since", pushback)

    def listing(request: httpx.Request) -> httpx.Response:
        match = CXS.match(str(request.url))
        assert match is not None
        tenant, site = match.groups()
        return _answer(tenant, kinds[tenant.lower(), site.lower()], json.loads(request.content))

    detail = httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Build.</p>"}})
    reads: dict[str, list[datetime]] = {adapter.board_key(c): [] for c in companies}
    spent: list[int] = []
    capped = 0
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=CXS.pattern).mock(side_effect=listing)
        mock.get(url__regex=r"https://[^/]+\.myworkdayjobs\.com/.*").mock(return_value=detail)
        for index in range(RUNS):
            moment = START + TICK * index + timedelta(minutes=index * 7 % 6)
            before = route.call_count
            async with open_repository(tmp_path / "stage.db") as repository:
                async for event in sync(
                    repository, companies, sources=("workday",), now_fn=partial(_at, moment)
                ):
                    capped += isinstance(event, SourceCapped) and not event.allowance
                for visit in await repository.all_visits():
                    if visit.last_success_at == moment:
                        reads[visit.board].append(moment)
            spent.append(route.call_count - before)

    window = NORMAL_REFRESH_H + REFRESH_SLACK_H
    gaps = Counter(
        {
            board: max((b - a).total_seconds() / 3600 for a, b in zip(seen, seen[1:], strict=False))
            for board, seen in reads.items()
            if len(seen) > 1
        }
    )
    unread = [board for board, seen in reads.items() if len(seen) < 2]
    assert not unread, f"{len(unread)} boards were not read twice in {RUNS / 2:g} hours"
    worst, hours = gaps.most_common(1)[0]
    assert hours <= window, f"{worst} waited {hours:.2f} h between reads"
    assert not capped, "the daily allowance ran out"
    posture = profile("workday").sized_for(len(companies), _reserve_for(adapter))
    day = sum(spent[1:12]) * 24 / NORMAL_REFRESH_H
    cap = _quiet_day(posture, len(companies))
    assert day <= cap, f"a day of Workday reads needs {day:.0f} requests against {cap}"
