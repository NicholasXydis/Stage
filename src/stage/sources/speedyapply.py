import base64
import binascii
import re
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient
from stage.sources._text import collapse_whitespace, strip_html
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

HOST = "api.github.com"
CONTENT_URL = "https://api.github.com/repos/speedyapply/{repository}/contents/{path}?ref=main"
HREF = re.compile(r'<a\s+href="(?P<url>[^"]+)"', re.IGNORECASE)
STRONG = re.compile(r"<strong>(?P<text>.*?)</strong>", re.IGNORECASE)
AGE = re.compile(r"(?P<days>\d+)d")


class GitHubContent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    encoding: NonEmptyStr
    content: NonEmptyStr


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
                listings,
                source=self.name,
                slug=str(self.season_year(now)),
            )
            malformed += dropped + unconvertible
            jobs.extend(converted)
        if failures and not jobs:
            raise PayloadValidationError(f"{self.name}: every file failed — {'; '.join(failures)}")
        if not modified and not failures:
            return FetchResult(not_modified=True)
        unique = {job.id: job for job in jobs}
        notes = []
        if failures:
            notes.append(f"{len(failures)} of {len(self.plan(now))} file(s) unavailable")
        if malformed:
            notes.append(malformed_note(malformed))
        return FetchResult(
            jobs=tuple(unique.values()),
            degraded="; ".join(notes),
            authoritative=not (failures or malformed),
            stale_urls=tuple(stale),
        )

    def _validate(
        self, payload: Any, url: str, now: datetime
    ) -> tuple[list[SpeedyApplyListing], int]:
        try:
            content = GitHubContent.model_validate(payload)
        except Exception as exc:
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: content envelope failed validation (captured {captured})"
            ) from exc
        if content.encoding != "base64":
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: unsupported content encoding {content.encoding!r} "
                f"(captured {captured})"
            )
        try:
            text = base64.b64decode("".join(content.content.split()), validate=True).decode("utf-8")
        except (UnicodeDecodeError, binascii.Error, ValueError) as exc:
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: content is not valid base64 UTF-8 (captured {captured})"
            ) from exc
        rows, malformed = self._rows(text, str(self.season_year(now)))
        if not rows:
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: no internship table rows were found (captured {captured})"
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
            location_raw=collapse_whitespace(listing.location),
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
            signals=SourceSignals(employment_type="internship"),
        )
