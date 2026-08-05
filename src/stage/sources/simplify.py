
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)
from stage.sources.feed import register_feed, upcoming_season_year

HOST = "raw.githubusercontent.com"
LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer{year}-Internships/dev/.github/"
    "scripts/listings.json"
)


class SimplifyListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    company_name: str
    title: str
    url: str = ""
    locations: list[str] = Field(default_factory=list)
    active: bool = True
    is_visible: bool = True
    date_posted: int | None = None
    terms: list[str] = Field(default_factory=list)
    sponsorship: str = ""
    degrees: list[str] = Field(default_factory=list)
    category: str = ""


@register_feed
class SimplifyFeed:
    name: ClassVar[str] = "simplify"
    rate_profile: ClassVar[str] = "feeds"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""

    def season_year(self, now: datetime) -> int:
        return upcoming_season_year(now)

    def plan(self, now: datetime) -> tuple[str, ...]:
        return (LISTINGS_URL.format(year=self.season_year(now)),)

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        response = await client.get_json(self.plan(now)[0])
        if response.not_modified:
            return FetchResult(not_modified=True)
        listings, dropped = self._validate(response.payload, now)
        return FetchResult(
            jobs=tuple(
                self._to_job(listing, now)
                for listing in listings
                if listing.active and listing.is_visible
            ),
            degraded=malformed_note(dropped),
            authoritative=not dropped,
        )

    def _validate(self, payload: Any, now: datetime) -> tuple[list[SimplifyListing], int]:
        if not isinstance(payload, list):
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"simplify/{self.season_year(now)}: field '<root>' failed validation "
                f"(expected a JSON list of listings); raw payload captured at {captured}"
            )
        return validate_rows(
            SimplifyListing, payload, source=self.name, slug=str(self.season_year(now))
        )

    def _to_job(self, listing: SimplifyListing, now: datetime) -> Job:
        posted = (
            datetime.fromtimestamp(listing.date_posted, tz=now.tzinfo)
            if listing.date_posted
            else None
        )
        return Job(
            id=job_id(self.name, listing.company_name, listing.id),
            source=self.name,
            company=collapse_whitespace(listing.company_name),
            title_raw=listing.title,
            title_normalized=collapse_whitespace(listing.title),
            apply_url_raw=listing.url,
            description=strip_html(""),
            location_raw=collapse_whitespace(" / ".join(listing.locations)),
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
            signals=SourceSignals(
                terms=tuple(listing.terms),
                sponsorship=listing.sponsorship,
                degrees=tuple(listing.degrees),
                category=listing.category,
            ),
        )
