from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.domain import Company, Platform
from stage.http import HttpClient, profile
from stage.sources import get_adapter
from stage.sources.base import PayloadValidationError

ENDPOINT = "https://api.lever.co/v0/postings/acme"


@pytest.fixture
def acme_lever() -> Company:
    return Company(name="Acme", platform=Platform.LEVER, slug="acme")


def _client() -> HttpClient:
    adapter = get_adapter("lever")
    return HttpClient(
        allowed_hosts=adapter.hosts, posture=profile(adapter.rate_profile), jitter=False
    )


def _posting(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "abc-123",
        "text": "Stagiaire en génie logiciel",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
        "createdAt": 1782396857137,
        "categories": {
            "location": "Montreal, QC",
            "allLocations": ["Montreal, QC", "Toronto, ON"],
            "commitment": "Permanent Full-Time | Permanent temps-plein",
        },
        "descriptionPlain": "Opening and body text.",
        "additionalPlain": "Duration: 4 months. Must be enrolled.",
    }
    base.update(overrides)
    return base


@respx.mock
async def test_fetch_maps_a_posting_with_its_full_description(
    acme_lever: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[_posting()]))
    adapter = get_adapter("lever")
    async with _client() as client:
        result = await adapter.fetch(acme_lever, client, run_time)

    job = result.jobs[0]
    assert job.title_raw == "Stagiaire en génie logiciel"
    assert "Duration: 4 months" in job.description
    assert "Opening and body text." in job.description
    assert job.apply_url_raw == "https://jobs.lever.co/acme/abc-123"


@respx.mock
async def test_epoch_milliseconds_become_a_real_timestamp(
    acme_lever: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[_posting()]))
    adapter = get_adapter("lever")
    async with _client() as client:
        result = await adapter.fetch(acme_lever, client, run_time)

    posted = result.jobs[0].source_posted_at
    assert posted is not None
    assert posted.tzinfo is not None
    assert posted == datetime.fromtimestamp(1782396857137 / 1000, tz=UTC)
    assert result.jobs[0].first_seen == run_time


@respx.mock
async def test_every_location_survives_rather_than_the_first(
    acme_lever: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[_posting()]))
    adapter = get_adapter("lever")
    async with _client() as client:
        result = await adapter.fetch(acme_lever, client, run_time)

    assert result.jobs[0].location_raw == "Montreal, QC / Toronto, ON"


@respx.mock
async def test_a_posting_without_categories_still_maps(
    acme_lever: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=[_posting(categories=None, createdAt=None)])
    )
    adapter = get_adapter("lever")
    async with _client() as client:
        result = await adapter.fetch(acme_lever, client, run_time)

    assert result.jobs[0].location_raw == ""
    assert result.jobs[0].source_posted_at is None


@respx.mock
async def test_one_bad_row_is_dropped_and_costs_the_board_its_authority(
    acme_lever: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=[{"id": "abc", "text": "Intern"}, {"text": "no id"}])
    )
    adapter = get_adapter("lever")
    async with _client() as client:
        result = await adapter.fetch(acme_lever, client, run_time)

    assert len(result.jobs) == 1
    assert "1 posting(s) failed validation" in result.degraded
    assert not result.authoritative


@respx.mock
async def test_a_non_list_payload_is_drift_not_an_empty_board(
    acme_lever: Company, run_time: datetime
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"postings": []}))
    adapter = get_adapter("lever")
    async with _client() as client:
        with pytest.raises(PayloadValidationError, match="expected a JSON list"):
            await adapter.fetch(acme_lever, client, run_time)
