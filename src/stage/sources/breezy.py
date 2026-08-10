from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import BoardAdapter, NullableBool, NullableStr

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
class BreezyAdapter(BoardAdapter):
    name: ClassVar[str] = "breezy"
    platform: ClassVar[Platform] = Platform.BREEZY
    rate_profile: ClassVar[str] = "moderate"
    bucket_key: ClassVar[str] = "breezy"
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    host_template: ClassVar[str] = HOST_TEMPLATE
    path: ClassVar[str] = PATH
    row_model: ClassVar[type[BaseModel]] = BreezyPosting

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        title = collapse_whitespace(row.name)
        return Job(
            id=job_id(self.name, company.slug, row.id),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=row.url,
            description=collapse_whitespace(strip_html(row.description)),
            location_raw=collapse_whitespace(row.where()),
            first_seen=now,
            last_seen=now,
            source_posted_at=row.published_date,
        )
