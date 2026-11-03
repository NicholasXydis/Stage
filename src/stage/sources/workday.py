import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from stage.domain import (
    FULL_READ_PREFIX,
    SEARCH_PREFIX,
    Company,
    DetailFetch,
    Job,
    Platform,
    SourceSignals,
    WorkdayCrawl,
    WorkdayCrawlStep,
    WorkdayFacet,
    board_key,
    job_id,
)
from stage.domain.text import counted
from stage.http import (
    CEILING_BACKSTOP,
    CaptivePortalError,
    HttpClient,
    HttpError,
    failure_text,
)
from stage.lexicon import fold, internship_lexicon, workday_facet_lexicon
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    parsed,
    sentences,
    validate_rows,
)
from stage.sources.platforms import SlugRejectedError, workday_target

PAGE_SIZE = 20
MAX_PAGES = 25
MAX_CRAWL_PAGES = 100
RESULT_CAP = 10_000
CRAWL_PAGE_CAP = 6
RETRY_RESERVE = 40
CHECK_RESERVE = 30
DETAIL_BUDGET = 60
HUGE_BOARD = CRAWL_PAGE_CAP * PAGE_SIZE
FULL_READ_MAX = MAX_CRAWL_PAGES * PAGE_SIZE
NARROW_TERMS = (
    "internship",
    "stagiaire",
    "alternance",
    "etudiant",
    '"summer associate"',
    '"summer analyst"',
    '"summer scholar"',
    '"industrial year"',
    '"year in industry"',
    "professionnalisation",
)
BROAD_TERMS = (
    "intern",
    '"co-op"',
    "coop",
    "stage",
    "student",
    "placement",
    "alternant",
    "undergraduate",
    '"work term"',
    "intership",
)
SEARCH_TERMS = NARROW_TERMS + BROAD_TERMS
SINGLE_TERMS = ("internship", "intern", "student", "stage", "stagiaire", "alternance")
WHOLE_READ_BOARDS: frozenset[str] = frozenset(
    {
        "workday:amcor-amcor_external_career_site",
        "workday:broadcom-external_career",
        "workday:cai-computer_aid",
        "workday:clio-cliocareersite",
        "workday:comcast-comcast_careers",
        "workday:coreandmain-coreandmain",
        "workday:danaher-danaherjobs",
        "workday:fis-searchjobs",
        "workday:jabil-jabil_careers",
        "workday:lowes-lws_external_cs",
        "workday:lseg-careers",
        "workday:maersk-maersk_careers",
        "workday:myhrabc-global",
        "workday:nationwidechildrens-nchcareers",
        "workday:pru-careers",
        "workday:qualys-careers",
        "workday:redhat-jobs",
        "workday:southstatebank-external",
        "workday:unisys-external",
        "workday:usnh-careers",
    }
)
NARROW_TEXT = " OR ".join(NARROW_TERMS)
BROAD_TEXT = " OR ".join(BROAD_TERMS)
SEARCH_PAGES = 12
QUIET_PAGES = 2
_NARROW_WORDS = tuple(fold(term) for term in NARROW_TERMS)
_BROAD_WORDS = tuple(fold(term) for term in BROAD_TERMS)
_REQUISITION = re.compile(r"\S*\d\S*")
_DAYS = re.compile(r"(\d+)\s*\+?\s*(?:days?|jours?)")


class WorkdayPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    externalPath: str = ""
    locationsText: str = ""
    postedOn: str = ""
    bulletFields: list[str] = Field(default_factory=list)

    def requisition(self) -> str:
        for field in self.bulletFields:
            cleaned = field.strip()
            if cleaned:
                if _REQUISITION.fullmatch(cleaned):
                    return cleaned
                break
        tail = self.externalPath.rstrip("/").rsplit("_", 1)
        return tail[-1] if len(tail) == 2 and tail[-1] else self.externalPath.rstrip("/")


class WorkdayFacetValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    descriptor: str = ""
    count: int = 0
    facetParameter: str = ""
    values: list["WorkdayFacetValue"] = Field(default_factory=list)


class WorkdayFacetGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facetParameter: str = ""
    descriptor: str = ""
    values: list[WorkdayFacetValue] = Field(default_factory=list)


class WorkdayPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: int = 0
    jobPostings: list[WorkdayPosting]
    facets: list[WorkdayFacetGroup] | None = None
    hidden: int = 0


class WorkdayRawPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: int = 0
    jobPostings: list[dict[str, Any]]
    facets: list[WorkdayFacetGroup] | None = None


