import json
from collections.abc import AsyncGenerator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import httpx
import pytest
import respx

from stage.cli import notify
from stage.domain import Company, Platform, SyncFinished
from stage.http import profiles
from stage.services.sync import sync
from stage.storage import open_repository

WEBHOOK = "https://discord.com/api/webhooks/1/tok"
CXS = "https://big.wd5.myworkdayjobs.com/wday/cxs/big/careers/jobs"
FACETS = [
    {
        "facetParameter": "timeType",
        "descriptor": "Time Type",
        "values": [
            {"id": "f-full", "descriptor": "Full time", "count": 400},
            {"id": "f-intern", "descriptor": "Intern", "count": 200},
        ],
    }
]


class _Console:
    def print(self, *_: object, **__: object) -> None:
        return None


def _big() -> Company:
    return Company(
        name="Big",
        platform=Platform.WORKDAY,
        slug="big",
        workday_tenant="big",
        workday_site="careers",
        workday_dc="wd5",
    )


def _serve_interns(total: int) -> list[int]:
    offsets: list[int] = []

    def page(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset = int(body["offset"])
        if not body["appliedFacets"]:
            return httpx.Response(200, json={"total": 600, "jobPostings": [], "facets": FACETS})
        offsets.append(offset)
        rows = [
            {
                "title": f"Software Engineering Intern {index}",
                "externalPath": f"/job/Toronto/Software-Intern_R{index:05d}",
                "locationsText": "Toronto, ON",
                "postedOn": "Posted 3 Days Ago",
                "bulletFields": [f"R{index:05d}"],
            }
            for index in range(offset, min(offset + 20, total))
        ]
        return httpx.Response(200, json={"total": total, "jobPostings": rows, "facets": FACETS})

    respx.post(CXS).mock(side_effect=page)
    return offsets


def _unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    fast = {
        name: replace(posture, min_interval_s=0.0) for name, posture in profiles.PROFILES.items()
    }
    monkeypatch.setattr(profiles, "PROFILES", MappingProxyType(fast))


def _capture_discord(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class _Response:
        status = 204

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def capture(request: Any, timeout: object = None) -> _Response:
        sent.append(json.loads(request.data.decode()))
        return _Response()

    monkeypatch.setattr(notify, "read", lambda *_, **__: WEBHOOK)
    monkeypatch.setattr(notify, "urlopen", capture)
    return sent


@respx.mock
async def test_the_last_leg_of_a_workday_first_crawl_is_announced_as_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli.commands.pipeline import _announce_new_postings

    _unpaced(monkeypatch)
    sent = _capture_discord(monkeypatch)
    offsets = _serve_interns(200)
    db = tmp_path / "w.db"

    async def scheduled_run() -> SyncFinished:
        finished: list[SyncFinished] = []
        async with open_repository(db) as repository:
            async for event in sync(repository, [_big()], sources=["workday"], force_refresh=True):
                if isinstance(event, SyncFinished):
                    finished.append(event)
            await _announce_new_postings(repository, _Console())
        return finished[-1]

    first = await scheduled_run()
    assert first.first_reads == ("workday:big-careers",)
    async with open_repository(db) as repository:
        assert "workday:big-careers" in await repository.load_workday_crawls()
    assert sent == []

    await scheduled_run()
    async with open_repository(db) as repository:
        assert await repository.load_workday_crawls() == {}
    assert offsets[-1] == 180
    assert sent == [], sent[0]["embeds"][0]["title"] if sent else ""


GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _greenhouse_jobs(count: int) -> dict[str, list[dict[str, Any]]]:
    return {
        "jobs": [
            {
                "id": 1000 + index,
                "title": f"Software Engineering Intern {index}",
                "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{1000 + index}",
                "location": {"name": "Toronto, ON"},
                "updated_at": "2026-10-30T12:00:00-04:00",
            }
            for index in range(count)
        ]
    }


@respx.mock
async def test_an_interrupted_first_read_is_announced_by_the_next_scheduled_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli.commands.pipeline import _announce_new_postings
    from stage.domain import SourceFinished

    _unpaced(monkeypatch)
    sent = _capture_discord(monkeypatch)
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, json=_greenhouse_jobs(30)))
    acme = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    db = tmp_path / "i.db"
    notify.mark_announced(datetime.now(UTC) - timedelta(minutes=30))

    async with open_repository(db) as repository:
        stream = cast(
            AsyncGenerator[object, None], sync(repository, [acme], sources=["greenhouse"])
        )
        async for event in stream:
            if isinstance(event, SourceFinished):
                break
        await stream.aclose()

    async with open_repository(db) as repository:
        await _announce_new_postings(repository, _Console())
    assert sent == [], sent[0]["embeds"][0]["title"] if sent else ""


