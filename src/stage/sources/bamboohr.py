from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from stage.domain import Company, Job, Platform, board_key, job_id
from stage.http import HttpClient
from stage.sources import register
from stage.sources._text import collapse_whitespace
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

HOST_TEMPLATE = "{slug}.bamboohr.com"
PATH = "/careers/list"


class BambooLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    city: NullableStr = ""
    state: NullableStr = ""
    country: NullableStr = ""

    def label(self) -> str:
        return ", ".join(part for part in (self.city, self.state, self.country) if part)


class BambooPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    jobOpeningName: str  # noqa: N815
    departmentLabel: NullableStr = ""  # noqa: N815
    employmentStatusLabel: NullableStr = ""  # noqa: N815
    location: BambooLocation | None = None
    atsLocation: BambooLocation | None = None  # noqa: N815
    isRemote: NullableBool = False  # noqa: N815

    def where(self) -> str:
        for candidate in (self.location, self.atsLocation):
            if candidate is not None:
                label = candidate.label()
                if label:
                    return label
        return "Remote" if self.isRemote else ""


class BambooBoard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    result: list[Any]


@register
class BambooHrAdapter:
    name: ClassVar[str] = "bamboohr"
    platform: ClassVar[Platform] = Platform.BAMBOOHR
    rate_profile: ClassVar[str] = "moderate"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = "bamboohr"
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

    def _validate(self, company: Company, payload: Any) -> tuple[list[BambooPosting], int]:
        try:
            board = BambooBoard.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"bamboohr/{company.slug}: field {field!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc
        return validate_rows(BambooPosting, board.result, source=self.name, slug=company.slug)

    def _to_job(self, company: Company, posting: BambooPosting, now: datetime) -> Job:
        title = collapse_whitespace(posting.jobOpeningName)
        host = HOST_TEMPLATE.format(slug=safe_slug(company.slug))
        return Job(
            id=job_id(self.name, company.slug, str(posting.id)),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=f"https://{host}/careers/{posting.id}",
            description="",
            location_raw=collapse_whitespace(posting.where()),
            first_seen=now,
            last_seen=now,
        )
