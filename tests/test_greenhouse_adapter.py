import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from stage.domain import Company, Platform
from stage.http import HostNotAllowedError, HttpClient, profile
from stage.paths import capture_dir
from stage.sources import get_adapter, get_adapters
from stage.sources.base import PayloadValidationError

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _client() -> HttpClient:
    adapter = get_adapter("greenhouse")
    return HttpClient(
        allowed_hosts=adapter.hosts, posture=profile(adapter.rate_profile), jitter=False
    )


async def test_adapter_self_registers() -> None:
    assert "greenhouse" in get_adapters()
    assert get_adapter("greenhouse").platform is Platform.GREENHOUSE


@respx.mock
async def test_fetch_maps_payload_to_domain(acme: Company, run_time: datetime) -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
    )
    adapter = get_adapter("greenhouse")

    async with _client() as client:
        jobs = (await adapter.fetch(acme, client, run_time)).jobs

    assert route.called
    assert route.calls[0].request.url.params["content"] == "true"
    assert [job.id for job in jobs] == [
        "greenhouse:acme:4012345",
        "greenhouse:acme:4012346",
        "greenhouse:acme:4012347",
    ]

    first = jobs[0]
    assert first.company == "Acme"
    assert first.title_raw == "Software Engineering Intern, Summer 2027"
    assert first.location_raw == "Montréal, QC"
    assert first.first_seen == run_time
    assert first.last_seen == run_time
    assert first.source_posted_at is not None


@respx.mock
async def test_description_html_is_stripped(acme: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
    )
    async with _client() as client:
        jobs = (await get_adapter("greenhouse").fetch(acme, client, run_time)).jobs

    description = jobs[0].description
    assert "<p>" not in description
    assert "&lt;" not in description
    assert "Join the platform team for a 4-month stage." in description
    assert "\x1b" not in description


@respx.mock
async def test_accents_survive_ingestion(acme: Company, run_time: datetime) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
    )
    async with _client() as client:
        jobs = (await get_adapter("greenhouse").fetch(acme, client, run_time)).jobs

    assert jobs[1].title_raw == "Stagiaire en génie logiciel — Été 2027"
    assert "l'équipe infonuagique" in jobs[1].description


@respx.mock
async def test_a_missing_jobs_key_is_a_shape_change_and_fails_loudly(
    acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"meta": {}}))
    async with _client() as client:
        with pytest.raises(PayloadValidationError) as caught:
            await get_adapter("greenhouse").fetch(acme, client, run_time)

    message = str(caught.value)
    assert "jobs" in message
    captured = Path(message.split("captured at ")[1])
    assert captured.exists()
    captured.unlink()


@respx.mock
async def test_one_bad_row_is_dropped_and_costs_the_board_its_authority(
    acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 1, "title": "Intern", "absolute_url": "https://example.test/1"},
                    {"id": "not-an-int", "title": "x"},
                ]
            },
        )
    )
    async with _client() as client:
        result = await get_adapter("greenhouse").fetch(acme, client, run_time)

    assert len(result.jobs) == 1, "the good row must survive its neighbour"
    assert "1 posting(s) failed validation" in result.degraded
    assert not result.authoritative, "an incomplete listing must close nothing"

    captured = sorted(capture_dir().glob("greenhouse-posting-*.json"))
    assert captured, "the dropped row is captured so a fixture can be built from it"
    for path in captured:
        path.unlink()


async def test_adapter_cannot_reach_a_host_outside_the_registry() -> None:
    async with HttpClient(allowed_hosts=frozenset({"boards-api.greenhouse.io"})) as client:
        with pytest.raises(HostNotAllowedError):
            await client.get_json("https://evil.example.com/v1/boards/acme/jobs")


@respx.mock
async def test_a_non_json_body_stays_loud_on_the_adapter_path(
    acme: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, text="<!doctype html>"))
    adapter = get_adapter("greenhouse")

    async with _client() as client:
        with pytest.raises(ValueError):
            await adapter.fetch(acme, client, run_time)