@respx.mock
async def test_stage_canary_on_the_real_database_closes_every_other_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import JobFilters, JobStatus
    from stage.services.canary import canary

    _unpaced(monkeypatch)
    acme = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    zeta = Company(name="Zeta", platform=Platform.GREENHOUSE, slug="zeta")
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, json=_greenhouse_jobs(2)))
    other = _greenhouse_jobs(3)
    for row in other["jobs"]:
        row["absolute_url"] = row["absolute_url"].replace("acme", "zeta")
    respx.get("https://boards-api.greenhouse.io/v1/boards/zeta/jobs").mock(
        return_value=httpx.Response(200, json=other)
    )
    db = tmp_path / "c.db"
    async with open_repository(db) as repository:
        async for _ in sync(repository, [acme, zeta], sources=["greenhouse"]):
            pass
        before = await repository.list_jobs(JobFilters(limit=None, status=JobStatus.OPEN))
        assert {job.id.split(":")[1] for job in before} == {"acme", "zeta"}

        await canary(repository, [acme, zeta])

        after = await repository.list_jobs(JobFilters(limit=None, status=JobStatus.OPEN))
    assert {job.id.split(":")[1] for job in after} == {"acme", "zeta"}


@respx.mock
async def test_postings_the_canary_closed_stay_closed_while_their_board_answers_304(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.domain import JobFilters, JobStatus
    from stage.services.canary import canary

    _unpaced(monkeypatch)
    acme = Company(name="Acme", platform=Platform.GREENHOUSE, slug="acme")
    zeta = Company(name="Zeta", platform=Platform.GREENHOUSE, slug="zeta")
    respx.get(GREENHOUSE).mock(return_value=httpx.Response(200, json=_greenhouse_jobs(2)))
    other = _greenhouse_jobs(3)
    for row in other["jobs"]:
        row["absolute_url"] = row["absolute_url"].replace("acme", "zeta")

    def zeta_board(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(200, json=other, headers={"ETag": '"v1"'})

    respx.get("https://boards-api.greenhouse.io/v1/boards/zeta/jobs").mock(side_effect=zeta_board)
    db = tmp_path / "d.db"
    async with open_repository(db) as repository:
        async for _ in sync(repository, [acme, zeta], sources=["greenhouse"]):
            pass
        await canary(repository, [acme, zeta])
        async for _ in sync(repository, [acme, zeta], sources=["greenhouse"], force_refresh=True):
            pass
        after = await repository.list_jobs(JobFilters(limit=None, status=JobStatus.OPEN))
    assert {job.id.split(":")[1] for job in after} == {"acme", "zeta"}


def test_a_personal_workday_site_beside_a_shipped_site_of_the_same_tenant_is_kept() -> None:
    from stage.companies import personal_kept

    shipped = Company(
        name="Adobe",
        platform=Platform.WORKDAY,
        slug="adobe",
        workday_tenant="adobe",
        workday_site="external_experienced",
        workday_dc="wd5",
    )
    mine = replace(shipped, name="Adobe University", workday_site="university")
    assert personal_kept([shipped], [mine]) == (mine,)


NO_INTERN = [
    {
        "facetParameter": "timeType",
        "descriptor": "Time Type",
        "values": [{"id": "f-full", "descriptor": "Full time", "count": 900}],
    }
]


def _serve_huge(total: int) -> list[int]:
    offsets: list[int] = []

    def page(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["searchText"]:
            return httpx.Response(200, json={"total": 0, "jobPostings": []})
        offset = int(body["offset"])
        offsets.append(offset)
        rows = [
            {
                "title": f"Software Engineering Intern {index}",
                "externalPath": f"/job/Toronto/Software-Intern_R{index:05d}",
                "locationsText": "Toronto, ON",
                "postedOn": "Posted Today" if index < 40 else "Posted 30+ Days Ago",
                "bulletFields": [f"R{index:05d}"],
            }
            for index in range(offset, min(offset + 20, total))
        ]
        return httpx.Response(200, json={"total": total, "jobPostings": rows, "facets": NO_INTERN})

    respx.post(CXS).mock(side_effect=page)
    respx.get(url__regex=r"https://big\.wd5\.myworkdayjobs\.com/wday/cxs/big/careers/job/.*").mock(
        return_value=httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "Interns"}})
    )
    return offsets


