import json
from collections.abc import Sequence
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from stage.domain import Company, Platform, SyncEvent
from stage.services.sync import sync
from stage.storage import AsyncRepository, open_repository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
GOLDEN = Path(__file__).parent / "golden" / "sync_stream.json"
WHEN = datetime(2026, 8, 3, 12, tzinfo=UTC)

VOLATILE = frozenset(
    {
        "elapsed_ms",
        "started_at",
        "finished_at",
        "blocked_until",
        "remaining_s",
        "latency_p50_ms",
        "latency_p95_ms",
        "fetch_ms",
        "normalize_ms",
        "write_ms",
    }
)


def _payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def unpaced(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage.http import RatePosture
    from stage.services import sync as sync_module

    monkeypatch.setattr(
        sync_module,
        "resolve",
        lambda *_: RatePosture(concurrency=4, min_interval_s=0.0, max_requests_per_run=300),
    )


def _shape(event: SyncEvent) -> dict[str, Any]:
    rendered: dict[str, Any] = {"event": type(event).__name__}
    for field in fields(event):
        if field.name in VOLATILE:
            continue
        value = getattr(event, field.name)
        if isinstance(value, (list, tuple, set, frozenset)):
            value = sorted(str(item) for item in value)
        elif isinstance(value, datetime):
            continue
        else:
            value = str(value)
        rendered[field.name] = value
    return rendered


def _company(slug: str, name: str) -> Company:
    return Company(name=name, platform=Platform.GREENHOUSE, slug=slug)


async def _stream(
    repository: AsyncRepository, companies: Sequence[Company]
) -> list[dict[str, Any]]:
    return [
        _shape(event)
        async for event in sync(
            repository,
            companies,
            sources=["greenhouse"],
            now_fn=lambda: WHEN,
            force_refresh=True,
        )
    ]


async def _stored(repository: AsyncRepository) -> list[tuple[str, ...]]:
    from stage.domain import JobFilters
    from stage.services.query import list_jobs

    listing = await list_jobs(repository, JobFilters(limit=None), window_days=None, now=WHEN)
    return sorted(
        (job.id, job.company, job.title_raw, job.status.value, job.role.value)
        for job in listing.jobs
    )


@respx.mock
async def test_the_event_stream_and_stored_rows_are_stable(tmp_path: Path) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=_payload())
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/globex/jobs").mock(
        return_value=httpx.Response(500, json={})
    )

    async with open_repository(tmp_path / "shape.db") as repository:
        stream = await _stream(repository, [_company("acme", "Acme"), _company("globex", "Globex")])
        rows = await _stored(repository)

    names = [entry["event"] for entry in stream]

    assert names[0] == "SyncStarted"
    assert names[-1] == "SyncFinished"
    assert "SourceStarted" in names
    assert names.index("SourceStarted") < names.index("SourceFinished")
    assert "CompanyFinished" in names
    assert "CompanyFailed" in names
    assert names.index("SourceStarted") < names.index("CompanyFinished")
    assert names.index("CompanyFinished") < names.index("SourceFinished")
    assert names.index("SourceFinished") < names.index("SyncFinished")

    finished = next(entry for entry in stream if entry["event"] == "SyncFinished")
    assert finished["outcome"] == "partial"
    assert finished["added"] == "3"
    assert sorted(finished["failed_sources"]) == ["greenhouse"]

    assert len(rows) == 3
    assert {row[1] for row in rows} == {"Acme"}
    assert {row[3] for row in rows} == {"open"}


@respx.mock
async def test_a_clean_run_reports_success_and_no_failures(tmp_path: Path) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=_payload())
    )

    async with open_repository(tmp_path / "clean.db") as repository:
        stream = await _stream(repository, [_company("acme", "Acme")])
        rows = await _stored(repository)

    finished = next(entry for entry in stream if entry["event"] == "SyncFinished")

    assert finished["outcome"] == "success"
    assert finished["failed_sources"] == []
    assert len(rows) == 3


OUTCOMES = frozenset(
    {
        "RequestLogged",
        "CompanyStarted",
        "CompanyFinished",
        "CompanyFailed",
        "CompanyUnchanged",
        "CompanyDeferred",
    }
)
LATER = WHEN + timedelta(minutes=10)
LATEST = WHEN + timedelta(hours=6)
WORKDAY_URL = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/"
    "scripts/listings.json"
)


def _line(event: SyncEvent) -> str:
    return " ".join(f"{key}={value}" for key, value in _shape(event).items())


def _canonical(events: Sequence[SyncEvent]) -> list[str]:
    lanes: dict[str, list[str]] = {}
    for event in events:
        lanes.setdefault(str(getattr(event, "source", "")), []).append(_line(event))
    lines: list[str] = []
    for source in sorted(lanes):
        run: list[str] = []
        for line in [*lanes[source], ""]:
            if line.split(" ", 1)[0].removeprefix("event=") in OUTCOMES:
                run.append(line)
                continue
            lines.extend(sorted(run))
            run = []
            if line:
                lines.append(line)
    return lines