def _matches(folded: str, descriptors: frozenset[str]) -> bool:
    padded = f" {folded} "
    if any(f" {phrase} " in padded for phrase in internship_lexicon().blocked_bigrams):
        return False
    return any(f" {phrase} " in padded for phrase in descriptors)


def resolve_facet(page: WorkdayPage, tenant: str, site: str, now: datetime) -> WorkdayFacet | None:
    parameters, descriptors = workday_facet_lexicon()
    groups = {group.facetParameter: group for group in page.facets or ()}
    for parameter in parameters:
        group = groups.get(parameter)
        if group is None:
            continue
        matched = [
            value
            for value in group.values
            if value.id and _matches(fold(value.descriptor), descriptors)
        ]
        if matched:
            return WorkdayFacet(
                tenant=tenant,
                site=site,
                parameter=parameter,
                facet_ids=tuple(value.id for value in matched),
                descriptor=", ".join(value.descriptor for value in matched),
                resolved_at=now,
            )
    return None


def _facet_groups(
    parameter: str, values: Sequence[WorkdayFacetValue]
) -> Iterator[tuple[str, Sequence[WorkdayFacetValue]]]:
    yield parameter, values
    for value in values:
        if value.facetParameter:
            yield from _facet_groups(value.facetParameter, value.values)


def facet_still_offered(page: WorkdayPage, facet: WorkdayFacet) -> bool:
    if not page.facets:
        return True
    for group in page.facets:
        for parameter, values in _facet_groups(group.facetParameter, group.values):
            if parameter != facet.parameter:
                continue
            offered = {value.id for value in values}
            if all(facet_id in offered for facet_id in facet.facet_ids):
                return True
    return False


