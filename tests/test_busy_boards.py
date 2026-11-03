import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from stage.domain import Company, Platform, SourceFresh
from stage.services.sync import sync
from stage.storage import open_repository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
MUSE = "https://www.themuse.com/api/public/jobs"
SIMPLIFY = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/"
    "scripts/listings.json"
)


def _board(slug: str) -> Company:
    return Company(name=slug.title(), platform=Platform.GREENHOUSE, slug=slug)


def _jobs(extra: int = 0) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = payload["jobs"][0]
    for number in range(extra):
        payload["jobs"].append(
            {**first, "id": 9_000_000 + number, "title": f"Software Engineering Intern {number}"}
        )
    return payload


def _listing(identifier: str, company: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "company_name": company,
        "title": "Software Engineering Intern",
        "url": f"https://example.com/{identifier}",
        "locations": ["Toronto, ON"],
        "terms": ["Summer 2027"],
    }


async def _sync(
    db: Path, companies: list[Company], moment: datetime, *sources: str
) -> list[object]:
    async with open_repository(db) as repository:
        return [
            event
            async for event in sync(
                repository, companies, sources=list(sources), now_fn=lambda: moment
            )
        ]


@respx.mock
async def test_a_new_boards_first_postings_do_not_make_it_busy(tmp_path: Path) -> None:
    route = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=_jobs())
    )
    db = tmp_path / "stage.db"

    await _sync(db, [_board("acme")], NOW, "greenhouse")
    await _sync(db, [_board("acme")], NOW + timedelta(hours=1), "greenhouse")

    assert route.call_count == 1, "a first read is backlog, not news"


@respx.mock
async def test_a_board_with_a_new_internship_this_week_is_read_every_hour(
    tmp_path: Path,
) -> None:
    busy = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        side_effect=[httpx.Response(200, json=_jobs())]
        + [httpx.Response(200, json=_jobs(extra=1))] * 3
    )
    quiet = respx.get("https://boards-api.greenhouse.io/v1/boards/calm/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    boards = [_board("acme"), _board("calm")]
    db = tmp_path / "stage.db"

    await _sync(db, boards, NOW, "greenhouse")
    await _sync(db, boards, NOW + timedelta(hours=6), "greenhouse")
    events = await _sync(db, boards, NOW + timedelta(hours=7), "greenhouse")

    assert busy.call_count == 3
    assert quiet.call_count == 2
    assert any(isinstance(event, SourceFresh) and event.skipped == 1 for event in events)


@respx.mock
async def test_a_quiet_board_is_read_again_within_six_hours(tmp_path: Path) -> None:
    route = respx.get("https://boards-api.greenhouse.io/v1/boards/calm/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    db = tmp_path / "stage.db"

    await _sync(db, [_board("calm")], NOW, "greenhouse")
    await _sync(db, [_board("calm")], NOW + timedelta(hours=5), "greenhouse")
    assert route.call_count == 1
    await _sync(db, [_board("calm")], NOW + timedelta(hours=5, minutes=30), "greenhouse")
    assert route.call_count == 2


@respx.mock
async def test_a_clock_that_jumped_back_does_not_freeze_a_board(tmp_path: Path) -> None:
    route = respx.get("https://boards-api.greenhouse.io/v1/boards/calm/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    db = tmp_path / "stage.db"

    await _sync(db, [_board("calm")], NOW, "greenhouse")
    await _sync(db, [_board("calm")], NOW - timedelta(days=1), "greenhouse")

    assert route.call_count == 2, "a read stamped in the future must not count as recent"


@respx.mock
async def test_a_community_list_tip_reads_the_employers_board_on_the_next_run(
    tmp_path: Path,
) -> None:
    board = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=_jobs())
    )
    respx.get(SIMPLIFY).mock(
        side_effect=[
            httpx.Response(200, json=[_listing("old", "Acme")]),
            httpx.Response(200, json=[_listing("old", "Acme"), _listing("new", "Acme")]),
            httpx.Response(200, json=[_listing("old", "Acme"), _listing("new", "Acme")]),
            httpx.Response(200, json=[_listing("old", "Acme"), _listing("new", "Acme")]),
        ]
    )
    db = tmp_path / "stage.db"
    acme = [_board("acme")]

    await _sync(db, acme, NOW, "greenhouse", "simplify")
    await _sync(db, acme, NOW + timedelta(minutes=30), "greenhouse", "simplify")
    assert board.call_count == 1, "a list's first read is backlog and tips nothing"
    await _sync(db, acme, NOW + timedelta(hours=1), "greenhouse", "simplify")
    assert board.call_count == 2, "the new listing tips Acme's own board"
    await _sync(db, acme, NOW + timedelta(hours=1, minutes=30), "greenhouse", "simplify")
    assert board.call_count == 2, "a tip is used once"


def _avoid(*names: str) -> None:
    from stage.domain.preferences import ChosenCompany, Preferences
    from stage.lexicon import fold
    from stage.services.preferences import save_preferences

    chosen = tuple(ChosenCompany(name=name, folded=fold(name)) for name in names)
    save_preferences(Preferences(avoid=chosen))


@respx.mock
async def test_an_avoided_busy_board_keeps_the_normal_window_not_the_hourly_floor(
    tmp_path: Path,
) -> None:
    routes = {
        slug: respx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").mock(
            side_effect=[httpx.Response(200, json=_jobs())]
            + [httpx.Response(200, json=_jobs(extra=1))] * 4
        )
        for slug in ("acme", "rbc")
    }
    boards = [_board("acme"), _board("rbc")]
    db = tmp_path / "stage.db"
    _avoid("Rbc")

    await _sync(db, boards, NOW, "greenhouse")
    await _sync(db, boards, NOW + timedelta(hours=6), "greenhouse")
    await _sync(db, boards, NOW + timedelta(hours=7), "greenhouse")

    assert routes["acme"].call_count == 3, "a busy board is read every hour"
    assert routes["rbc"].call_count == 2, "an avoided one is not, however busy"
    await _sync(db, boards, NOW + timedelta(hours=11, minutes=30), "greenhouse")
    assert routes["rbc"].call_count == 3, "it is still read on its ordinary window"


