import re
from datetime import datetime
from typing import ClassVar
from urllib.parse import parse_qsl, urlencode

from bs4 import BeautifulSoup
from bs4.element import Tag

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient
from stage.sources import register_feed
from stage.sources._text import collapse_whitespace
from stage.sources.base import FetchResult, PayloadValidationError, capture_payload, malformed_note

HOST = "www.jobbank.gc.ca"
SEARCH = f"https://{HOST}/jobsearch/jobsearch"
POSTING = f"https://{HOST}/jobsearch/jobposting/{{id}}"
TERMS = ("programmer", "developer", "software", "informatique", "programmeur", "développeur")
PROVINCES = ("QC", "ON", "BC", "AB")
PAGE_CAP = 2
FILTER_PAGE_CAP = 4
MAX_ROWS = 2000
_ARTICLE_ID = re.compile(r"^article-(\d+)$")
KEPT_FLAGS = ("jobinternshipflag", "jobstudentflag")
COUNTED_TYPES = ("job-types_1", "job-types_intern")
_COUNT = re.compile(r"\d[\d,]*")


def posting_id(article: Tag) -> str:
    found = _ARTICLE_ID.match(str(article.get("id") or ""))
    return found.group(1) if found else ""


def declared_term(article: Tag) -> str:
    for flag in KEPT_FLAGS:
        found = article.select_one(f".{flag}")
        if found is not None:
            return collapse_whitespace(found.get_text(" ", strip=True))
    return ""


def type_counts(page: BeautifulSoup) -> dict[str, int] | None:
    counts: dict[str, int] = {}
    for target in COUNTED_TYPES:
        badge = page.select_one(f'label[for="{target}"] .badge')
        found = _COUNT.search(badge.get_text(" ", strip=True)) if badge is not None else None
        if found is None:
            return None
        counts[target] = int(found.group(0).replace(",", ""))
    return counts


def filtered_url(query: str, page: int) -> str:
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True)]
    pairs = [(key, value) for key, value in pairs if key != "page"]
    return f"{SEARCH}?{urlencode([*pairs, ('page', str(page))])}"


def field(article: Tag, selector: str) -> str:
    found = article.select_one(selector)
    if found is None:
        return ""
    for hidden in found.select(".wb-inv"):
        hidden.decompose()
    return collapse_whitespace(found.get_text(" ", strip=True))


@register_feed
class JobBankFeed:
    name: ClassVar[str] = "jobbank"
    rate_profile: ClassVar[str] = "jobbank"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = "jobbank"

    def season_year(self, now: datetime) -> int:
        return now.year

    def plan(self, now: datetime) -> tuple[str, ...]:
        return tuple(
            f"{SEARCH}?searchstring={term}&fprov={province}"
            for province in PROVINCES
            for term in TERMS
        )

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        seen: dict[str, Job] = {}
        malformed = 0
        truncated = False
        searched = False
        counted_none = True
        unread = 0
        empty: list[str] = []
        last_empty = ""
        for province in PROVINCES:
            for term in TERMS:
                search = f"{SEARCH}?searchstring={term}&fprov={province}"
                text = await client.get_text(f"{search}&page=1")
                if text.not_modified:
                    continue
                page = BeautifulSoup(text.text, "html.parser")
                rows = page.select("article")
                if not rows:
                    empty.append(f"{province}/{term}")
                    last_empty = text.text
                    continue
                searched = True
                counts = type_counts(page)
                if counts is None:
                    counted_none = False
                    for number in range(2, PAGE_CAP + 1):
                        more = await self._rows(client, f"{search}&page={number}", revalidate=True)
                        if not more:
                            break
                        rows.extend(more)
                else:
                    for target, count in counts.items():
                        if not count:
                            continue
                        counted_none = False
                        found = await self._filtered(client, page, target, count)
                        unread += max(0, count - sum(1 for row in found if declared_term(row)))
                        rows.extend(found)
                for article in rows:
                    job, dropped = self._to_job(article, now)
                    malformed += dropped
                    if job is not None:
                        seen.setdefault(job.id, job)
                if len(seen) >= MAX_ROWS:
                    truncated = True
                    break
            if truncated:
                break

        if not searched:
            captured = capture_payload(self.name, "search", {"head": last_empty[:4000]})
            raise PayloadValidationError(
                f"{self.name}: no query returned an <article> row, so the results page changed "
                f"shape rather than matching nothing; captured at {captured}"
            )

        notes = []
        if empty:
            notes.append(f"{len(empty)} of {len(PROVINCES) * len(TERMS)} searches matched nothing")
        if truncated:
            notes.append(f"stopped at the {MAX_ROWS}-posting cap for one run")
        if malformed:
            notes.append(malformed_note(malformed))
        if unread:
            notes.append(f"{unread} counted student or internship job(s) could not be read")
        declared_empty = counted_none and not seen and not malformed
        if declared_empty:
            notes.append("Job Bank lists no student jobs or internships for these searches")
        return FetchResult(
            jobs=tuple(seen.values()),
            authoritative=False,
            degraded="; ".join(notes),
            declared_empty=declared_empty,
        )

    async def _filtered(
        self, client: HttpClient, page: BeautifulSoup, target: str, count: int
    ) -> list[Tag]:
        control = page.select_one(f"input#{target}")
        query = str(control.get("data-url") or "") if control is not None else ""
        if not query:
            return []
        rows: list[Tag] = []
        for number in range(1, FILTER_PAGE_CAP + 1):
            found = await self._rows(client, filtered_url(query, number))
            if not found:
                break
            rows.extend(found)
            if len(rows) >= count:
                break
        return rows

    @staticmethod
    async def _rows(client: HttpClient, url: str, *, revalidate: bool = False) -> list[Tag]:
        text = await client.get_text(url, revalidate=revalidate)
        if text.not_modified:
            return []
        return BeautifulSoup(text.text, "html.parser").select("article")

    def _to_job(self, article: Tag, now: datetime) -> tuple[Job | None, int]:
        identifier = posting_id(article)
        title = field(article, ".noctitle")
        term = declared_term(article)
        if not identifier or not title:
            return None, 1
        if not term:
            return None, 0
        company = field(article, ".business") or "Job Bank"
        city = field(article, ".location")
        return (
            Job(
                id=job_id(self.name, "search", identifier),
                source=self.name,
                company=company,
                title_raw=title,
                title_normalized=title.lower(),
                apply_url_raw=POSTING.format(id=identifier),
                description="",
                location_raw=city or "Canada",
                first_seen=now,
                last_seen=now,
                signals=SourceSignals(employment_type=term.lower()),
            ),
            0,
        )
