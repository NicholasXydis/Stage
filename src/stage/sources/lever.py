
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Company, Job, Platform, board_key, job_id
from stage.http import HttpClient
from stage.sources import register
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    FetchResult,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)

BASE_URL = "https://api.lever.co/v0/postings/{slug}"
HOST = "api.lever.co"


class LeverCategories(BaseModel):
    model_config = ConfigDict(extra="ignore")

    location: str = ""
    allLocations: list[str] = Field(default_factory=list)  # noqa: N815 - the API's field name
    commitment: str = ""
    team: str = ""
    department: str = ""

    def label(self) -> str:
        if self.allLocations:
            return " / ".join(dict.fromkeys(self.allLocations))
        return self.location


class LeverPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    hostedUrl: str = ""  # noqa: N815 - the API's field name
    applyUrl: str = ""  # noqa: N815 - the API's field name
    createdAt: int | None = None  # noqa: N815 - the API's field name
    categories: LeverCategories | None = None
    descriptionPlain: str = ""  # noqa: N815 - the API's field name
    additionalPlain: str = ""  # noqa: N815 - the API's field name


@register
class LeverAdapter:
    name: ClassVar[str] = "lever"
    platform: ClassVar[Platform] = Platform.LEVER
    rate_profile: ClassVar[str] = "standard"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""
    detail_budget: ClassVar[int] = 0
    rotation_slice: ClassVar[int] = 0

    max_requests_per_company: ClassVar[int] = 1

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:  # noqa: ARG002
        return self.hosts

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def plan(self, company: Company) -> tuple[str, ...]:
        return (f"{BASE_URL.format(slug=company.slug)}?mode=json",)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,  # noqa: ARG002
        details: Sequence[str] = (),  # noqa: ARG002
    ) -> FetchResult:
        response = await client.get_json(
            BASE_URL.format(slug=company.slug), params={"mode": "json"}
        )
        if response.not_modified:
            return FetchResult(not_modified=True)
        postings, dropped = self._validate(company, response.payload)
        return FetchResult(
            jobs=tuple(self._to_job(company, posting, now) for posting in postings),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[LeverPosting], int]:
        if not isinstance(payload, list):
            captured = capture_payload(self.name, company.slug, payload)
            raise PayloadValidationError(
                f"lever/{company.slug}: field '<root>' failed validation (expected a JSON "
                f"list of postings); raw payload captured at {captured}"
            )
        return validate_rows(LeverPosting, payload, source=self.name, slug=company.slug)

    def _to_job(self, company: Company, posting: LeverPosting, now: datetime) -> Job:
        posted = (
            datetime.fromtimestamp(posting.createdAt / 1000, tz=UTC)
            if posting.createdAt
            else None
        )
        parts = (posting.descriptionPlain, posting.additionalPlain)
        body = "\n\n".join(part for part in parts if part)
        return Job(
            id=job_id(self.name, company.slug, posting.id),
            source=self.name,
            company=company.name,
            title_raw=posting.text,
            title_normalized=collapse_whitespace(posting.text),
            apply_url_raw=posting.hostedUrl or posting.applyUrl,
            description=body,
            location_raw=collapse_whitespace(
                posting.categories.label() if posting.categories else ""
            ),
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
        )
