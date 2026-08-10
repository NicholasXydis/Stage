from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, SourceSignals, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import BoardAdapter, NullableBool, NullableStr

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
class RecruiteeAdapter(BoardAdapter):
    name: ClassVar[str] = "recruitee"
    platform: ClassVar[Platform] = Platform.RECRUITEE
    rate_profile: ClassVar[str] = "moderate"
    bucket_key: ClassVar[str] = "recruitee"
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    host_template: ClassVar[str] = HOST_TEMPLATE
    path: ClassVar[str] = PATH
    root_model: ClassVar[type[BaseModel] | None] = RecruiteeBoard
    rows_field: ClassVar[str] = "offers"
    row_model: ClassVar[type[BaseModel]] = RecruiteeOffer

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        title = collapse_whitespace(row.title)
        return Job(
            id=job_id(self.name, company.slug, str(row.id)),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=row.careers_url or row.careers_apply_url,
            description=row.body(),
            location_raw=collapse_whitespace(row.where()),
            first_seen=now,
            last_seen=now,
            source_posted_at=row.posted(),
            signals=SourceSignals(employment_type=row.employment_type_code),
        )