@register
class WorkdayAdapter:
    name: ClassVar[str] = "workday"
    platform: ClassVar[Platform] = Platform.WORKDAY
    rate_profile: ClassVar[str] = "workday"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = "workday"

    detail_budget: ClassVar[int] = DETAIL_BUDGET

    rotation_slice: ClassVar[int] = CEILING_BACKSTOP - RETRY_RESERVE - DETAIL_BUDGET

    max_requests_per_company: ClassVar[int] = MAX_PAGES

    crawl_page_cap: ClassVar[int] = CRAWL_PAGE_CAP
    retry_reserve: ClassVar[int] = RETRY_RESERVE
    check_reserve: ClassVar[int] = CHECK_RESERVE

    @classmethod
    def crawl_budgets(
        cls,
        companies: Sequence[Company],
        crawls: Mapping[str, WorkdayCrawl],
        facets: Mapping[tuple[str, str], WorkdayFacet],
        ceiling: int,
        full_reads: Sequence[str] = (),
        glances: Collection[str] = (),
    ) -> tuple[dict[str, int], int]:
        if not companies:
            raise ValueError("a Workday crawl needs at least one board")
        available = max(1, ceiling - cls.retry_reserve)
        budgets = {company.registry_key: 1 for company in companies}
        remaining = max(0, available - len(budgets))
        demands: list[tuple[int, str]] = []
        for company in companies:
            crawl = crawls.get(board_key(cls.name, _board(company)))
            facet = _pinned_facet(company) or facets.get(
                (company.workday_tenant or "", company.workday_site or "")
            )
            if crawl is None or crawl.total is None or _unfiltered_and_huge(crawl):
                continue
            if crawl.facet_parameter != (facet.parameter if facet is not None else ""):
                continue
            if crawl.facet_ids != (facet.facet_ids if facet is not None else ()):
                continue
            start = max(0, crawl.next_offset - PAGE_SIZE)
            pages = max(1, (max(0, crawl.total - start) + PAGE_SIZE - 1) // PAGE_SIZE)
            demands.append((min(MAX_CRAWL_PAGES, pages), company.registry_key))
        for pages, key in sorted(demands):
            extra = max(0, min(remaining, pages - budgets[key]))
            budgets[key] += extra
            remaining -= extra
        for company in sorted(companies, key=lambda row: _faceted(row, facets)):
            key = company.registry_key
            extra = max(0, min(remaining, cls.crawl_page_cap - budgets[key]))
            budgets[key] += extra
            remaining -= extra
        for key in glances:
            if key not in budgets:
                continue
            extra = max(0, min(remaining, cls.crawl_page_cap + SEARCH_PAGES - budgets[key]))
            budgets[key] += extra
            remaining -= extra
        details = min(cls.detail_budget, max(0, remaining))
        remaining = max(0, remaining - details - cls.check_reserve)
        for key in full_reads:
            if key not in budgets:
                continue
            extra = max(0, min(remaining, MAX_CRAWL_PAGES - budgets[key]))
            budgets[key] += extra
            remaining -= extra
        return budgets, details

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        allowed: set[str] = set()
        for company in companies:
            try:
                host, _ = workday_target(
                    company.workday_tenant or "",
                    company.workday_site or "",
                    company.workday_dc or "",
                )
            except SlugRejectedError:
                continue
            allowed.add(host)
        return frozenset(allowed)

    def board_key(self, company: Company) -> str:
        return board_key(self.name, _board(company))

    def plan(self, company: Company) -> tuple[str, ...]:
        host, path = workday_target(
            company.workday_tenant or "",
            company.workday_site or "",
            company.workday_dc or "",
        )
        return (f"https://{host}{path}",)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: Mapping[tuple[str, str], WorkdayFacet] | None = None,
        details: Sequence[str] = (),
        crawl: WorkdayCrawl | None = None,
        page_budget: int = MAX_PAGES,
        full_read: bool = False,
        size_known: bool = False,
        search: bool = True,
    ) -> FetchResult:
        if page_budget < 1:
            raise ValueError("Workday page budget must be positive")
        if page_budget > MAX_CRAWL_PAGES:
            raise ValueError("Workday page budget exceeds its crawl cap")
        url = self.plan(company)[0]
        tenant = company.workday_tenant or ""
        site = company.workday_site or ""
        facet = _pinned_facet(company) or (facets or {}).get((tenant, site))
        applied = {facet.parameter: list(facet.facet_ids)} if facet is not None else {}
        crawl_reset = crawl is not None and (
            crawl.facet_parameter != (facet.parameter if facet is not None else "")
            or crawl.facet_ids != (facet.facet_ids if facet is not None else ())
        )
        persisted = None if crawl_reset else crawl
        if persisted is not None and _unfiltered_and_huge(persisted):
            persisted = None
            crawl_reset = True
        degraded = ""

        discovered: WorkdayFacet | None = None
        forgotten: WorkdayFacet | None = None
        restarted = False
        malformed = 0
        fell_back = False
        stale_facet = False
        drifted = False
        postings: list[WorkdayPosting] = []
        reached_end = False
        offset = 0
        if persisted is not None:
            offset = persisted.next_offset
            if page_budget > 1:
                offset = max(0, offset - PAGE_SIZE)
        pages = 0
        total = persisted.total if persisted is not None else None
        total_changed = False
        listed_facets = True
        huge = False
        whole = False
        limit = page_budget

        while pages < limit:
            body: dict[str, Any] = {
                "appliedFacets": applied,
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            }
            response = await client.post_json(url, body=body)
            page, dropped = _validate(response.payload, company)
            malformed += dropped
            pages += 1

            if facet is not None and page.facets is None:
                if not drifted:
                    drifted = True
                    captured = capture_payload("workday-nofacets", company.slug, response.payload)
                    degraded = (
                        f"The board sent no facet list at all while Stage applied facet "
                        f"{_ids(facet)}, so Stage kept the facet and this read closes nothing. "
                        f"Stage saved the answer to {captured}"
                    )
            elif facet is not None and not facet_still_offered(page, facet):
                if facet.pinned:
                    stale_facet = True
                    degraded = (
                        f"The pinned facet {_ids(facet)} is no longer offered under "
                        f'"{facet.parameter}", but Stage used it anyway. Re-pin it with '
                        "stage discover --url or clear workday_facet"
                    )
                else:
                    probe_body: dict[str, Any] = {
                        "appliedFacets": {},
                        "limit": PAGE_SIZE,
                        "offset": 0,
                        "searchText": "",
                    }
                    probe_response = await client.post_json(url, body=probe_body)
                    probe, probe_dropped = _validate(probe_response.payload, company)
                    malformed += probe_dropped
                    pages += 1
                    if not facet_still_offered(probe, facet):
                        stale_facet = True
                        degraded = (
                            f"The saved facet {_ids(facet)} is no longer offered under "
                            f'"{facet.parameter}", so Stage looked for a new one in the '
                            "board's facet list"
                        )
                        forgotten = facet
                        facet = None
                        applied = {}
                        postings.clear()
                        offset = 0
                        total = None
                        crawl_reset = True
                        page = probe
                        dropped = probe_dropped
                        response = probe_response

            if facet is None and not applied:
                resolved = resolve_facet(page, tenant, site, now)
                if resolved is not None:
                    discovered = resolved
                    forgotten = None
                else:
                    fell_back = True
                    listed_facets = listed_facets and page.facets is not None
                    degraded = _fallback_reason(page, company, response.payload)

            if discovered is not None and not applied and not restarted:
                applied = {discovered.parameter: list(discovered.facet_ids)}
                restarted = True
                postings.clear()
                offset = 0
                total = None
                crawl_reset = True
                continue

            postings.extend(page.jobPostings)
            if page.total > 0:
                if total is None:
                    total = page.total
                elif total != page.total:
                    total_changed = True
                    total = page.total

            returned = len(page.jobPostings) + page.hidden + dropped
            if returned < PAGE_SIZE:
                reached_end = True
                break
            if fell_back and not huge and total is not None and total > HUGE_BOARD:
                huge = True
                whole = full_read and total < FULL_READ_MAX and _pages(total) <= page_budget
                if not whole:
                    limit = 1 if _newest_first(postings) else min(page_budget, CRAWL_PAGE_CAP)
            offset += PAGE_SIZE
            if offset >= _last(total):
                reached_end = True
                break

        glance = huge and not whole
        capped = not reached_end and not glance
        searched: list[DetailFetch] = []
        if glance and search:
            found, spent, lost, stopped = await _search(
                client,
                url,
                company,
                page_budget - pages,
                exhaustive=self.board_key(company) in WHOLE_READ_BOARDS,
            )
            pages += spent
            malformed += lost
            known = {posting.requisition() for posting in postings}
            for posting in found:
                if posting.requisition() not in known:
                    known.add(posting.requisition())
                    postings.append(posting)
            degraded = sentences(
                f"Stage read the newest postings of this {total or 0:,}-posting board and "
                "searched it for internships, since it has no internship filter, so it closes "
                "postings only on its weekly full read",
                stopped,
            )
            if not stopped or stopped == IGNORED_OR:
                searched.append(DetailFetch(id=f"{SEARCH_PREFIX}{self.board_key(company)}"))
        elif glance:
            degraded = (
                f"Stage read only the newest postings of this {total or 0:,}-posting board, "
                "which has no internship filter and was searched for internships within "
                "5.5 hours, so it closes postings only on its weekly full read"
            )
        if capped:
            if page_budget != MAX_PAGES and page_budget < MAX_CRAWL_PAGES:
                degraded = (
                    f"Stage paused the crawl after {counted(pages, 'page')} and resumes from "
                    f"posting {offset} next sync, so this read closes nothing yet"
                )
            else:
                degraded = (
                    f"Stage stopped at the {page_budget}-page cap, so the listing may be incomplete"
                )
        if total_changed:
            degraded = (
                "The board's total changed during the crawl, so Stage kept its progress and "
                f"resumes from posting {offset} next sync"
                if not reached_end
                else "The board's total changed during the crawl, so Stage refreshed its "
                "postings but closed nothing, and the next sync starts a fresh pass"
            )
        degraded = sentences(malformed_note(malformed), degraded)

        internship_facets = workday_facet_lexicon()[0]
        faceted = "internship" if set(applied) & set(internship_facets) else ""
        paired = [(posting, _to_job(company, posting, now, faceted)) for posting in postings]
        wanted = set(details)
        fetched: list[DetailFetch] = []
        if wanted:
            paired, fetched = await _attach_descriptions(company, client, paired, wanted)

        crawl_step = None
        crawling = (page_budget < MAX_PAGES or crawl is not None) and not glance and not whole
        unfiltered = fell_back and not (listed_facets and (whole or not huge))
        safe = not (malformed or unfiltered or stale_facet or drifted or total_changed)
        if crawl is not None and (glance or whole):
            crawl_step = WorkdayCrawlStep(
                board=self.board_key(company),
                next_offset=0,
                total=total,
                discard=True,
            )
        if crawling:
            crawl_step = WorkdayCrawlStep(
                board=self.board_key(company),
                next_offset=0 if reached_end else offset,
                total=total,
                facet_parameter=next(iter(applied), ""),
                facet_ids=tuple(next(iter(applied.values()), ())),
                seen_ids=tuple(dict.fromkeys(job.id for _, job in paired)),
                complete=reached_end and safe,
                reset=crawl_reset,
                discard=bool(malformed) or (reached_end and not safe),
            )

        fetched.extend(searched)
        if huge and (full_read or not size_known):
            attempt = whole and reached_end and safe
            fetched.append(
                DetailFetch(
                    id=f"{FULL_READ_PREFIX}{self.board_key(company)}",
                    resolved=attempt,
                    failed=full_read
                    and not attempt
                    and total is not None
                    and total < FULL_READ_MAX,
                )
            )

        return FetchResult(
            jobs=tuple(job for _, job in paired),
            degraded=degraded,
            authoritative=reached_end and safe and not glance,
            facets=(discovered,) if discovered is not None else (),
            forgotten_facets=(forgotten,) if forgotten is not None else (),
            detail_fetches=tuple(fetched),
            workday_crawl=crawl_step,
        )


