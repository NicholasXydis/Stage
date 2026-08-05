from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

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
from stage.sources.platforms import safe_slug

BASE_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
HOST = "apply.workable.com"


class WorkablePosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shortcode: str
    title: str
    code: NullableStr = ""
    department: NullableStr = ""
    url: NullableStr = ""
    application_url: NullableStr = ""
    shortlink: NullableStr = ""
    country: NullableStr = ""
    city: NullableStr = ""
    state: NullableStr = ""
    employment_type: NullableStr = ""
    telecommuting: NullableBool = False
    published_on: NullableStr = ""
    description: NullableStr = ""
    requirements: NullableStr = ""
    benefits: NullableStr = ""

    def where(self) -> str:
        parts = [part for part in (self.city, self.state, self.country) if part]
        if not parts and self.telecommuting:
            return "Remote"
        return ", ".join(parts)

    def body(self) -> str:
        sections = (self.description, self.requirements, self.benefits)
        joined = "\n\n".join(section for section in sections if section)
        return collapse_whitespace(strip_html(joined))


class WorkableBoard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    jobs: list[Any]


@register
class WorkableAdapter:
    name: ClassVar[str] = "workable"
    platform: ClassVar[Platform] = Platform.WORKABLE
    rate_profile: ClassVar[str] = "conservative"
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
        return (f"{BASE_URL.format(slug=safe_slug(company.slug))}?details=true",)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,  # noqa: ARG002
        details: Sequence[str] = (),  # noqa: ARG002
    ) -> FetchResult:
        response = await client.get_json(
            BASE_URL.format(slug=safe_slug(company.slug)), params={"details": "true"}
        )
        if response.not_modified:
            return FetchResult(not_modified=True)
        postings, dropped = self._validate(company, response.payload)
        return FetchResult(
            jobs=tuple(self._to_job(company, posting, now) for posting in postings),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[WorkablePosting], int]:
        try:
            board = WorkableBoard.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"workable/{company.slug}: field {field!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc
        return validate_rows(
            WorkablePosting, board.jobs, source=self.name, slug=company.slug
        )

    def _to_job(self, company: Company, posting: WorkablePosting, now: datetime) -> Job:
        title = collapse_whitespace(posting.title)
        return Job(
            id=job_id(self.name, company.slug, posting.shortcode),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=posting.url or posting.application_url or posting.shortlink,
            description=posting.body(),
            location_raw=collapse_whitespace(posting.where()),
            first_seen=now,
            last_seen=now,
        )
