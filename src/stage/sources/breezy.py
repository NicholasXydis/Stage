from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, board_key, job_id
from stage.http import HttpClient
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    NullableBool,
    NullableStr,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)
from stage.sources.platforms import SlugRejectedError, safe_slug

HOST_TEMPLATE = "{slug}.breezy.hr"
PATH = "/json"


class BreezyCountry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: NullableStr = ""


class BreezyLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: NullableStr = ""
    city: NullableStr = ""
    country: BreezyCountry | None = None
    is_remote: NullableBool = False

    def label(self) -> str:
        if self.name:
            return self.name
        parts = [self.city, self.country.name if self.country else ""]
        seen = [part for part in parts if part]
        if not seen and self.is_remote:
            return "Remote"
        return ", ".join(seen)


class BreezyType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""


class BreezyPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    friendly_id: NullableStr = ""
    type: BreezyType | None = None
    education: NullableStr = ""
    department: NullableStr = ""
    description: NullableStr = ""
    location: BreezyLocation | None = None
    url: NullableStr = ""
    published_date: datetime | None = None

    def where(self) -> str:
        return self.location.label() if self.location else ""


@register
class BreezyAdapter:
    name: ClassVar[str] = "breezy"
    platform: ClassVar[Platform] = Platform.BREEZY
    rate_profile: ClassVar[str] = "moderate"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = "breezy"
    detail_budget: ClassVar[int] = 0
    rotation_slice: ClassVar[int] = 0

    max_requests_per_company: ClassVar[int] = 1

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        allowed: set[str] = set()
        for company in companies:
            try:
                allowed.add(HOST_TEMPLATE.format(slug=safe_slug(company.slug)))
            except SlugRejectedError:
                continue
        return frozenset(allowed)

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def plan(self, company: Company) -> tuple[str, ...]:
        host = HOST_TEMPLATE.format(slug=safe_slug(company.slug))
        return (f"https://{host}{PATH}",)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,  # noqa: ARG002
        details: Sequence[str] = (),  # noqa: ARG002
    ) -> FetchResult:
        response = await client.get_json(self.plan(company)[0])
        if response.not_modified:
            return FetchResult(not_modified=True)
        postings, dropped = self._validate(company, response.payload)
        return FetchResult(
            jobs=tuple(self._to_job(company, posting, now) for posting in postings),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[BreezyPosting], int]:
        if not isinstance(payload, list):
            captured = capture_payload(self.name, company.slug, payload)
            raise PayloadValidationError(
                f"breezy/{company.slug}: field '<root>' failed validation (expected a JSON "
                f"list of postings); raw payload captured at {captured}"
            )
        return validate_rows(BreezyPosting, payload, source=self.name, slug=company.slug)

    def _to_job(self, company: Company, posting: BreezyPosting, now: datetime) -> Job:
        title = collapse_whitespace(posting.name)
        return Job(
            id=job_id(self.name, company.slug, posting.id),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=posting.url,
            description=collapse_whitespace(strip_html(posting.description)),
            location_raw=collapse_whitespace(posting.where()),
            first_seen=now,
            last_seen=now,
            source_posted_at=posting.published_date,
        )