@respx.mock
async def test_the_first_weekly_full_read_of_a_quick_look_board_announces_its_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage.cli.commands.pipeline import _announce_new_postings

    _unpaced(monkeypatch)
    sent = _capture_discord(monkeypatch)
    offsets = _serve_huge(400)
    db = tmp_path / "h.db"

    async def scheduled_run() -> None:
        finished: list[SyncFinished] = []
        async with open_repository(db) as repository:
            async for event in sync(repository, [_big()], sources=["workday"], force_refresh=True):
                if isinstance(event, SyncFinished):
                    finished.append(event)
            await _announce_new_postings(repository, _Console())

    await scheduled_run()
    assert offsets == [0]
    assert sent == []

    import sqlite3

    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE detail_fetches SET fetched_at = ? WHERE id LIKE 'full:%'", (week_ago,))
    monkeypatch.setattr(
        "stage.services.sync.full_read_day", lambda _board: datetime.now(UTC).weekday()
    )
    await scheduled_run()
    assert len(offsets) == 21
    assert sent == [], sent[0]["embeds"][0]["title"] if sent else ""


@respx.mock
async def test_one_slow_description_does_not_throw_away_the_whole_workday_board() -> None:
    from stage.http import HttpClient, RatePosture
    from stage.sources.workday import WorkdayAdapter

    _serve_interns(40)
    respx.get(url__regex=r"https://big\.wd5\.myworkdayjobs\.com/wday/cxs/big/careers/job/.*").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    async with HttpClient(
        allowed_hosts=frozenset({"big.wd5.myworkdayjobs.com"}),
        posture=RatePosture(concurrency=1, min_interval_s=0.0, max_requests_per_run=500),
        bucket_key="workday",
        jitter=False,
    ) as client:
        result = await WorkdayAdapter().fetch(
            _big(), client, datetime.now(UTC), details=["workday:big-careers:r00001"]
        )
    assert len(result.jobs) == 40


@respx.mock
async def test_a_full_read_backlog_does_not_make_a_quick_look_board_hourly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from stage.http import RatePosture

    _unpaced(monkeypatch)
    _serve_huge(400)
    db = tmp_path / "a.db"

    async def run() -> None:
        async with open_repository(db) as repository:
            async for _ in sync(repository, [_big()], sources=["workday"], force_refresh=True):
                pass

    await run()
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE detail_fetches SET fetched_at = ? WHERE id LIKE 'full:%'", (week_ago,))
    monkeypatch.setattr(
        "stage.services.sync.full_read_day", lambda _board: datetime.now(UTC).weekday()
    )
    await run()
    now = datetime.now(UTC)
    async with open_repository(db) as repository:
        activity = await repository.board_activity(
            now - timedelta(days=30), now - timedelta(days=7)
        )
    month, week = activity.get("workday:big-careers", (0, 0))
    assert RatePosture().window(month, week) > 1.0, (month, week)


def test_a_page_of_equal_ages_is_no_evidence_of_newest_first() -> None:
    from stage.sources.workday import WorkdayPosting, _newest_first

    page = [WorkdayPosting(title=f"T{i}", postedOn="Posted 30+ Days Ago") for i in range(20)]
    assert not _newest_first(page)
