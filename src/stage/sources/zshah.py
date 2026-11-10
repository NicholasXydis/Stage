from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Job, SourceSignals, job_id
from stage.domain.text import counted
from stage.http import HttpClient, HttpStatusError
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    FetchResult,
    NonEmptyStr,
    parsed,
    payload_error,
    validate_rows,
)
from stage.sources.feed import register_feed, upcoming_season_year

URL = "https://zshah101.github.io/Automated-List-Of-Summer-{year}-and-Fall-{fall}-Tech-Internships/api/jobs.json"
HOST = "zshah101.github.io"


class ZshahListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: NonEmptyStr
    company: NonEmptyStr
    title: NonEmptyStr
    location: str = ""
    url: NonEmptyStr
    program: str = ""


class ZshahEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[Any] = Field(default_factory=list)


@register_feed
class ZshahFeed:
    name: ClassVar[str] = "zshah101"
    rate_profile: ClassVar[str] = "feeds"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""

    def season_year(self, now: datetime) -> int:
        return upcoming_season_year(now)

    def plan(self, now: datetime) -> tuple[str, ...]:
        year = self.season_year(now)
        return tuple(URL.format(year=named, fall=named - 1) for named in (year, year - 1))

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        *earlier, last = self.plan(now)
        for url in earlier:
            try:
                response = await client.get_json(url)
            except HttpStatusError as exc:
                if exc.status != 404:
                    raise
                continue
            break
        else:
            response = await client.get_json(last)
        if response.not_modified:
            return FetchResult(not_modified=True)
        envelope = parsed(
            ZshahEnvelope,
            response.payload,
            source=self.name,
            slug=str(self.season_year(now)),
            board=self.name,
        )
        listings, malformed = validate_rows(
            ZshahListing, envelope.jobs, source=self.name, slug=str(self.season_year(now))
        )
        internships = [
            listing for listing in listings if listing.program.casefold() == "internship"
        ]
        if not internships:
            raise payload_error(
                self.name,
                str(self.season_year(now)),
                response.payload,
                f"{self.name} has no internship records at {url}",
            )
        jobs = tuple(
            Job(
                id=job_id(self.name, listing.company, listing.id),
                source=self.name,
                company=collapse_whitespace(listing.company),
                title_raw=collapse_whitespace(listing.title),
                title_normalized=collapse_whitespace(listing.title),
                apply_url_raw=listing.url,
                description="",
                location_raw=collapse_whitespace(listing.location),
                first_seen=now,
                last_seen=now,
                signals=SourceSignals(employment_type="internship"),
            )
            for listing in internships
        )
        return FetchResult(
            jobs=jobs,
            degraded=(
                f"Stage dropped {counted(malformed, 'malformed posting')}, so this read closes "
                "nothing."
                if malformed
                else ""
            ),
            authoritative=not malformed,
        )
