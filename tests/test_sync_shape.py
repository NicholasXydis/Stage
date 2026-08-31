import json
from collections.abc import Sequence
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from stage.domain import Company, Platform, SyncEvent
from stage.services.sync import sync
from stage.storage import AsyncRepository, open_repository

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
WHEN = datetime(2026, 8, 3, 12, tzinfo=UTC)

VOLATILE = frozenset(
    {"elapsed_ms", "started_at", "finished_at", "blocked_until", "remaining_s", "latency_p50_ms"}
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