@respx.mock
async def test_a_list_posting_does_not_tip_an_avoided_employers_board(tmp_path: Path) -> None:
    board = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=_jobs())
    )
    respx.get(SIMPLIFY).mock(
        side_effect=[httpx.Response(200, json=[_listing("old", "Acme")])]
        + [httpx.Response(200, json=[_listing("old", "Acme"), _listing("new", "Acme")])] * 2
    )
    db = tmp_path / "stage.db"
    acme = [_board("acme")]
    _avoid("Acme")

    await _sync(db, acme, NOW, "greenhouse", "simplify")
    await _sync(db, acme, NOW + timedelta(minutes=30), "greenhouse", "simplify")
    await _sync(db, acme, NOW + timedelta(hours=1), "greenhouse", "simplify")

    assert board.call_count == 1, "the user said never to show this employer"


@respx.mock
async def test_a_search_feed_rests_between_reads_but_force_refresh_reads_it(
    tmp_path: Path,
) -> None:
    route = respx.get(url__startswith=MUSE).mock(
        return_value=httpx.Response(200, json={"results": [], "page_count": 1})
    )
    db = tmp_path / "stage.db"

    await _sync(db, [], NOW, "themuse")
    calls = route.call_count
    events = await _sync(db, [], NOW + timedelta(hours=1), "themuse")

    assert calls > 0
    assert route.call_count == calls
    assert any(isinstance(event, SourceFresh) for event in events)

    async with open_repository(db) as repository:
        [
            event
            async for event in sync(
                repository,
                [],
                sources=["themuse"],
                now_fn=lambda: NOW + timedelta(hours=1),
                force_refresh=True,
            )
        ]
    assert route.call_count > calls


