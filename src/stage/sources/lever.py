from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Company, Job, Platform, SourceSignals, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace
from stage.sources.base import BoardAdapter

BASE_URL = "https://api.lever.co/v0/postings/{slug}"
HOST = "api.lever.co"


class LeverCategories(BaseModel):
    model_config = ConfigDict(extra="ignore")

    location: str = ""
    allLocations: list[str] = Field(default_factory=list)
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
    hostedUrl: str = ""
    applyUrl: str = ""
    createdAt: int | None = None
    categories: LeverCategories | None = None
    descriptionPlain: str = ""
    additionalPlain: str = ""


@register
class LeverAdapter(BoardAdapter):
    name: ClassVar[str] = "lever"
    platform: ClassVar[Platform] = Platform.LEVER
    rate_profile: ClassVar[str] = "standard"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    base_url: ClassVar[str] = BASE_URL
    query: ClassVar[tuple[tuple[str, str], ...]] = (("mode", "json"),)
    row_model: ClassVar[type[BaseModel]] = LeverPosting

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        posted = datetime.fromtimestamp(row.createdAt / 1000, tz=UTC) if row.createdAt else None
        parts = (row.descriptionPlain, row.additionalPlain)
        return Job(
            id=job_id(self.name, company.slug, row.id),
            source=self.name,
            company=company.name,
            title_raw=row.text,
            title_normalized=collapse_whitespace(row.text),
            apply_url_raw=row.hostedUrl or row.applyUrl,
            description="\n\n".join(part for part in parts if part),
            location_raw=collapse_whitespace(row.categories.label() if row.categories else ""),
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
            signals=SourceSignals(
                employment_type=row.categories.commitment if row.categories else ""
            ),
        )
