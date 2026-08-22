import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urljoin

from bs4 import Tag
from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, job_id
from stage.http import HttpClient
from stage.sources import register
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    BoardAdapter,
    FetchResult,
    NonEmptyStr,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)
from stage.sources.custom_json import html_rows

HOST = "jobs.jobvite.com"
BASE_URL = "https://jobs.jobvite.com/{slug}/jobs"
LISTING = "table.jv-job-list"
ROW = "table.jv-job-list tbody tr"
NAME_CELL = "td.jv-job-list-name"
LOCATION_CELL = "td.jv-job-list-location"
ONCLICK = re.compile(r"""location\.href\s*=\s*['"](?P<href>[^'"]{1,300})['"]""")


class JobviteRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: NonEmptyStr
    url: NonEmptyStr
    location: str = ""

    def posting_id(self) -> str:
        return self.url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _text(block: Tag, selector: str) -> str:
    found = block.select_one(selector)
    return collapse_whitespace(found.get_text(" ", strip=True)) if found is not None else ""


def _href(block: Tag) -> str:
    link = block.select_one(f"{NAME_CELL} a")
    if link is not None:
        return str(link.get("href", ""))
    found = ONCLICK.search(str(block.get("onclick", "")))
    return found.group("href") if found is not None else ""


def _row(block: Tag) -> dict[str, str]:
    href = _href(block)
    return {
        "title": _text(block, NAME_CELL),
        "url": urljoin(f"https://{HOST}/", href) if href else "",
        "location": _text(block, LOCATION_CELL),
    }


@register
class JobviteAdapter(BoardAdapter):
    name: ClassVar[str] = "jobvite"
    platform: ClassVar[Platform] = Platform.JOBVITE
    rate_profile: ClassVar[str] = "moderate"
    bucket_key: ClassVar[str] = "jobvite"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    base_url: ClassVar[str] = BASE_URL
    row_model: ClassVar[type[BaseModel]] = JobviteRow

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        response = await client.get_text(self.url_for(company))
        if response.not_modified:
            return FetchResult(not_modified=True)
        return self.result(company, response.text, now)

    def result(self, company: Company, payload: Any, now: datetime) -> FetchResult:
        blocks = self._blocks(company, str(payload))
        listed = [block for block in blocks if block.select_one(NAME_CELL) is not None]
        rows, dropped = validate_rows(
            self.row_model, [_row(block) for block in listed], source=self.name, slug=company.slug
        )
        truncated = len(listed) != len(blocks)
        notes = [malformed_note(dropped)] if dropped else []
        if truncated:
            notes.append(
                "the board hides the rest of some categories behind a Show More search page, "
                "so this listing closes nothing"
            )
        return FetchResult(
            jobs=tuple(self.to_job(company, row, now) for row in rows),
            degraded="; ".join(note for note in notes if note),
            authoritative=not dropped and not truncated,
        )

    def _blocks(self, company: Company, text: str) -> list[Tag]:
        if not html_rows(text, LISTING):
            captured = capture_payload(self.name, company.slug, {"text": text})
            raise PayloadValidationError(
                f"{self.name}/{company.slug}: the board page carries no {LISTING!r} listing, "
                f"so this is drift or a retired tenant; raw page captured at {captured}"
            )
        return html_rows(text, ROW)

    def to_job(self, company: Company, row: Any, now: datetime) -> Job:
        return Job(
            id=job_id(self.name, company.slug, row.posting_id()),
            source=self.name,
            company=company.name,
            title_raw=row.title,
            title_normalized=row.title.lower(),
            apply_url_raw=row.url,
            description="",
            location_raw=row.location,
            first_seen=now,
            last_seen=now,
        )