def _mixed_registry() -> list[Company]:
    return [
        _company("acme", "Acme"),
        _company("globex", "Globex"),
        _company("initech", "Initech"),
        _company("wayne", "Wayne"),
        Company(name="Hooli", platform=Platform.LEVER, slug="hooli"),
        Company(name="Pied", platform=Platform.BAMBOOHR, slug="pied"),
        Company(name="Stark", platform=Platform.BAMBOOHR, slug="stark"),
        Company(
            name="Acme Workday",
            platform=Platform.WORKDAY,
            slug="acme",
            workday_tenant="acme",
            workday_site="External",
            workday_dc="wd5",
        ),
    ]


def _mock_mixed_boards() -> None:
    board = "https://boards-api.greenhouse.io/v1/boards"
    tagged = {"ETag": '"v1"'}
    respx.get(f"{board}/acme/jobs").mock(
        side_effect=lambda request: (
            httpx.Response(304)
            if request.headers.get("if-none-match") == '"v1"'
            else httpx.Response(200, json=_payload(), headers=tagged)
        )
    )
    respx.get(f"{board}/globex/jobs").mock(return_value=httpx.Response(404, json={}))
    broken = _payload()
    broken["jobs"].append({"title": "No id"})
    respx.get(f"{board}/initech/jobs").mock(return_value=httpx.Response(200, json=broken))
    respx.get(f"{board}/wayne/jobs").mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get("https://api.lever.co/v0/postings/hooli").mock(return_value=httpx.Response(403))
    for slug in ("pied", "stark"):
        respx.get(f"https://{slug}.bamboohr.com/careers/list").mock(
            return_value=httpx.Response(200, json={"result": []})
        )
    respx.post(WORKDAY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "facets": [],
                "jobPostings": [
                    {
                        "title": "Software Developer Intern",
                        "externalPath": "/job/Montreal/Software-Developer-Intern_R1",
                        "locationsText": "Montreal, QC",
                        "postedOn": "Posted Today",
                        "bulletFields": ["R1"],
                    }
                ],
            },
        )
    )
    respx.get(
        f"{WORKDAY_URL.removesuffix('/jobs')}/job/Montreal/Software-Developer-Intern_R1"
    ).mock(
        return_value=httpx.Response(
            200, json={"jobPostingInfo": {"jobDescription": "<p>Build tools.</p>"}}
        )
    )
    respx.get(SIMPLIFY_URL).mock(
        side_effect=lambda request: (
            httpx.Response(304)
            if request.headers.get("if-none-match") == '"s1"'
            else httpx.Response(
                200,
                headers={"ETag": '"s1"'},
                json=[
                    {
                        "id": "s1",
                        "company_name": "Acme",
                        "title": "Software Engineer Intern",
                        "url": "https://simplify.jobs/p/s1",
                        "locations": ["Toronto, ON"],
                    }
                ],
            )
        )
    )


async def _seed_rest_and_block(repository: AsyncRepository) -> None:
    from stage.domain import CompanyVisit, RateState
    from stage.storage import SourceBatch

    for hours in (3, 2, 1):
        await repository.apply_source_batch(
            SourceBatch(
                source="greenhouse",
                run_started_at=WHEN - timedelta(hours=hours),
                visits=(CompanyVisit(board="greenhouse:wayne", succeeded=False, error="x"),),
            )
        )
    await repository.apply_source_batch(
        SourceBatch(
            source="bamboohr",
            run_started_at=WHEN - timedelta(minutes=1),
            rate_state=(
                RateState(
                    bucket="pied.bamboohr.com",
                    updated_at=WHEN - timedelta(minutes=1),
                    blocked_until=WHEN + timedelta(hours=2),
                    consecutive_failures=1,
                    reason="HTTP 403",
                ),
            ),
        )
    )


async def _mixed_stream(
    repository: AsyncRepository, when: datetime, *, dry_run: bool = False
) -> list[str]:
    import random

    events = [
        event
        async for event in sync(
            repository,
            _mixed_registry(),
            sources=["greenhouse", "lever", "bamboohr", "workday", "simplify"],
            now_fn=lambda: when,
            rng=random.Random(7),
            dry_run=dry_run,
        )
    ]
    return _canonical(events)


@respx.mock
async def test_a_mixed_run_streams_the_same_events_every_time(tmp_path: Path) -> None:
    _mock_mixed_boards()
    async with open_repository(tmp_path / "mixed.db") as repository:
        await _seed_rest_and_block(repository)
        first = await _mixed_stream(repository, WHEN)
        rehearsal = await _mixed_stream(repository, LATER, dry_run=True)
        second = await _mixed_stream(repository, LATER)
        third = await _mixed_stream(repository, LATEST)

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert first == golden["first"]
    assert rehearsal == golden["rehearsal"]
    assert second == golden["second"]
    assert third == golden["third"]
