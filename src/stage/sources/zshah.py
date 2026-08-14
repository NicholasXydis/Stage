from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    FetchResult,
    NonEmptyStr,
    PayloadValidationError,
    capture_payload,
    validate_rows,
)
from stage.sources.feed import register_feed, upcoming_season_year

URL = "https://zshah101.github.io/Automated-List-Of-Summer-{year}-and-Fall-2026-Tech-Internships/api/jobs.json"
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
        return (URL.format(year=self.season_year(now)),)

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        url = self.plan(now)[0]
        response = await client.get_json(url)
        if response.not_modified:
            return FetchResult(not_modified=True)
        try:
            envelope = ZshahEnvelope.model_validate(response.payload)
        except Exception as exc:
            captured = capture_payload(self.name, str(self.season_year(now)), response.payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: payload envelope failed validation (captured {captured})"
            ) from exc
        listings, malformed = validate_rows(
            ZshahListing, envelope.jobs, source=self.name, slug=str(self.season_year(now))
        )
        internships = [
            listing for listing in listings if listing.program.casefold() == "internship"
        ]
        if not internships:
            captured = capture_payload(self.name, str(self.season_year(now)), response.payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: no internship records were found (captured {captured})"
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
                f"{malformed} malformed posting(s) were dropped; the feed closes nothing"
                if malformed
                else ""
            ),
            authoritative=not malformed,
        )
