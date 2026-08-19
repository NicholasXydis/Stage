from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stage.domain import Company, Job, Platform, board_key, job_id
from stage.http import HttpClient
from stage.sources import register
from stage.sources._text import collapse_whitespace, strip_html
from stage.sources.base import (
    FetchResult,
    NullableStr,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)
from stage.sources.platforms import oracle_target

PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
PAGE_SIZE = 100
MAX_PAGES = 10
KEYWORD = "internship"


class OraclePosting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Id: str
    Title: str
    PostedDate: date | None = None
    PrimaryLocation: NullableStr = ""
    PrimaryLocationCountry: NullableStr = ""
    ShortDescriptionStr: NullableStr = ""
    ExternalQualificationsStr: NullableStr = ""
    ExternalResponsibilitiesStr: NullableStr = ""


class OraclePage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    TotalJobsCount: int = Field(ge=0)
    requisitionList: list[Any] = Field(default_factory=list)


@register
class OracleCloudAdapter:
    name: ClassVar[str] = "oracle_cloud"
    platform: ClassVar[Platform] = Platform.ORACLE_CLOUD
    rate_profile: ClassVar[str] = "paginated"
    hosts: ClassVar[frozenset[str]] = frozenset()
    bucket_key: ClassVar[str] = "oracle_cloud"
    detail_budget: ClassVar[int] = 0
    rotation_slice: ClassVar[int] = 0
    max_requests_per_company: ClassVar[int] = MAX_PAGES

    def host_for(self, company: Company) -> str:
        host, _ = oracle_target(company.oracle_host or "", company.oracle_site or "")
        return host

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]:
        return frozenset(self.host_for(company) for company in companies)

    def board_key(self, company: Company) -> str:
        return board_key(self.name, self._board(company))

    def url_for(self, company: Company) -> str:
        return f"https://{self.host_for(company)}{PATH}"

    def params_for(self, company: Company, offset: int) -> dict[str, str]:
        _, site = oracle_target(company.oracle_host or "", company.oracle_site or "")
        return {
            "onlyData": "true",
            "expand": "requisitionList",
            "finder": (
                f"findReqs;siteNumber={site},keyword={KEYWORD},offset={offset},limit={PAGE_SIZE}"
            ),
        }

    def plan(self, company: Company) -> tuple[str, ...]:
        return (self.url_for(company),)

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: object = None,
        details: Sequence[str] = (),
    ) -> FetchResult:
        postings: list[OraclePosting] = []
        malformed = 0
        truncated = False
        premature_end = False

        for page in range(MAX_PAGES):
            response = await client.get_json(
                self.url_for(company), params=self.params_for(company, page * PAGE_SIZE)
            )
            if response.not_modified:
                if page == 0:
                    return FetchResult(not_modified=True)
                return FetchResult(
                    jobs=tuple(self._to_job(company, row, now) for row in postings),
                    authoritative=False,
                    degraded="a later page answered 304, so the walk ended early",
                )
            rows, dropped, total = self._validate(company, response.payload)
            malformed += dropped
            if not rows and not dropped:
                premature_end = len(postings) + malformed < total
                break
            postings.extend(rows)
            if len(postings) + malformed >= total:
                break
        else:
            truncated = True

        notes = []
        if truncated:
            notes.append(f"stopped at the {MAX_PAGES}-page cap")
        if premature_end:
            notes.append("received an empty page before the reported total")
        if malformed:
            notes.append(malformed_note(malformed))
        return FetchResult(
            jobs=tuple(self._to_job(company, row, now) for row in postings),
            authoritative=not (truncated or premature_end or malformed),
            degraded="; ".join(notes),
        )

    def _validate(self, company: Company, payload: Any) -> tuple[list[OraclePosting], int, int]:
        try:
            parents = payload["items"] if isinstance(payload, dict) else None
            if not isinstance(parents, list) or len(parents) != 1:
                raise ValueError("items must contain exactly one search result")
            page = OraclePage.model_validate(parents[0])
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            captured = capture_payload(self.name, company.slug, payload)
            raise PayloadValidationError(
                f"oracle_cloud/{company.slug}: invalid recruiting search response; raw payload "
                f"captured at {captured}"
            ) from exc
        rows, dropped = validate_rows(
            OraclePosting, page.requisitionList, source=self.name, slug=company.slug
        )
        return rows, dropped, page.TotalJobsCount

    def _to_job(self, company: Company, row: OraclePosting, now: datetime) -> Job:
        host, site = oracle_target(company.oracle_host or "", company.oracle_site or "")
        title = collapse_whitespace(row.Title)
        description = collapse_whitespace(
            " ".join(
                strip_html(text)
                for text in (
                    row.ShortDescriptionStr,
                    row.ExternalQualificationsStr,
                    row.ExternalResponsibilitiesStr,
                )
                if text
            )
        )
        location = collapse_whitespace(
            ", ".join(part for part in (row.PrimaryLocation, row.PrimaryLocationCountry) if part)
        )
        posted = (
            datetime.combine(row.PostedDate, datetime.min.time(), tzinfo=UTC)
            if row.PostedDate is not None
            else None
        )
        return Job(
            id=job_id(self.name, self._board(company), row.Id),
            source=self.name,
            company=company.name,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{row.Id}",
            description=description,
            location_raw=location,
            first_seen=now,
            last_seen=now,
            source_posted_at=posted,
        )

    def _board(self, company: Company) -> str:
        return f"{company.oracle_host or company.slug}-{company.oracle_site or ''}"
