import html
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, NamedTuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict

from stage.domain import Company, Job, Platform, job_id
from stage.http import HttpClient, HttpError
from stage.sources import register
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    BoardAdapter,
    FetchResult,
    NonEmptyStr,
    malformed_note,
    parse_page,
    payload_error,
    sentences,
    validate_rows,
)
from stage.sources.custom_json import MAX_HTML_ROWS

HOST = "jobs.jobvite.com"
BASE_URL = "https://jobs.jobvite.com/{slug}/jobs"
MAX_SHOW_MORE = 8
_SHOW_MORE = re.compile(r'href="(/[^"/]+/search\?c=[^"]+)"')
_SHOWN = re.compile(r"jv-pagination-text\">\s*\d+\s*-\s*(\d+)\s+of\s+(\d+)")
LISTING = "table.jv-job-list"
ROW = "table.jv-job-list tbody tr"
NAME_CELL = "td.jv-job-list-name"
LOCATION_CELL = "td.jv-job-list-location"
NOTICE = "p.jv-text-center"
NO_OPEN_JOBS = re.compile(
    r"\bno open jobs\b|\bdo not have any (?:open )?positions available\b", re.IGNORECASE
)
HIDDEN = (
    "The board hides the rest of some categories behind a Show More search page, "
    "so this read closes nothing"
)
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


def _declares_empty(page: BeautifulSoup) -> bool:
    return any(
        NO_OPEN_JOBS.search(notice.get_text(" ", strip=True)) for notice in page.select(NOTICE)
    )


def _row(block: Tag) -> dict[str, str]:
    href = _href(block)
    return {
        "title": _text(block, NAME_CELL),
        "url": urljoin(f"https://{HOST}/", href) if href else "",
        "location": _text(block, LOCATION_CELL),
    }


class JobvitePage(NamedTuple):
    rows: list[dict[str, str]]
    shown: bool = True
    listing: bool = True
    declared_empty: bool = False


def read_page(text: str) -> JobvitePage:
    page = BeautifulSoup(text, "html.parser")
    if not page.select(LISTING):
        return JobvitePage([], listing=False, declared_empty=_declares_empty(page))
    blocks = page.select(ROW)[:MAX_HTML_ROWS]
    listed = [_row(block) for block in blocks if block.select_one(NAME_CELL) is not None]
    return JobvitePage(listed, shown=len(listed) == len(blocks))


def _search_url(link: str, number: int) -> str:
    parts = urlsplit(urljoin(f"https://{HOST}/", link))
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "p"]
    return urlunsplit(parts._replace(query=urlencode([*query, ("p", str(number))])))


def _whole(text: str) -> bool:
    shown = _SHOWN.search(text)
    return shown is None or int(shown[1]) >= int(shown[2])


@register
class JobviteAdapter(BoardAdapter):
    name: ClassVar[str] = "jobvite"
    platform: ClassVar[Platform] = Platform.JOBVITE
    rate_profile: ClassVar[str] = "moderate"
    bucket_key: ClassVar[str] = "jobvite"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    detail_budget: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = 1 + MAX_SHOW_MORE

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
        links = list(dict.fromkeys(map(html.unescape, _SHOW_MORE.findall(response.text))))
        hidden, complete = await self._hidden(client, links)
        page = await parse_page(read_page, response.text)
        if not page.listing:
            if page.declared_empty:
                return FetchResult(
                    degraded="The board lists no open jobs right now, so this read closes nothing.",
                    authoritative=False,
                    declared_empty=True,
                )
            raise payload_error(
                self.name,
                company.slug,
                {"text": response.text},
                f'{self.name}/{company.slug} has no "{LISTING}" listing on its page, so the '
                "page changed or the board is retired",
            )
        listed = list(page.rows)
        for text in hidden:
            listed.extend((await parse_page(read_page, text)).rows)
        return self._result(
            company, listed, now, truncated=not page.shown and not (complete and hidden)
        )

    async def _hidden(self, client: HttpClient, links: Sequence[str]) -> tuple[list[str], bool]:
        pages: list[str] = []
        for link in links:
            number = 0
            while True:
                if len(pages) == MAX_SHOW_MORE:
                    return pages, False
                try:
                    page = await client.get_text(_search_url(link, number), revalidate=True)
                except HttpError:
                    return pages, False
                pages.append(page.text)
                if _whole(page.text):
                    break
                number += 1
        return pages, True

    def _result(
        self, company: Company, listed: Sequence[dict[str, str]], now: datetime, *, truncated: bool
    ) -> FetchResult:
        unique: dict[str, dict[str, str]] = {}
        for number, row in enumerate(listed):
            unique.setdefault(row["url"] or str(number), row)
        rows, dropped = validate_rows(
            self.row_model, list(unique.values()), source=self.name, slug=company.slug
        )
        return FetchResult(
            jobs=tuple(self.to_job(company, row, now) for row in rows),
            degraded=sentences(malformed_note(dropped), HIDDEN if truncated else ""),
            authoritative=not dropped and not truncated,
        )

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
