from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stage.domain import (
    Company,
    DetailFetch,
    Job,
    Platform,
    SourceSignals,
    WorkdayFacet,
    board_key,
    job_id,
)
from stage.http import HttpClient, HttpError
from stage.lexicon import fold, internship_lexicon, workday_facet_lexicon
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)
from stage.sources.platforms import SlugRejectedError, workday_target

PAGE_SIZE = 20
MAX_PAGES = 25
RESULT_CAP = 10_000


class WorkdayPosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    externalPath: str = ""  # noqa: N815 - the API's own field name
    locationsText: str = ""  # noqa: N815 - the API's own field name
    postedOn: str = ""  # noqa: N815 - the API's own field name
    bulletFields: list[str] = Field(default_factory=list)  # noqa: N815 - the API's own name

    def requisition(self) -> str:
        for field in self.bulletFields:
            cleaned = field.strip()
            if cleaned:
                return cleaned
        tail = self.externalPath.rstrip("/").rsplit("_", 1)
        return tail[-1] if len(tail) == 2 and tail[-1] else self.externalPath.rstrip("/")


class WorkdayFacetValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    descriptor: str = ""
    count: int = 0


class WorkdayFacetGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facetParameter: str = ""  # noqa: N815 - the API's own field name
    descriptor: str = ""
    values: list[WorkdayFacetValue] = Field(default_factory=list)


class WorkdayPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: int = 0
    jobPostings: list[WorkdayPosting]  # noqa: N815
    facets: list[WorkdayFacetGroup] | None = None


class WorkdayRawPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: int = 0
    jobPostings: list[dict[str, Any]]  # noqa: N815
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


def facet_still_offered(page: WorkdayPage, facet: WorkdayFacet) -> bool:
    if page.facets is None:
        return True
    for group in page.facets:
        if group.facetParameter != facet.parameter:
            continue
        offered = {value.id for value in group.values}
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

    detail_budget: ClassVar[int] = 60

    rotation_slice: ClassVar[int] = 40

    max_requests_per_company: ClassVar[int] = MAX_PAGES

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
    ) -> FetchResult:
        url = self.plan(company)[0]
        tenant = company.workday_tenant or ""
        site = company.workday_site or ""
        facet = _pinned_facet(company) or (facets or {}).get((tenant, site))
        applied = {facet.parameter: list(facet.facet_ids)} if facet is not None else {}
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
        pages = 0
        total: int | None = None

        while pages < MAX_PAGES:
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
                        f"no facet list at all while facet {facet.facet_ids!r} applied, "
                        f"so staleness is undecided; facet kept, nothing closed. "
                        f"Payload captured at {captured}"
                    )
            elif facet is not None and not facet_still_offered(page, facet):
                stale_facet = True
                if facet.pinned:
                    degraded = (
                        f"pinned facet {facet.facet_ids!r} is no longer offered under "
                        f"{facet.parameter!r}; honoured anyway. Re-pin with "
                        "`stage discover --url` or clear `workday_facet`"
                    )
                else:
                    degraded = (
                        f"cached facet {facet.facet_ids!r} is no longer offered under "
                        f"{facet.parameter!r}; re-resolving from this tenant's own facet list"
                    )
                    forgotten = facet
                    facet = None
                    applied = {}
                    postings.clear()
                    offset = 0
                    total = None
                    continue

            if facet is None and not applied:
                resolved = resolve_facet(page, tenant, site, now)
                if resolved is not None:
                    discovered = resolved
                    forgotten = None
                else:
                    fell_back = True
                    degraded = _fallback_reason(page, company, response.payload)

            if discovered is not None and not applied and not restarted:
                applied = {discovered.parameter: list(discovered.facet_ids)}
                restarted = True
                postings.clear()
                offset = 0
                total = None
                continue

            postings.extend(page.jobPostings)
            if total is None and page.total > 0:
                total = page.total

            returned = len(page.jobPostings) + dropped
            if returned < PAGE_SIZE:
                reached_end = True
                break
            offset += PAGE_SIZE
            if offset >= (RESULT_CAP if total is None else min(total, RESULT_CAP)):
                reached_end = True
                break

        capped = not reached_end and bool(postings)
        if capped:
            degraded = f"stopped at the {MAX_PAGES}-page cap; the board may be truncated"
        if malformed:
            degraded = malformed_note(malformed) + (f" ({degraded})" if degraded else "")

        faceted = "internship" if applied else ""
        paired = [(posting, _to_job(company, posting, now, faceted)) for posting in postings]
        wanted = set(details)
        fetched: list[DetailFetch] = []
        if wanted:
            paired, fetched = await _attach_descriptions(company, client, paired, wanted)

        return FetchResult(
            jobs=tuple(job for _, job in paired),
            degraded=degraded,
            authoritative=not (capped or malformed or fell_back or stale_facet or drifted),
            facets=(discovered,) if discovered is not None else (),
            forgotten_facets=(forgotten,) if forgotten is not None else (),
            detail_fetches=tuple(fetched),
        )


def _fallback_reason(page: WorkdayPage, company: Company, payload: Any) -> str:
    if page.facets:
        names = {group.facetParameter for group in page.facets if group.facetParameter}
        advertised = ", ".join(sorted(names))
        return (
            f"no internship facet among the values this tenant advertises ({advertised}); "
            "walking the whole board instead, which the bilingual classifier then filters"
        )
    if page.facets == []:
        return (
            "this tenant advertises an empty facet list, so there is no internship facet "
            "to resolve; walking the whole board instead"
        )
    captured = capture_payload("workday-nofacets", company.slug, payload)
    return f"no facet list at all, so resolution could not run; payload captured at {captured}"


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
    try:
        raw = WorkdayRawPage.model_validate(payload)
    except ValidationError as exc:
        captured = capture_payload("workday", company.slug, payload)
        raise PayloadValidationError(
            f"workday payload for {company.name} failed validation: {exc} (captured {captured})"
        ) from exc

    kept, dropped = validate_rows(
        WorkdayPosting, raw.jobPostings, source="workday", slug=company.slug
    )
    return WorkdayPage(total=raw.total, jobPostings=kept, facets=raw.facets), dropped


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
        except HttpError:
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