@respx.mock
async def test_a_host_that_asks_for_an_hour_postpones_its_other_boards_without_failing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from stage.domain import CompanyDeferred, CompanyFailed
    from stage.http import profiles

    one_at_a_time = {
        name: replace(posture, concurrency=1, min_interval_s=0.0)
        for name, posture in profiles.PROFILES.items()
    }
    monkeypatch.setattr(profiles, "PROFILES", MappingProxyType(one_at_a_time))

    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"})
    )
    calm = [_board(f"calm{index}") for index in range(3)]
    routes = [
        respx.get(f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )
        for board in calm
    ]
    db = tmp_path / "stage.db"
    await _sync(db, calm, NOW - timedelta(hours=6), "greenhouse")
    events = await _sync(db, [_board("acme"), *calm], NOW, "greenhouse")
    async with open_repository(db) as repository:
        visits = {visit.board: visit for visit in await repository.all_visits()}

    postponed = {event.company for event in events if isinstance(event, CompanyDeferred)}
    failed = {event.company for event in events if isinstance(event, CompanyFailed)}
    untouched = [board for board, route in zip(calm, routes, strict=True) if route.call_count == 1]
    assert untouched, "the hour-long wait reached the boards read after the 429"
    for board in untouched:
        assert board.name in postponed and board.name not in failed
        assert visits[f"greenhouse:{board.slug}"].consecutive_failures == 0


def _one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from stage.http import profiles

    serial = {
        name: replace(posture, concurrency=1, min_interval_s=0.0)
        for name, posture in profiles.PROFILES.items()
    }
    monkeypatch.setattr(profiles, "PROFILES", MappingProxyType(serial))


@respx.mock
async def test_boards_are_read_never_read_first_then_stalest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_at_a_time(monkeypatch)
    groups = {
        name: [_board(f"{name}{index}") for index in range(8)] for name in ("new", "old", "mid")
    }
    for boards in groups.values():
        for board in boards:
            respx.get(f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs").mock(
                return_value=httpx.Response(200, json={"jobs": []})
            )
    db = tmp_path / "stage.db"
    await _sync(db, groups["old"], NOW - timedelta(hours=20), "greenhouse")
    await _sync(db, groups["mid"], NOW - timedelta(hours=10), "greenhouse")
    before = len(respx.calls)

    await _sync(db, [*groups["mid"], *groups["old"], *groups["new"]], NOW, "greenhouse")

    calls = list(respx.calls)[before:]
    order = [call.request.url.path.split("/")[3].rstrip("0123456789") for call in calls]
    assert order == ["new"] * 8 + ["old"] * 8 + ["mid"] * 8


@respx.mock
async def test_a_sync_stopped_early_leaves_no_board_read_running(tmp_path: Path) -> None:
    import asyncio

    from stage.domain import CompanyStarted

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        return httpx.Response(200, json={"jobs": []})

    for index in range(3):
        respx.get(f"https://boards-api.greenhouse.io/v1/boards/slow{index}/jobs").mock(
            side_effect=hang
        )
    boards = [_board(f"slow{index}") for index in range(3)]
    async with open_repository(tmp_path / "stage.db") as repository:
        stream = cast(
            AsyncGenerator[object],
            sync(repository, boards, sources=["greenhouse"], now_fn=lambda: NOW),
        )
        async for event in stream:
            if isinstance(event, CompanyStarted):
                break
        await stream.aclose()

    others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    assert [task for task in others if not task.done()] == []


@respx.mock
@pytest.mark.parametrize("sources", [["greenhouse"], ["greenhouse", "lever"]])
async def test_closing_a_sync_cancels_its_board_reads_before_it_returns(
    tmp_path: Path, sources: list[str]
) -> None:
    import asyncio

    from stage.domain import CompanyStarted

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        return httpx.Response(200, json={"jobs": []})

    respx.get(url__regex=r"^https://(boards-api\.greenhouse\.io|api\.lever\.co)/").mock(
        side_effect=hang
    )
    boards = [
        *(_board(f"slow{index}") for index in range(3)),
        Company(name="Lever Slow", platform=Platform.LEVER, slug="leverslow"),
    ]
    async with open_repository(tmp_path / "stage.db") as repository:
        stream = cast(
            AsyncGenerator[object],
            sync(repository, boards, sources=sources, now_fn=lambda: NOW),
        )
        started = 0
        async for event in stream:
            started += isinstance(event, CompanyStarted)
            if started == len(sources) + 2:
                break
        await stream.aclose()
        running = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]

    assert running == []
