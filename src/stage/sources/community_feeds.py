from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient, JsonResponse
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    FetchResult,
    NonEmptyStr,
    convert_rows,
    payload_error,
    read_files,
    validate_rows,
)
from stage.sources.feed import register_feed, upcoming_season_year

HOST = "raw.githubusercontent.com"


class CommunityListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    company_name: NonEmptyStr
    title: NonEmptyStr
    url: str = ""
    locations: list[str] = Field(default_factory=list)
    active: bool = True
    is_visible: bool = True
    date_posted: int | None = None
    season: str = ""
    sponsorship: str = ""

    @property
    def identity(self) -> str:
        return self.id or f"{self.company_name}:{self.title}:{self.url}"


class _CommunityFeed:
    name: ClassVar[str] = ""
    rate_profile: ClassVar[str] = "feeds"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""
    templates: ClassVar[tuple[str, ...]] = ()

    def season_year(self, now: datetime) -> int:
        return upcoming_season_year(now)

    def plan(self, now: datetime) -> tuple[str, ...]:
        year = self.season_year(now)
        return tuple(template.format(year=year) for template in self.templates)

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        def parse(url: str, response: JsonResponse) -> tuple[list[Job], int]:
            listings, dropped = self._validate(response.payload, url, now)
            converted, unconvertible = convert_rows(
                lambda listing: self._to_job(listing, now),
                [listing for listing in listings if listing.active and listing.is_visible],
                source=self.name,
                slug=str(self.season_year(now)),
            )
            return converted, dropped + unconvertible

        return await read_files(self.name, self.plan(now), client.get_json, parse)

    def _validate(
        self, payload: Any, url: str, now: datetime
    ) -> tuple[list[CommunityListing], int]:
        rows = payload.get("listings") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise payload_error(
                self.name,
                str(self.season_year(now)),
                payload,
                f"{self.name} answered with something other than a JSON list of listings at {url}",
            )
        return validate_rows(
            CommunityListing, rows, source=self.name, slug=str(self.season_year(now))
        )

    def _to_job(self, listing: CommunityListing, now: datetime) -> Job:
        posted = (
            datetime.fromtimestamp(listing.date_posted, tz=now.tzinfo)
            if listing.date_posted
            else None
        )
        return Job(
            id=job_id(self.name, listing.company_name, listing.identity),
            source=self.name,
            company=collapse_whitespace(listing.company_name),
            title_raw=listing.title,
            title_normalized=collapse_whitespace(listing.title),
            apply_url_raw=listing.url,
            description="",
            location_raw=collapse_whitespace(" / ".join(listing.locations)),
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
            signals=SourceSignals(
                season=listing.season,
                sponsorship=listing.sponsorship,
            ),
        )


@register_feed
class VanshFeed(_CommunityFeed):
    name: ClassVar[str] = "vanshb03"
    templates: ClassVar[tuple[str, ...]] = (
        "https://raw.githubusercontent.com/vanshb03/Summer{year}-Internships/dev/"
        ".github/scripts/listings.json",
    )
