import re
from datetime import datetime
from typing import ClassVar, NamedTuple
from urllib.parse import parse_qsl, urlencode

from bs4 import BeautifulSoup
from bs4.element import Tag

from stage.domain import Job, SourceSignals, job_id
from stage.domain.text import counted
from stage.http import HttpClient
from stage.sources import register_feed
from stage.sources._text import collapse_whitespace
from stage.sources.base import FetchResult, malformed_note, parse_page, payload_error, sentences

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


class ResultsPage(NamedTuple):
    rows: list[dict[str, str]]
    counts: dict[str, int] | None
    queries: dict[str, str]


def _row(article: Tag) -> dict[str, str]:
    return {
        "id": posting_id(article),
        "title": field(article, ".noctitle"),
        "term": declared_term(article),
        "company": field(article, ".business"),
        "city": field(article, ".location"),
    }


def read_page(text: str) -> ResultsPage:
    page = BeautifulSoup(text, "html.parser")
    queries: dict[str, str] = {}
    for target in COUNTED_TYPES:
        control = page.select_one(f"input#{target}")
        query = str(control.get("data-url") or "") if control is not None else ""
        if query:
            queries[target] = query
    return ResultsPage(
        [_row(article) for article in page.select("article")], type_counts(page), queries
    )


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
                page = await parse_page(read_page, text.text)
                rows = page.rows
                if not rows:
                    empty.append(f"{province}/{term}")
                    last_empty = text.text
                    continue
                searched = True
                counts = page.counts
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
                        found = await self._filtered(client, page.queries.get(target, ""), count)
                        unread += max(0, count - sum(1 for row in found if row["term"]))
                        rows.extend(found)
                for row in rows:
                    job, dropped = self._to_job(row, now)
                    malformed += dropped
                    if job is not None:
                        seen.setdefault(job.id, job)
                if len(seen) >= MAX_ROWS:
                    truncated = True
                    break
            if truncated:
                break

        if not searched:
            raise payload_error(
                self.name,
                "search",
                {"head": last_empty[:4000]},
                f"No {self.name} search returned an <article> row, so the results page "
                "changed shape",
            )

        searches = len(PROVINCES) * len(TERMS)
        declared_empty = counted_none and not seen and not malformed
        return FetchResult(
            jobs=tuple(seen.values()),
            authoritative=False,
            degraded=sentences(
                f"{len(empty)} of {searches} searches matched nothing" if empty else "",
                f"Stage stopped at the {MAX_ROWS:,}-posting cap for one run" if truncated else "",
                malformed_note(malformed),
                f"Stage could not read {counted(unread, 'counted student or internship job')}"
                if unread
                else "",
                "Job Bank lists no student jobs or internships for these searches"
                if declared_empty
                else "",
            ),
            declared_empty=declared_empty,
        )

    async def _filtered(self, client: HttpClient, query: str, count: int) -> list[dict[str, str]]:
        if not query:
            return []
        rows: list[dict[str, str]] = []
        for number in range(1, FILTER_PAGE_CAP + 1):
            found = await self._rows(client, filtered_url(query, number))
            if not found:
                break
            rows.extend(found)
            if len(rows) >= count:
                break
        return rows

    @staticmethod
    async def _rows(
        client: HttpClient, url: str, *, revalidate: bool = False
    ) -> list[dict[str, str]]:
        text = await client.get_text(url, revalidate=revalidate)
        if text.not_modified:
            return []
        return (await parse_page(read_page, text.text)).rows

    def _to_job(self, row: dict[str, str], now: datetime) -> tuple[Job | None, int]:
        identifier, title, term = row["id"], row["title"], row["term"]
        if not identifier or not title:
            return None, 1
        if not term:
            return None, 0
        company = row["company"] or "Job Bank"
        city = row["city"]
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
