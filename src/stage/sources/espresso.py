from datetime import datetime
from typing import ClassVar

from bs4 import BeautifulSoup
from bs4.element import Tag

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient, HttpStatusError
from stage.http.client import HostBudgetExceededError
from stage.sources import register_feed
from stage.sources._text import collapse_whitespace
from stage.sources.base import FetchResult, malformed_note, parse_page, payload_error, sentences

HOST = "www.espresso-jobs.com"
SEARCH = f"https://{HOST}/emploi"
POSTING = f"https://{HOST}/emploi/{{id}}/{{slug}}"
TERMS = ("stage", "stagiaire", "intern", "internship")
PAGE_CAP = 3
PAGE_SIZE = 21
MAX_ROWS = 1000
INTERNSHIP_BADGE = "stage"
ROW_SELECTOR = "div.job_index-content_list_item"


def badge(row: Tag) -> str:
    found = row.select_one("p.job_index-content_list_item_infos-type")
    if found is None:
        return ""
    return collapse_whitespace(found.get_text(" ", strip=True))


def field(row: Tag, selector: str) -> str:
    found = row.select_one(selector)
    if found is None:
        return ""
    return collapse_whitespace(found.get_text(" ", strip=True))


def where(row: Tag) -> str:
    found = row.select_one("div.job-location-info")
    if found is None:
        return "Québec, Canada"
    city = collapse_whitespace(str(found.get("data-city") or ""))
    province = collapse_whitespace(str(found.get("data-province") or ""))
    parts = [part for part in (city, province, "Canada") if part]
    return ", ".join(parts)


def listed(text: str) -> list[dict[str, str]]:
    return [
        {
            "id": collapse_whitespace(str(row.get("id") or "")),
            "slug": collapse_whitespace(str(row.get("data-slug") or "")),
            "title": field(row, "h2.job_index-content_list_item-title"),
            "badge": badge(row),
            "company": field(row, "p.job_index-content_list_item-company"),
            "where": where(row),
        }
        for row in BeautifulSoup(text, "html.parser").select(ROW_SELECTOR)
    ]


@register_feed
class EspressoJobsFeed:
    name: ClassVar[str] = "espresso-jobs"
    rate_profile: ClassVar[str] = "feeds"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = "espresso-jobs"

    def season_year(self, now: datetime) -> int:
        return now.year

    def plan(self, now: datetime) -> tuple[str, ...]:
        return tuple(f"{SEARCH}?keyword={term}&distance=all&page_no=1" for term in TERMS)

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        seen: dict[str, Job] = {}
        malformed = 0
        truncated = False
        capped = False
        searched = False
        empty: list[str] = []
        last_empty = ""

        exhausted: list[str] = []

        for term in TERMS:
            for page in range(1, PAGE_CAP + 1):
                url = f"{SEARCH}?keyword={term}&distance=all&page_no={page}"
                try:
                    text = await client.get_text(url, revalidate=page > 1)
                except HostBudgetExceededError:
                    if not searched:
                        raise
                    capped = True
                    break
                except HttpStatusError as exc:
                    if page > 1 and exc.status == 404:
                        exhausted.append(term)
                        break
                    raise
                if text.not_modified:
                    break
                rows = await parse_page(listed, text.text)
                if not rows:
                    if page == 1:
                        empty.append(term)
                        last_empty = text.text
                    break
                searched = True
                for row in rows:
                    job, dropped = self._to_job(row, now)
                    malformed += dropped
                    if job is not None:
                        seen.setdefault(job.id, job)
                if len(seen) >= MAX_ROWS:
                    truncated = True
                    break
                if len(rows) < PAGE_SIZE:
                    break
            if truncated or capped:
                break

        if not searched:
            raise payload_error(
                self.name,
                "search",
                {"head": last_empty[:4000]},
                f"No {self.name} search returned a listing row, so the results page changed shape",
            )

        return FetchResult(
            jobs=tuple(seen.values()),
            authoritative=False,
            degraded=sentences(
                f"{len(empty)} of {len(TERMS)} searches matched nothing" if empty else "",
                f"{len(exhausted)} of {len(TERMS)} searches ran past their last page, "
                "which answers 404"
                if exhausted
                else "",
                f"Stage stopped at the {MAX_ROWS:,}-posting cap for one run" if truncated else "",
                "Stage reached its request ceiling for this run, so the last searches wait for "
                "the next sync"
                if capped
                else "",
                malformed_note(malformed),
            ),
        )

    def _to_job(self, row: dict[str, str], now: datetime) -> tuple[Job | None, int]:
        identifier, slug, title = row["id"], row["slug"], row["title"]
        if not identifier or not slug or not title:
            return None, 1
        declared = row["badge"]
        if declared.lower() != INTERNSHIP_BADGE:
            return None, 0
        company = row["company"] or "Espresso-Jobs"
        return (
            Job(
                id=job_id(self.name, "search", identifier),
                source=self.name,
                company=company,
                title_raw=title,
                title_normalized=title.lower(),
                apply_url_raw=POSTING.format(id=identifier, slug=slug),
                description="",
                location_raw=row["where"],
                first_seen=now,
                last_seen=now,
                signals=SourceSignals(employment_type=declared.lower()),
            ),
            0,
        )
