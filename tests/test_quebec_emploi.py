from datetime import UTC, datetime

import httpx
import pytest
import respx

from stage.http import HttpClient, profile
from stage.sources import get_feeds
from stage.sources.quebec_emploi import QuebecEmploiFeed

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _payload(rows: list[dict[str, object]], total: int) -> dict[str, object]:
    return {"items": rows, "meta": {"total_hits": total}}


def _listing(identifier: int, title: str) -> dict[str, object]:
    return {
        "ide_affch": identifier,
        "titre": title,
        "employeur": "Acme Québec",
        "nom_ville": "Montréal",
    }


def test_quebec_emploi_registers_with_official_student_stage_filters() -> None:
    feed = QuebecEmploiFeed()
    assert get_feeds()[feed.name] is not None
    request = feed._request(1)
    assert request["langue"] == "fr"
    filters = request["filter"]
    assert isinstance(filters, dict)
    assert filters["offerType"] == ["2", "3"]


@respx.mock
async def test_quebec_emploi_maps_public_student_stage_listings() -> None:
    feed = QuebecEmploiFeed()
    route = respx.post(feed.plan(NOW)[0]).mock(
        return_value=httpx.Response(
            200,
            json=_payload([_listing(544165, "Développeur logiciel")], 1),
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, NOW)

    assert route.called
    job = result.jobs[0]
    assert job.company == "Acme Québec"
    assert job.location_raw == "Montréal, Québec, Canada"
    assert job.apply_url_raw.endswith("/plateforme-emploi/poste/544165")
    assert job.signals.employment_type == "student stage"
    assert result.authoritative


@respx.mock
async def test_quebec_emploi_never_closes_source_after_its_public_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stage.sources.quebec_emploi.PAGE_CAP", 1)
    feed = QuebecEmploiFeed()
    respx.post(feed.plan(NOW)[0]).mock(
        return_value=httpx.Response(200, json=_payload([_listing(544165, "Développeur")], 2))
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, NOW)

    assert not result.authoritative
    assert "page public-search cap" in result.degraded
