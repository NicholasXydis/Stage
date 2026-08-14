from datetime import UTC, datetime

import httpx
import respx

from stage.domain import Company, Platform
from stage.http import HttpClient, profile
from stage.sources import adapter_for_platform
from stage.sources.collage import CollageAdapter
from stage.sources.platforms import identify_url

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def test_collage_is_detected_and_registered() -> None:
    assert adapter_for_platform(Platform.COLLAGE) is not None
    candidate = identify_url("https://secure.collage.co/jobs/acme/42/apply")
    assert candidate is not None
    assert (candidate.platform, candidate.slug) == (Platform.COLLAGE, "acme")


@respx.mock
async def test_collage_positions_map_to_jobs() -> None:
    adapter = CollageAdapter()
    company = Company(name="Acme", platform=Platform.COLLAGE, slug="acme")
    respx.get(adapter.plan(company)[0]).mock(
        return_value=httpx.Response(
            200,
            json={
                "positions": [
                    {
                        "id": 42,
                        "title": "Software Engineer Intern",
                        "location": "Montreal, QC",
                        "descriptionPlain": "Build <b>systems</b>",
                        "hostedUrl": "https://secure.collage.co/jobs/acme/42",
                        "applyUrl": "https://secure.collage.co/jobs/acme/42/apply",
                    }
                ]
            },
        )
    )
    async with HttpClient(
        allowed_hosts=adapter.hosts, posture=profile(adapter.rate_profile), jitter=False
    ) as client:
        result = await adapter.fetch(company, client, NOW)

    assert [(job.title_raw, job.apply_url_raw, job.description) for job in result.jobs] == [
        (
            "Software Engineer Intern",
            "https://secure.collage.co/jobs/acme/42/apply",
            "Build systems",
        )
    ]