async def _search(
    client: HttpClient, url: str, company: Company, budget: int, exhaustive: bool = False
) -> tuple[list[WorkdayPosting], int, int, str]:
    found: list[WorkdayPosting] = []
    pages = lost = 0
    limit = budget if exhaustive else min(budget, SEARCH_PAGES)
    queue = [(NARROW_TEXT, _NARROW_WORDS, exhaustive), (BROAD_TEXT, _BROAD_WORDS, exhaustive)]
    ignored = False
    while queue:
        text, words, whole = queue.pop(0)
        offset = total = quiet = 0
        while True:
            if pages >= limit:
                return found, pages, lost, _search_note(f"stopped after {counted(pages, 'page')}")
            body: dict[str, Any] = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": text,
            }
            try:
                response = await client.post_json(url, body=body)
                page, dropped = _validate(response.payload, company)
            except CaptivePortalError:
                raise
            except (HttpError, httpx.HTTPError, PayloadValidationError) as exc:
                return found, pages, lost, _search_note(f"failed ({failure_text(exc)})")
            pages += 1
            lost += dropped
            found.extend(page.jobPostings)
            offset += PAGE_SIZE
            total = total or page.total
            named = any(_named(posting.title, words) for posting in page.jobPostings)
            quiet = 0 if named or whole else quiet + 1
            returned = len(page.jobPostings) + page.hidden + dropped
            if returned < PAGE_SIZE or offset >= _last(total or None):
                break
            if quiet == QUIET_PAGES:
                break
        if words is _BROAD_WORDS and not total:
            ignored = True
            queue.extend((term, (fold(term),), False) for term in SINGLE_TERMS if exhaustive)
    return found, pages, lost, IGNORED_OR if ignored and not exhaustive else ""


