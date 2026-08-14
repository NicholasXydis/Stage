import base64
from datetime import datetime

import httpx
import respx

from stage.http import HttpClient, profile
from stage.sources import get_feeds
from stage.sources.curated_markdown import HanziliFeed, NegarFeed, NorthwesternQuantFeed
from stage.sources.zshah import ZshahFeed


def _content(text: str) -> dict[str, str]:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def test_public_markdown_feeds_register_and_keep_only_current_internship_rows() -> None:
    feeds = get_feeds()
    assert {"negar", "hanzili", "northwestern-quant", "zshah101"} <= set(feeds)

    nested_apply = "[![Apply](https://image)](https://jobs.example/acme)"
    negar, malformed = NegarFeed().rows(
        "| Company | Role | Location | Apply | Date Posted |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Acme | Software Engineer Intern | Montreal, QC | "
        f"{nested_apply} | Aug 1, 2026 |\n"
        "| ↳ | Data Engineer Co-op | Toronto, ON | "
        "[Apply](https://jobs.example/data) | Aug 1, 2026 |\n"
        "| Acme | Closed | Montreal, QC | Closed🔒 | Aug 1, 2026 |\n"
    )
    assert malformed == 0
    assert [(row.company, row.title, row.url) for row in negar] == [
        ("Acme", "Software Engineer Intern", "https://jobs.example/acme"),
        ("Acme", "Data Engineer Co-op", "https://jobs.example/data"),
    ]

    hanzili, malformed = HanziliFeed().rows(
        "| Title | Company | Role | Company Info | Details | Location | Apply |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| Software Developer Intern | Acme | Build APIs | Company | Intern · 4mo | "
        "Montreal, QC | [Apply](<https://jobs.example/acme>) |\n"
        "| Junior Developer | Acme | Maintain systems | Company | Contract · 3mo | "
        "Montreal, QC | [Apply](https://jobs.example/contract) |\n"
    )
    assert malformed == 0
    assert [(row.title, row.description) for row in hanzili] == [
        ("Software Developer Intern", "Build APIs")
    ]

    quant, malformed = NorthwesternQuantFeed().rows(
        "## Quant Firm\n"
        "**Locations**: Chicago, IL\n"
        "|Role|Links|\n"
        "|---|---|\n"
        "|SWE|[✅ C++](https://jobs.example/cpp)|\n"
        "|QR|[✅ PhD](https://jobs.example/phd)|\n"
        "|QR Fellowship|[✅](https://jobs.example/fellowship)|\n"
    )
    assert malformed == 0
    assert [(row.title, row.location) for row in quant] == [
        ("Software Engineer Intern — C++", "Chicago, IL"),
        ("Quantitative Research Intern — PhD", "Chicago, IL"),
    ]


@respx.mock
async def test_zshah_feed_accepts_only_explicit_internships(run_time: datetime) -> None:
    feed = ZshahFeed()
    respx.get(feed.plan(run_time)[0]).mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "one",
                        "company": "Acme",
                        "title": "Software Engineer Intern",
                        "location": "Montreal, QC",
                        "url": "https://jobs.example/one",
                        "program": "Internship",
                    },
                    {
                        "id": "two",
                        "company": "Acme",
                        "title": "Software Engineer",
                        "location": "Montreal, QC",
                        "url": "https://jobs.example/two",
                        "program": "New Grad",
                    },
                ]
            },
        )
    )
    async with HttpClient(
        allowed_hosts=feed.hosts, posture=profile(feed.rate_profile), jitter=False
    ) as client:
        result = await feed.fetch(client, run_time)

    assert [job.title_raw for job in result.jobs] == ["Software Engineer Intern"]
    assert result.jobs[0].signals.employment_type == "internship"
