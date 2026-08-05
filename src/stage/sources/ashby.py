from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
HOST = "api.ashbyhq.com"


class AshbyLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    locationName: NullableStr = ""  # noqa: N815


class AshbyPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    location: NullableStr = ""
    secondaryLocations: list[AshbyLocation] = Field(default_factory=list)  # noqa: N815
    department: NullableStr = ""
    team: NullableStr = ""
    employmentType: NullableStr = ""  # noqa: N815
    isListed: NullableBool = True  # noqa: N815
    isRemote: NullableBool = False  # noqa: N815
    publishedAt: datetime | None = None  # noqa: N815
    jobUrl: NullableStr = ""  # noqa: N815
    applyUrl: NullableStr = ""  # noqa: N815
    descriptionPlain: NullableStr = ""  # noqa: N815
    descriptionHtml: NullableStr = ""  # noqa: N815

    def where(self) -> str:
        names = [self.location] if self.location else []
        names.extend(entry.locationName for entry in self.secondaryLocations)
        seen = [name for name in dict.fromkeys(names) if name]
        if not seen and self.isRemote:
            return "Remote"
        return " / ".join(seen)

    def body(self) -> str:
        if self.descriptionPlain:
            return collapse_whitespace(self.descriptionPlain)
        return collapse_whitespace(strip_html(self.descriptionHtml))


class AshbyBoard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[Any]


@register
class AshbyAdapter:
    name: ClassVar[str] = "ashby"
    platform: ClassVar[Platform] = Platform.ASHBY
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
        return (BASE_URL.format(slug=safe_slug(company.slug)),)

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
        listed = [posting for posting in postings if posting.isListed]
        return FetchResult(
            jobs=tuple(self._to_job(company, posting, now) for posting in listed),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[AshbyPosting], int]:
        try:
            board = AshbyBoard.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"ashby/{company.slug}: field {field!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc
        return validate_rows(AshbyPosting, board.jobs, source=self.name, slug=company.slug)

    def _to_job(self, company: Company, posting: AshbyPosting, now: datetime) -> Job:
        title = collapse_whitespace(posting.title)
        return Job(
            id=job_id(self.name, company.slug, posting.id),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=posting.jobUrl or posting.applyUrl,
            description=posting.body(),
            location_raw=collapse_whitespace(posting.where()),
            first_seen=now,
            last_seen=now,
            source_posted_at=posting.publishedAt,
        )
