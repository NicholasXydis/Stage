from collections.abc import Sequence
from datetime import UTC, datetime
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
from stage.sources.platforms import SlugRejectedError, safe_slug

HOST_TEMPLATE = "{slug}.recruitee.com"
PATH = "/api/offers/"


class RecruiteeOffer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    slug: NullableStr = ""
    description: NullableStr = ""
    requirements: NullableStr = ""
    location: NullableStr = ""
    city: NullableStr = ""
    country_code: NullableStr = ""
    department: NullableStr = ""
    employment_type_code: NullableStr = ""
    careers_url: NullableStr = ""
    careers_apply_url: NullableStr = ""
    remote: NullableBool = False
    published_at: NullableStr = ""

    def where(self) -> str:
        if self.location:
            return self.location
        parts = [part for part in (self.city, self.country_code) if part]
        if not parts and self.remote:
            return "Remote"
        return ", ".join(parts)

    def posted(self) -> datetime | None:
        raw = self.published_at.strip()
        if not raw:
            return None
        for pattern in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, pattern).replace(tzinfo=UTC)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def body(self) -> str:
        joined = "\n\n".join(part for part in (self.description, self.requirements) if part)
        return collapse_whitespace(strip_html(joined))


class RecruiteeBoard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    offers: list[Any]


@register
class RecruiteeAdapter:
    name: ClassVar[str] = "recruitee"
    platform: ClassVar[Platform] = Platform.RECRUITEE
    rate_profile: ClassVar[str] = "moderate"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = "recruitee"
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
        offers, dropped = self._validate(company, response.payload)
        return FetchResult(
            jobs=tuple(self._to_job(company, offer, now) for offer in offers),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[RecruiteeOffer], int]:
        try:
            board = RecruiteeBoard.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"recruitee/{company.slug}: field {field!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc
        return validate_rows(RecruiteeOffer, board.offers, source=self.name, slug=company.slug)

    def _to_job(self, company: Company, offer: RecruiteeOffer, now: datetime) -> Job:
        title = collapse_whitespace(offer.title)
        return Job(
            id=job_id(self.name, company.slug, str(offer.id)),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=offer.careers_url or offer.careers_apply_url,
            description=offer.body(),
            location_raw=collapse_whitespace(offer.where()),
            first_seen=now,
            last_seen=now,
            source_posted_at=offer.posted(),
        )
