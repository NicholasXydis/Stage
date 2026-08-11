from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from stage.domain import Company, CustomBoard, Job, Platform, board_key, job_id
from stage.http import HttpClient
from stage.sources import register
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
from stage.sources.platforms import dig

MAX_ROWS = 5000


class CustomListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: NonEmptyStr
    title: NonEmptyStr
    location: str = ""
    url: str = ""
    description: str = ""
    department: str = ""


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _rows(payload: Any, board: CustomBoard) -> Any:
    return dig(payload, board.jobs_path) if board.jobs_path else payload


def _project(entry: Any, board: CustomBoard) -> dict[str, str]:
    projected: dict[str, str] = {}
    for name in ("title", "id", "location", "url", "description", "department"):
        path = board.mapped(name)
        if not path:
            continue
        value = dig(entry, path)
        if isinstance(value, list):
            value = " / ".join(str(item) for item in value if item)
        projected[name] = "" if value is None else str(value)
    if not projected.get("id"):
        projected["id"] = projected.get("url") or projected.get("title", "")
    return projected


@register
class CustomJsonAdapter:
    name: ClassVar[str] = "custom_json"
    platform: ClassVar[Platform] = Platform.CUSTOM_JSON
    rate_profile: ClassVar[str] = "conservative"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = ""
    detail_budget: ClassVar[int] = 0
    rotation_slice: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1

    def board_key(self, company: Company) -> str:
        return board_key(self.name, company.slug)

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        found = set()
        for company in companies:
            if company.custom is not None:
                host = _host(company.custom.url)
                if host:
                    found.add(host)
        return frozenset(found)

    def plan(self, company: Company) -> tuple[str, ...]:
        board = self._board(company)
        return (board.url,)

    @staticmethod
    def _board(company: Company) -> CustomBoard:
        if company.custom is None:
            raise PayloadValidationError(
                f"{company.name}: platform custom_json needs a 'custom' block — "
                "add url and a title field mapping to the registry row"
            )
        return company.custom

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        board = self._board(company)
        response = await client.get_json(board.url)
        if response.not_modified:
            return FetchResult(not_modified=True)

        entries = _rows(response.payload, board)
        if not isinstance(entries, list):
            captured = capture_payload(self.name, company.slug, response.payload)
            raise PayloadValidationError(
                f"{self.name}/{company.slug}: jobs_path "
                f"{board.jobs_path or '<root>'!r} is not a list; captured at {captured}"
            )
        truncated = len(entries) > MAX_ROWS
        listings, dropped = validate_rows(
            CustomListing,
            [_project(entry, board) for entry in entries[:MAX_ROWS]],
            source=self.name,
            slug=company.slug,
        )
        jobs, unconvertible = convert_rows(
            lambda listing: self._to_job(company, board, listing, now),
            listings,
            source=self.name,
            slug=company.slug,
        )
        dropped += unconvertible
        notes = [malformed_note(dropped)] if dropped else []
        if truncated:
            notes.append(f"stopped at {MAX_ROWS} rows; the listing is incomplete")
        return FetchResult(
            jobs=tuple(jobs),
            degraded="; ".join(note for note in notes if note),
            authoritative=not (dropped or truncated),
        )

    def _to_job(
        self, company: Company, board: CustomBoard, listing: CustomListing, now: datetime
    ) -> Job:
        apply_url = listing.url
        if not apply_url and board.url_template:
            apply_url = board.url_template.format(id=listing.id)
        return Job(
            id=job_id(self.name, company.slug, listing.id),
            source=self.name,
            company=company.name,
            title_raw=listing.title,
            title_normalized=collapse_whitespace(listing.title),
            apply_url_raw=apply_url or board.url,
            description=collapse_whitespace(strip_html(listing.description)),
            location_raw=collapse_whitespace(listing.location),
            first_seen=now,
            last_seen=now,
        )
