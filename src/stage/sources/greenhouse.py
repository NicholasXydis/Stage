from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, job_id
from stage.http import HttpClient, ResponseTooLargeError
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import BoardAdapter, FetchResult, malformed_note

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
HOST = "boards-api.greenhouse.io"


class GreenhouseLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = ""


class GreenhouseJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    absolute_url: str
    updated_at: datetime | None = None
    location: GreenhouseLocation | None = None
    content: str = ""


class GreenhouseBoard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[Any]


@register
class GreenhouseAdapter(BoardAdapter):
    name: ClassVar[str] = "greenhouse"
    platform: ClassVar[Platform] = Platform.GREENHOUSE
    rate_profile: ClassVar[str] = "standard"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 2

    base_url: ClassVar[str] = BASE_URL
    query: ClassVar[tuple[tuple[str, str], ...]] = (("content", "true"),)
    root_model: ClassVar[type[BaseModel] | None] = GreenhouseBoard
    rows_field: ClassVar[str] = "jobs"
    row_model: ClassVar[type[BaseModel]] = GreenhouseJob

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        url = self.url_for(company)
        try:
            response = await client.get_json(url, params={"content": "true"})
        except ResponseTooLargeError:
            return await self._without_descriptions(company, client, now, url)
        if response.not_modified:
            return FetchResult(not_modified=True)
        return self.result(company, response.payload, now)

    async def _without_descriptions(
        self, company: Company, client: HttpClient, now: datetime, url: str
    ) -> FetchResult:
        response = await client.get_json(url, params={"content": "false"})
        if response.not_modified:
            return FetchResult(not_modified=True)
        postings, dropped = self.validate(company, response.payload)
        notes = ["board exceeds the response cap with content=true; fetched without descriptions"]
        if dropped:
            notes.append(malformed_note(dropped))
        return FetchResult(
            jobs=tuple(self.to_job(company, posting, now) for posting in postings),
            degraded="; ".join(notes),
            authoritative=not dropped,
            stale_urls=(f"{url}?content=false",),
        )

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        return Job(
            id=job_id(self.name, company.slug, str(row.id)),
            source=self.name,
            company=company.name,
            title_raw=row.title,
            title_normalized=collapse_whitespace(row.title),
            apply_url_raw=row.absolute_url,
            description=strip_html(row.content),
            location_raw=collapse_whitespace(
                row.location.name if row.location and row.location.name else ""
            ),
            first_seen=now,
            last_seen=now,
            source_posted_at=row.updated_at,
        )
