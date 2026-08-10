from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from stage.domain import Company, DetailFetch, Job, Platform, board_key, job_id
from stage.http import HttpClient, HttpError
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)
from stage.sources.platforms import safe_slug

BASE_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting}"
HOST = "api.smartrecruiters.com"
PAGE_SIZE = 100
MAX_PAGES = 30


class SmartRecruitersLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    city: str = ""
    region: str = ""
    country: str = ""
    remote: bool = False
    fullLocation: str = ""  # noqa: N815 - the API's own field name

    def label(self) -> str:
        if self.fullLocation:
            return self.fullLocation
        parts = [part for part in (self.city, self.region, self.country) if part]
        return ", ".join(parts) or ("Remote" if self.remote else "")


class SmartRecruitersPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    releasedDate: datetime | None = None  # noqa: N815 - the API's own field name
    location: SmartRecruitersLocation | None = None


class SmartRecruitersPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    totalFound: int  # noqa: N815 - the API's own field name
    content: list[Any]


@register
class SmartRecruitersAdapter:
    name: ClassVar[str] = "smartrecruiters"
    platform: ClassVar[Platform] = Platform.SMARTRECRUITERS
    rate_profile: ClassVar[str] = "moderate"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = ""
    detail_budget: ClassVar[int] = 120
    rotation_slice: ClassVar[int] = 0

    max_requests_per_company: ClassVar[int] = MAX_PAGES

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:  # noqa: ARG002
        return self.hosts

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def plan(self, company: Company) -> tuple[str, ...]:
        return (f"{BASE_URL.format(slug=safe_slug(company.slug))}?limit={PAGE_SIZE}&offset=0",)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,  # noqa: ARG002
        details: Sequence[str] = (),
    ) -> FetchResult:
        url = BASE_URL.format(slug=safe_slug(company.slug))
        postings: list[SmartRecruitersPosting] = []
        truncated = False
        stale_page = False
        malformed = 0

        wanted = {job for job in details if job.startswith(f"{self.board_key(company)}:")}

        for page in range(MAX_PAGES):
            response = await client.get_json(
                url,
                params={"limit": str(PAGE_SIZE), "offset": str(page * PAGE_SIZE)},
                revalidate=bool(wanted),
            )
            if response.not_modified:
                if page == 0:
                    return FetchResult(not_modified=True)
                stale_page = True
                break
            rows, dropped, total = self._validate(company, response.payload)
            malformed += dropped
            if not rows and not dropped:
                break
            postings.extend(rows)
            if len(postings) + malformed >= total:
                break
        else:
            truncated = True

        notes = []
        if truncated:
            notes.append(f"stopped at the {MAX_PAGES}-page cap")
        if stale_page:
            notes.append(
                "a later page answered 304, so the walk ended early on an unchanged page "
                "rather than on the end of the list"
            )
        if malformed:
            notes.append(malformed_note(malformed))
        paired = [(posting, self._to_job(company, posting, now)) for posting in postings]
        fetched: list[DetailFetch] = []
        if wanted:
            paired, fetched = await self._attach_descriptions(company, client, paired, wanted)
        jobs = [job for _, job in paired]

        return FetchResult(
            jobs=tuple(jobs),
            degraded="; ".join(notes),
            authoritative=not (truncated or stale_page or malformed),
            detail_fetches=tuple(fetched),
        )

    async def _attach_descriptions(
        self,
        company: Company,
        client: HttpClient,
        paired: list[tuple[SmartRecruitersPosting, Job]],
        wanted: set[str],
    ) -> tuple[list[tuple[SmartRecruitersPosting, Job]], list[DetailFetch]]:
        outcomes: list[DetailFetch] = []
        merged: list[tuple[SmartRecruitersPosting, Job]] = []
        for posting, job in paired:
            if job.id not in wanted:
                merged.append((posting, job))
                continue
            url = DETAIL_URL.format(slug=safe_slug(company.slug), posting=posting.id)
            try:
                response = await client.get_json(url)
            except HttpError:
                outcomes.append(DetailFetch(id=job.id, resolved=False, failed=True))
                merged.append((posting, job))
                continue
            body = _description_from(response.payload)
            outcomes.append(DetailFetch(id=job.id, resolved=bool(body)))
            merged.append((posting, replace(job, description=body) if body else job))
        return merged, outcomes

    def _validate(
        self, company: Company, payload: Any
    ) -> tuple[list[SmartRecruitersPosting], int, int]:
        page = self._validate_page(company, payload)
        rows, dropped = validate_rows(
            SmartRecruitersPosting, page.content, source=self.name, slug=company.slug
        )
        return rows, dropped, page.totalFound

    def _validate_page(self, company: Company, payload: Any) -> SmartRecruitersPage:
        try:
            return SmartRecruitersPage.model_validate(payload)
        except ValidationError as exc:
            captured = capture_payload(self.name, company.slug, payload)
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "<root>"
            raise PayloadValidationError(
                f"smartrecruiters/{company.slug}: field {field!r} failed validation "
                f"({first['msg']}); raw payload captured at {captured}"
            ) from exc

    def _to_job(self, company: Company, posting: SmartRecruitersPosting, now: datetime) -> Job:
        return Job(
            id=job_id(self.name, company.slug, posting.id),
            source=self.name,
            company=company.name,
            title_raw=posting.name,
            title_normalized=collapse_whitespace(posting.name),
            apply_url_raw=f"https://jobs.smartrecruiters.com/{company.slug}/{posting.id}",
            description="",
            location_raw=collapse_whitespace(posting.location.label() if posting.location else ""),
            first_seen=now,
            last_seen=now,
            source_posted_at=posting.releasedDate,
        )


def _description_from(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    ad = payload.get("jobAd")
    sections = ad.get("sections") if isinstance(ad, dict) else None
    if not isinstance(sections, dict):
        return ""
    parts: list[str] = []
    for section in sections.values():
        if isinstance(section, dict):
            text = section.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(strip_html(text))
    return collapse_whitespace(" ".join(parts))
