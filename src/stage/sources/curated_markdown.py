from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient, HttpStatusError, JsonResponse
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import FetchResult, NonEmptyStr, PayloadValidationError, capture_payload
from stage.sources.feed import register_feed, upcoming_season_year

HOST = "api.github.com"
CONTENT_URL = "https://api.github.com/repos/{repository}/contents/{path}?ref=main"
LINK = re.compile(r"\[([^\]]*)\]\(\s*<?(?P<url>https?://[^)\s<>]+)>?\s*\)")
URL = re.compile(r"\]\(\s*<?(?P<url>https?://[^)\s<>]+)>?\s*\)")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
INTERN = re.compile(r"\b(?:intern(?:ship)?s?|co-?op|stagiaire|stage|alternance|student)\b", re.I)


class GitHubContent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    encoding: NonEmptyStr
    content: NonEmptyStr


@dataclass(frozen=True, slots=True)
class Listing:
    company: str
    title: str
    location: str
    url: str
    description: str = ""


def _plain(value: str) -> str:
    text = COMMENT.sub("", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("`", "")
    return collapse_whitespace(strip_html(text)).strip("| ")


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped[1:-1].split("|")]
    if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells):
        return None
    return cells


def _links(value: str) -> tuple[tuple[str, str], ...]:
    return tuple((match.group(1), match.group("url")) for match in LINK.finditer(value))


def _urls(value: str) -> tuple[str, ...]:
    return tuple(match.group("url") for match in URL.finditer(value))


class _GitHubMarkdownFeed:
    name: ClassVar[str] = ""
    rate_profile: ClassVar[str] = "feeds"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""

    def season_year(self, now: datetime) -> int:
        return upcoming_season_year(now)

    def plan(self, now: datetime) -> tuple[str, ...]:
        raise NotImplementedError

    async def _available(
        self, client: HttpClient, urls: tuple[str, ...]
    ) -> tuple[str, JsonResponse]:
        missing: HttpStatusError | None = None
        for url in urls:
            try:
                return url, await client.get_json(url)
            except HttpStatusError as exc:
                if exc.status != 404:
                    raise
                missing = exc
        if missing is None:
            raise PayloadValidationError(f"{self.name}: no feed url was planned")
        raise missing

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        url, response = await self._available(client, self.plan(now))
        if response.not_modified:
            return FetchResult(not_modified=True)
        text = self._decode(response.payload, url, now)
        listings, malformed = self.rows(text)
        if not listings:
            captured = capture_payload(self.name, str(self.season_year(now)), {"text": text})
            raise PayloadValidationError(
                f"{self.name}/{url}: no current internship rows were found (captured {captured})"
            )
        jobs = tuple(self._to_job(listing, now) for listing in listings)
        return FetchResult(
            jobs=jobs,
            degraded=(
                f"{malformed} malformed Markdown row(s) were skipped; the feed closes nothing"
                if malformed
                else ""
            ),
            authoritative=not malformed,
        )

    def _decode(self, payload: Any, url: str, now: datetime) -> str:
        try:
            content = GitHubContent.model_validate(payload)
        except Exception as exc:
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: GitHub content envelope failed validation "
                f"(captured {captured})"
            ) from exc
        if content.encoding != "base64":
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: unsupported content encoding {content.encoding!r} "
                f"(captured {captured})"
            )
        try:
            return base64.b64decode("".join(content.content.split()), validate=True).decode("utf-8")
        except (UnicodeDecodeError, binascii.Error, ValueError) as exc:
            captured = capture_payload(self.name, str(self.season_year(now)), payload)
            raise PayloadValidationError(
                f"{self.name}/{url}: content is not valid base64 UTF-8 (captured {captured})"
            ) from exc

    def rows(self, text: str) -> tuple[list[Listing], int]:
        raise NotImplementedError

    def _to_job(self, listing: Listing, now: datetime) -> Job:
        return Job(
            id=job_id(
                self.name, listing.company, f"{listing.title}:{listing.location}:{listing.url}"
            ),
            source=self.name,
            company=listing.company,
            title_raw=listing.title,
            title_normalized=listing.title,
            apply_url_raw=listing.url,
            description=listing.description,
            location_raw=listing.location,
            first_seen=now,
            last_seen=now,
            signals=SourceSignals(employment_type="internship"),
        )


