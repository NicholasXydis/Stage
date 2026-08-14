from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Company, Job, Platform, job_id
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import BoardAdapter, NullableStr

HOST = "api.collage.co"


class CollagePosition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    location: NullableStr = ""
    descriptionPlain: NullableStr = ""
    applyUrl: NullableStr = ""
    hostedUrl: NullableStr = ""


class CollageBoard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    positions: list[Any] = Field(default_factory=list)


@register
class CollageAdapter(BoardAdapter):
    name: ClassVar[str] = "collage"
    platform: ClassVar[Platform] = Platform.COLLAGE
    rate_profile: ClassVar[str] = "moderate"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = "collage"
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    base_url: ClassVar[str] = "https://api.collage.co/v1/positions/{slug}"
    root_model: ClassVar[type[BaseModel] | None] = CollageBoard
    rows_field: ClassVar[str] = "positions"
    row_model: ClassVar[type[BaseModel]] = CollagePosition

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        apply_url = row.applyUrl or row.hostedUrl
        title = collapse_whitespace(row.title)
        return Job(
            id=job_id(self.name, company.slug, str(row.id)),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=apply_url,
            description=collapse_whitespace(strip_html(row.descriptionPlain)),
            location_raw=collapse_whitespace(row.location),
            first_seen=now,
            last_seen=now,
        )
