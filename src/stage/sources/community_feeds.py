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
    convert_rows,
    malformed_note,
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
        jobs: list[Job] = []
        modified = False
        failures: list[str] = []
        stale: list[str] = []
        malformed = 0
        for url in self.plan(now):
            try:
                response = await client.get_json(url)
            except Exception as exc:
                failures.append(f"{url}: {type(exc).__name__}")
                stale.append(url)
                continue
            if response.not_modified:
                continue
            modified = True
            listings, dropped = self._validate(response.payload, url, now)
            converted, unconvertible = convert_rows(
                lambda listing: self._to_job(listing, now),
                [listing for listing in listings if listing.active and listing.is_visible],
                source=self.name,
                slug=str(self.season_year(now)),
            )
            malformed += dropped + unconvertible
            jobs.extend(converted)
        if failures and not jobs:
            raise PayloadValidationError(f"{self.name}: every file failed — {'; '.join(failures)}")
        if not modified and not failures:
            return FetchResult(not_modified=True)
        notes = []
        if failures:
            notes.append(f"{len(failures)} of {len(self.plan(now))} file(s) unavailable")
        if malformed:
            notes.append(malformed_note(malformed))
        return FetchResult(
            jobs=tuple(jobs),
            degraded="; ".join(notes),
            authoritative=not (failures or malformed),
            stale_urls=tuple(stale),
        )

    def _validate(
        self, payload: Any, url: str, now: datetime
    ) -> tuple[list[CommunityListing], int]:
        rows = payload.get("listings") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: payload failed validation (expected a JSON list of "
                f"listings); raw payload captured at {captured}"
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
