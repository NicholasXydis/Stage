from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, SourceSignals, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import BoardAdapter, NullableBool, NullableStr

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
class WorkableAdapter(BoardAdapter):
    name: ClassVar[str] = "workable"
    platform: ClassVar[Platform] = Platform.WORKABLE
    rate_profile: ClassVar[str] = "conservative"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    base_url: ClassVar[str] = BASE_URL
    query: ClassVar[tuple[tuple[str, str], ...]] = (("details", "true"),)
    root_model: ClassVar[type[BaseModel] | None] = WorkableBoard
    rows_field: ClassVar[str] = "jobs"
    row_model: ClassVar[type[BaseModel]] = WorkablePosting

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        title = collapse_whitespace(row.title)
        return Job(
            id=job_id(self.name, company.slug, row.shortcode),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=row.url or row.application_url or row.shortlink,
            description=row.body(),
            location_raw=collapse_whitespace(row.where()),
            first_seen=now,
            last_seen=now,
            signals=SourceSignals(employment_type=row.employment_type),
        )