IGNORED_OR = (
    "The board's search found nothing for any internship word, which means it ignores OR, "
    "so internships past the newest postings wait for the weekly full read"
)


def _search_note(outcome: str) -> str:
    return (
        f"The internship search {outcome}, so internships past the newest postings wait "
        "for the next sync"
    )


def _named(title: str, words: tuple[str, ...]) -> bool:
    padded = f" {fold(title)}"
    return any(f" {word}" in padded for word in words)


def _fallback_reason(page: WorkdayPage, company: Company, payload: Any) -> str:
    if page.facets:
        names = {group.facetParameter for group in page.facets if group.facetParameter}
        advertised = ", ".join(sorted(names))
        return (
            f"The board offers no internship facet among its filters ({advertised}), so Stage "
            "read the whole board and screened every posting"
        )
    if page.facets == []:
        return (
            "The board offers an empty facet list, so Stage read the whole board and "
            "screened every posting"
        )
    captured = capture_payload("workday-nofacets", company.slug, payload)
    return (
        "The board sent no facet list at all, so Stage read the whole board. "
        f"Stage saved the answer to {captured}"
    )


def _faceted(company: Company, facets: Mapping[tuple[str, str], WorkdayFacet]) -> bool:
    site = (company.workday_tenant or "", company.workday_site or "")
    return _pinned_facet(company) is not None or site in facets


def _ids(facet: WorkdayFacet) -> str:
    return ", ".join(facet.facet_ids)


def _unfiltered_and_huge(crawl: WorkdayCrawl) -> bool:
    return not crawl.facet_parameter and (crawl.total or 0) > HUGE_BOARD


def _last(total: int | None) -> int:
    return RESULT_CAP if total is None else min(total, RESULT_CAP)


