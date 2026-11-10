from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient
from stage.sources import register_feed
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    NonEmptyStr,
    malformed_note,
    parsed,
    sentences,
    validate_rows,
)

HOST = "www.themuse.com"
SEARCH = f"https://{HOST}/api/public/jobs"
LOCATIONS = (
    "Toronto, Canada",
    "Montreal, Canada",
    "Vancouver, Canada",
    "Ottawa, Canada",
    "Calgary, Canada",
    "Edmonton, Canada",
    "Waterloo, Canada",
    "Quebec City, Canada",
    "Halifax, Canada",
    "Winnipeg, Canada",
    "New York City, NY",
    "San Francisco, CA",
)
PAGE_CAP = 8


class MuseCompany(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""


class MuseLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""


class MuseRefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    landing_page: str = ""


class MuseListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)
    name: NonEmptyStr
    company: MuseCompany | None = None
    locations: list[MuseLocation] = Field(default_factory=list)
    refs: MuseRefs | None = None
    contents: str = ""
    categories: list[Any] = Field(default_factory=list)


class MusePage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[Any]
    page_count: int = Field(ge=0)


@register_feed
class TheMuseFeed:
    name: ClassVar[str] = "themuse"
    rate_profile: ClassVar[str] = "searches"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = "themuse"
    paused: ClassVar[str] = (
        "The Muse's job API now answers 404 or a bot check, so a sync skips it unless "
        "--source names it."
    )

    def season_year(self, now: datetime) -> int:
        return now.year

    def plan(self, now: datetime) -> tuple[str, ...]:
        return (self._url(1),)

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        seen: dict[str, Job] = {}
        malformed = 0
        truncated = False
        pages = PAGE_CAP
        for page in range(1, PAGE_CAP + 1):
            if page > pages:
                break
            response = await client.get_json(self._url(page), revalidate=page > 1)
            if response.not_modified and page == 1:
                return FetchResult(not_modified=True)
            rows, dropped, pages = self._validate(response.payload, page)
            malformed += dropped
            for listing in rows:
                job = self._to_job(listing, now)
                seen.setdefault(job.id, job)
        else:
            truncated = pages > PAGE_CAP

        return FetchResult(
            jobs=tuple(seen.values()),
            authoritative=False,
            degraded=sentences(
                f"Stage stopped at the {PAGE_CAP}-page cap with {pages} pages reported"
                if truncated
                else "",
                malformed_note(malformed),
            ),
        )

    def _url(self, page: int) -> str:
        locations = "&".join(f"location={quote(name)}" for name in LOCATIONS)
        return f"{SEARCH}?page={page}&level={quote('Internship')}&{locations}"

    def _validate(self, payload: Any, page: int) -> tuple[list[MuseListing], int, int]:
        if payload is None:
            return [], 0, page - 1
        found = parsed(MusePage, payload, source=self.name, slug=f"page-{page}", board=self.name)
        rows, dropped = validate_rows(
            MuseListing, found.results, source=self.name, slug=f"page-{page}"
        )
        return rows, dropped, found.page_count

    def _to_job(self, listing: MuseListing, now: datetime) -> Job:
        title = collapse_whitespace(listing.name)
        company = collapse_whitespace(listing.company.name if listing.company else "") or "The Muse"
        places = [collapse_whitespace(place.name) for place in listing.locations if place.name]
        landing = listing.refs.landing_page if listing.refs else ""
        category = ""
        if listing.categories and isinstance(listing.categories[0], dict):
            category = collapse_whitespace(str(listing.categories[0].get("name") or ""))
        return Job(
            id=job_id(self.name, "internships", str(listing.id)),
            source=self.name,
            company=company,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=landing or f"https://{HOST}/jobs/{listing.id}",
            description=strip_html(listing.contents),
            location_raw=" / ".join(places) or "Remote",
            first_seen=now,
            last_seen=now,
            signals=SourceSignals(employment_type="internship", category=category),
        )
