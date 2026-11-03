import re
from datetime import datetime, timedelta
from typing import ClassVar
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient, TextResponse
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    NonEmptyStr,
    capture_payload,
    convert_rows,
    payload_error,
    read_files,
    validate_rows,
)
from stage.sources.feed import register_feed, upcoming_season_year

HOST = "raw.githubusercontent.com"
CONTENT_URL = "https://raw.githubusercontent.com/speedyapply/{repository}/main/{path}"
HREF = re.compile(r'<a\s+href="(?P<url>[^"]+)"', re.IGNORECASE)
STRONG = re.compile(r"<strong>(?P<text>.*?)</strong>", re.IGNORECASE)
AGE = re.compile(r"(?P<days>\d+)d")
WORKDAY_PLACE = re.compile(
    r"https?://[^/]+\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?[^/]+"
    r"/job/(?P<place>[^/?#]+)/[^/?#]+",
    re.IGNORECASE,
)


class SpeedyApplyListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: NonEmptyStr
    title: NonEmptyStr
    location: str = ""
    url: NonEmptyStr
    age: str = ""

    @property
    def identity(self) -> str:
        return f"{self.company}:{self.title}:{self.location}:{self.url}"


@register_feed
class SpeedyApplyFeed:
    name: ClassVar[str] = "speedyapply"
    rate_profile: ClassVar[str] = "feeds"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""

    def season_year(self, now: datetime) -> int:
        return upcoming_season_year(now)

    def plan(self, now: datetime) -> tuple[str, ...]:
        year = self.season_year(now)
        sources = (
            (f"{year}-SWE-College-Jobs", "README.md"),
            (f"{year}-SWE-College-Jobs", "INTERN_INTL.md"),
            (f"{year}-AI-College-Jobs", "README.md"),
            (f"{year}-AI-College-Jobs", "INTERN_INTL.md"),
        )
        return tuple(
            CONTENT_URL.format(repository=repository, path=path) for repository, path in sources
        )

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        def parse(url: str, response: TextResponse) -> tuple[list[Job], int]:
            listings, dropped = self._validate(response.text, url, now)
            converted, unconvertible = convert_rows(
                lambda listing: self._to_job(listing, now),
                listings,
                source=self.name,
                slug=str(self.season_year(now)),
            )
            return converted, dropped + unconvertible

        return await read_files(self.name, self.plan(now), client.get_text, parse, unique=True)

    def _validate(self, text: str, url: str, now: datetime) -> tuple[list[SpeedyApplyListing], int]:
        rows, malformed = self._rows(text, str(self.season_year(now)))
        if not rows:
            raise payload_error(
                self.name,
                str(self.season_year(now)),
                {"text": text},
                f"{self.name} has no internship table rows at {url}",
            )
        listings, dropped = validate_rows(
            SpeedyApplyListing, rows, source=self.name, slug=str(self.season_year(now))
        )
        return listings, malformed + dropped

    def _rows(self, text: str, slug: str) -> tuple[list[dict[str, str]], int]:
        rows: list[dict[str, str]] = []
        malformed = 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not cells or not cells[0].lower().startswith("<a href="):
                continue
            if len(cells) == 6:
                apply_cell, age_cell = cells[4], cells[5]
            elif len(cells) == 5:
                apply_cell, age_cell = cells[3], cells[4]
            else:
                malformed += 1
                capture_payload(f"{self.name}-posting", slug, {"line": line})
                continue
            company = STRONG.search(cells[0])
            apply = HREF.search(apply_cell)
            if company is None or apply is None:
                malformed += 1
                capture_payload(f"{self.name}-posting", slug, {"line": line})
                continue
            rows.append(
                {
                    "company": collapse_whitespace(strip_html(company.group("text"))),
                    "title": collapse_whitespace(strip_html(cells[1])),
                    "location": collapse_whitespace(strip_html(cells[2])),
                    "url": apply.group("url"),
                    "age": collapse_whitespace(strip_html(age_cell)),
                }
            )
        return rows, malformed

    def _to_job(self, listing: SpeedyApplyListing, now: datetime) -> Job:
        age = AGE.fullmatch(listing.age)
        posted = now - timedelta(days=int(age.group("days"))) if age is not None else None
        return Job(
            id=job_id(self.name, listing.company, listing.identity),
            source=self.name,
            company=collapse_whitespace(listing.company),
            title_raw=collapse_whitespace(listing.title),
            title_normalized=collapse_whitespace(listing.title),
            apply_url_raw=listing.url,
            description="",
            location_raw=collapse_whitespace(listing.location) or _place_in_link(listing.url),
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
            signals=SourceSignals(employment_type="internship"),
        )


def _place_in_link(url: str) -> str:
    match = WORKDAY_PLACE.match(url)
    return collapse_whitespace(unquote(match["place"])) if match is not None else ""
