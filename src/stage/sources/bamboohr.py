from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace
from stage.sources.base import BoardAdapter, NullableBool, NullableStr

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
    jobOpeningName: str
    departmentLabel: NullableStr = ""
    employmentStatusLabel: NullableStr = ""
    location: BambooLocation | None = None
    atsLocation: BambooLocation | None = None
    isRemote: NullableBool = False

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
class BambooHrAdapter(BoardAdapter):
    name: ClassVar[str] = "bamboohr"
    platform: ClassVar[Platform] = Platform.BAMBOOHR
    rate_profile: ClassVar[str] = "moderate"
    bucket_key: ClassVar[str] = "bamboohr"
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    host_template: ClassVar[str] = HOST_TEMPLATE
    path: ClassVar[str] = PATH
    root_model: ClassVar[type[BaseModel] | None] = BambooBoard
    rows_field: ClassVar[str] = "result"
    row_model: ClassVar[type[BaseModel]] = BambooPosting

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        title = collapse_whitespace(row.jobOpeningName)
        return Job(
            id=job_id(self.name, company.slug, str(row.id)),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=f"https://{self.host_for(company)}/careers/{row.id}",
            description="",
            location_raw=collapse_whitespace(row.where()),
            first_seen=now,
            last_seen=now,
        )