def _pages(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def _age_in_days(label: str) -> int | None:
    text = fold(label)
    if "today" in text or "aujourd" in text:
        return 0
    if "yesterday" in text or "hier" in text:
        return 1
    match = _DAYS.search(text)
    if match is None:
        return None
    older = "+" in label or "plus de" in text or "more than" in text
    return int(match.group(1)) + int(older)


def _newest_first(postings: Sequence[WorkdayPosting]) -> bool:
    ages = [_age_in_days(posting.postedOn) for posting in postings]
    if not ages or any(age is None for age in ages):
        return False
    known = [age for age in ages if age is not None]
    if len(set(known)) < 2 and min(known) > 1:
        return False
    return all(left <= right for left, right in zip(known, known[1:], strict=False))


def full_read_day(board: str) -> int:
    from zlib import crc32

    return crc32(board.encode()) % 7


def _pinned_facet(company: Company) -> WorkdayFacet | None:
    if not company.workday_facet:
        return None
    parameter, _, value = company.workday_facet.partition(":")
    if not value:
        return WorkdayFacet(
            tenant=company.workday_tenant or "",
            site=company.workday_site or "",
            parameter=workday_facet_lexicon()[0][0],
            facet_ids=(parameter,),
            pinned=True,
        )
    return WorkdayFacet(
        tenant=company.workday_tenant or "",
        site=company.workday_site or "",
        parameter=parameter,
        facet_ids=tuple(value.split(",")),
        pinned=True,
    )


def _validate(payload: Any, company: Company) -> tuple[WorkdayPage, int]:
    raw = parsed(
        WorkdayRawPage,
        payload,
        source="workday",
        slug=company.slug,
        board=f"workday/{company.slug}",
    )

    shown = [row for row in raw.jobPostings if not _hidden(row)]
    hidden = len(raw.jobPostings) - len(shown)
    kept, dropped = validate_rows(WorkdayPosting, shown, source="workday", slug=company.slug)
    if hidden and not kept:
        dropped, hidden = dropped + hidden, 0
    page = WorkdayPage(total=raw.total, jobPostings=kept, facets=raw.facets, hidden=hidden)
    return page, dropped


def _hidden(row: Any) -> bool:
    return isinstance(row, dict) and not row.get("title") and not row.get("externalPath")


async def _attach_descriptions(
    company: Company,
    client: HttpClient,
    paired: list[tuple[WorkdayPosting, Job]],
    wanted: set[str],
) -> tuple[list[tuple[WorkdayPosting, Job]], list[DetailFetch]]:
    host, _ = workday_target(
        company.workday_tenant or "", company.workday_site or "", company.workday_dc or ""
    )
    outcomes: list[DetailFetch] = []
    merged: list[tuple[WorkdayPosting, Job]] = []
    for posting, job in paired:
        if job.id not in wanted or not posting.externalPath:
            merged.append((posting, job))
            continue
        path = posting.externalPath
        url = f"https://{host}/wday/cxs/{company.workday_tenant}/{company.workday_site}{path}"
        try:
            response = await client.get_json(url)
        except (HttpError, httpx.HTTPError):
            outcomes.append(DetailFetch(id=job.id, resolved=False, failed=True))
            merged.append((posting, job))
            continue
        body = _description_from(response.payload)
        outcomes.append(DetailFetch(id=job.id, resolved=bool(body)))
        merged.append((posting, replace(job, description=body) if body else job))
    return merged, outcomes


def _description_from(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    info = payload.get("jobPostingInfo")
    if not isinstance(info, dict):
        return ""
    body = info.get("jobDescription")
    return collapse_whitespace(strip_html(body)) if isinstance(body, str) else ""


def _board(company: Company) -> str:
    return f"{company.workday_tenant or company.slug}-{company.workday_site or ''}"


def _to_job(
    company: Company, posting: WorkdayPosting, now: datetime, employment_type: str = ""
) -> Job:
    host, _ = workday_target(
        company.workday_tenant or "", company.workday_site or "", company.workday_dc or ""
    )
    raw_path = posting.externalPath
    path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
    apply_url = f"https://{host}/{company.workday_site}{path}" if raw_path else ""
    title = collapse_whitespace(posting.title)
    return Job(
        id=job_id("workday", _board(company), posting.requisition()),
        source="workday",
        company=company.name,
        title_raw=title,
        title_normalized=title.lower(),
        apply_url_raw=apply_url,
        description="",
        location_raw=collapse_whitespace(posting.locationsText),
        first_seen=now,
        last_seen=now,
        signals=SourceSignals(employment_type=employment_type),
    )