@register_feed
class NegarFeed(_GitHubMarkdownFeed):
    name: ClassVar[str] = "negar"

    def plan(self, now: datetime) -> tuple[str, ...]:
        repository = "negarprh/Canadian-Tech-Internships-2026"
        return (
            CONTENT_URL.format(repository=repository, path=f"README-{self.season_year(now)}.md"),
            CONTENT_URL.format(repository=repository, path="README.md"),
        )

    def rows(self, text: str) -> tuple[list[Listing], int]:
        rows: list[Listing] = []
        malformed = 0
        company = ""
        for line in text.splitlines():
            cells = _cells(line)
            if cells is None or len(cells) != 5 or _plain(cells[0]).casefold() == "company":
                continue
            candidate = _plain(cells[0])
            if candidate and candidate != "↳":
                company = candidate
            urls = _urls(cells[3])
            if not company or not urls:
                if cells[3].strip() and "closed" not in cells[3].casefold():
                    malformed += 1
                continue
            rows.append(
                Listing(
                    company=company,
                    title=_plain(cells[1]),
                    location=_plain(cells[2]),
                    url=urls[-1],
                )
            )
        return rows, malformed


@register_feed
class HanziliFeed(_GitHubMarkdownFeed):
    name: ClassVar[str] = "hanzili"

    def plan(self, now: datetime) -> tuple[str, ...]:
        return (
            CONTENT_URL.format(repository="hanzili/canada_sde_intern_position", path="README.md"),
        )

    def rows(self, text: str) -> tuple[list[Listing], int]:
        rows: list[Listing] = []
        malformed = 0
        for line in text.splitlines():
            cells = _cells(line)
            if cells is None or len(cells) != 7 or _plain(cells[0]).casefold() == "title":
                continue
            title, company, description, _, details, location, apply = cells
            if not INTERN.search(f"{title} {details}"):
                continue
            urls = _urls(apply)
            if not urls:
                malformed += 1
                continue
            rows.append(
                Listing(
                    company=_plain(company),
                    title=_plain(title),
                    location=_plain(location),
                    url=urls[-1],
                    description=_plain(description),
                )
            )
        return rows, malformed


@register_feed
class NorthwesternQuantFeed(_GitHubMarkdownFeed):
    name: ClassVar[str] = "northwestern-quant"

    def plan(self, now: datetime) -> tuple[str, ...]:
        return (
            CONTENT_URL.format(
                repository=f"northwesternfintech/{self.season_year(now)}QuantInternships",
                path="README.md",
            ),
        )

    def rows(self, text: str) -> tuple[list[Listing], int]:
        rows: list[Listing] = []
        malformed = 0
        company = ""
        location = ""
        titles = {
            "qd": "Quantitative Developer Intern",
            "qr": "Quantitative Research Intern",
            "qt": "Quantitative Trader Intern",
            "swe": "Software Engineer Intern",
            "ml": "Machine Learning Intern",
            "hw": "Hardware Engineer Intern",
            "fpga": "FPGA Engineer Intern",
            "devops/sre": "DevOps/SRE Intern",
        }
        for line in text.splitlines():
            if line.startswith("## "):
                company = _plain(line[3:])
                location = ""
                continue
            if line.startswith("**Locations**:"):
                location = _plain(line.partition(":")[2])
                continue
            cells = _cells(line)
            if cells is None or len(cells) != 2 or _plain(cells[0]).casefold() == "role":
                continue
            role = _plain(cells[0])
            base = titles.get(role.casefold())
            if not company or base is None or "fellowship" in role.casefold():
                continue
            links = _links(cells[1])
            if not links and cells[1].strip():
                malformed += 1
            for label, url in links:
                qualifier = _plain(label).replace("✅", "").strip()
                title = f"{base} — {qualifier}" if qualifier else base
                rows.append(Listing(company=company, title=title, location=location, url=url))
        return rows, malformed
