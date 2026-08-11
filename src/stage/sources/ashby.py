from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Company, Job, Platform, SourceSignals, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import BoardAdapter, NullableBool, NullableStr

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
HOST = "api.ashbyhq.com"


class AshbyLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    locationName: NullableStr = ""


class AshbyPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    location: NullableStr = ""
    secondaryLocations: list[AshbyLocation] = Field(default_factory=list)
    department: NullableStr = ""
    team: NullableStr = ""
    employmentType: NullableStr = ""
    isListed: NullableBool = True
    isRemote: NullableBool = False
    publishedAt: datetime | None = None
    jobUrl: NullableStr = ""
    applyUrl: NullableStr = ""
    descriptionPlain: NullableStr = ""
    descriptionHtml: NullableStr = ""

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
class AshbyAdapter(BoardAdapter):
    name: ClassVar[str] = "ashby"
    platform: ClassVar[Platform] = Platform.ASHBY
    rate_profile: ClassVar[str] = "standard"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    base_url: ClassVar[str] = BASE_URL
    root_model: ClassVar[type[BaseModel] | None] = AshbyBoard
    rows_field: ClassVar[str] = "jobs"
    row_model: ClassVar[type[BaseModel]] = AshbyPosting

    def keep(self, row: Any) -> bool:
        return bool(row.isListed)

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        title = collapse_whitespace(row.title)
        return Job(
            id=job_id(self.name, company.slug, row.id),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=row.jobUrl or row.applyUrl,
            description=row.body(),
            location_raw=collapse_whitespace(row.where()),
            first_seen=now,
            last_seen=now,
            source_posted_at=row.publishedAt,
            signals=SourceSignals(employment_type=row.employmentType),
        )
