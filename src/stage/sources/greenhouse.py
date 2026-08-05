from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from stage.domain import Company, Job, Platform, board_key, job_id
from stage.http import HttpClient, ResponseTooLargeError
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
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
class GreenhouseAdapter:
    name: ClassVar[str] = "greenhouse"
    platform: ClassVar[Platform] = Platform.GREENHOUSE
    rate_profile: ClassVar[str] = "standard"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""
    detail_budget: ClassVar[int] = 0
    rotation_slice: ClassVar[int] = 0

    max_requests_per_company: ClassVar[int] = 2

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:  # noqa: ARG002
        return self.hosts

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def plan(self, company: Company) -> tuple[str, ...]:
        return (f"{BASE_URL.format(token=company.slug)}?content=true",)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,  # noqa: ARG002
        details: Sequence[str] = (),  # noqa: ARG002
    ) -> FetchResult:
        url = BASE_URL.format(token=company.slug)
        try:
            response = await client.get_json(url, params={"content": "true"})
        except ResponseTooLargeError:
            fallback = f"{url}?content=false"
            response = await client.get_json(url, params={"content": "false"})
            if response.not_modified:
                return FetchResult(not_modified=True)
            postings, dropped = self._validate(company, response.payload)
            notes = [
                "board exceeds the response cap with content=true; fetched without "
                "descriptions"
            ]
            if dropped:
                notes.append(malformed_note(dropped))
            return FetchResult(
                jobs=tuple(self._to_job(company, posting, now) for posting in postings),
                degraded="; ".join(notes),
                authoritative=not dropped,
                stale_urls=(fallback,),
            )
        if response.not_modified:
            return FetchResult(not_modified=True)
        postings, dropped = self._validate(company, response.payload)
        return FetchResult(
            jobs=tuple(self._to_job(company, posting, now) for posting in postings),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[GreenhouseJob], int]:
        try:
            board = GreenhouseBoard.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"greenhouse/{company.slug}: field {field!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc
        return validate_rows(
            GreenhouseJob, board.jobs, source=self.name, slug=company.slug
        )

    def _to_job(self, company: Company, posting: GreenhouseJob, now: datetime) -> Job:
        return Job(
            id=job_id(self.name, company.slug, str(posting.id)),
            source=self.name,
            company=company.name,
            title_raw=posting.title,
            title_normalized=collapse_whitespace(posting.title),
            apply_url_raw=posting.absolute_url,
            description=strip_html(posting.content),
            location_raw=collapse_whitespace(
                posting.location.name if posting.location and posting.location.name else ""
            ),
            first_seen=now,
            last_seen=now,
            source_posted_at=posting.updated_at,
        )
