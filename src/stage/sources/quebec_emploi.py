from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from stage.domain import Job, SourceSignals, job_id
from stage.http import HttpClient
from stage.sources import register_feed
from stage.sources._text import collapse_whitespace
from stage.sources.base import (
    FetchResult,
    NonEmptyStr,
    PayloadValidationError,
    capture_payload,
    malformed_note,
    validate_rows,
)

HOST = "www.quebecemploi.gouv.qc.ca"
URL = f"https://{HOST}/search/postingFilteredAI"
PAGE_CAP = 4


class QuebecEmploiListing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ide_affch: int = Field(gt=0)
    titre: NonEmptyStr
    employeur: str = ""
    nom_ville: str = ""


class QuebecEmploiMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_hits: int = Field(ge=0)


class QuebecEmploiPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[Any] = Field(default_factory=list)
    meta: QuebecEmploiMeta


@register_feed
class QuebecEmploiFeed:
    name: ClassVar[str] = "quebec-emploi"
    rate_profile: ClassVar[str] = "conservative"
    hosts: ClassVar[frozenset[str]] = frozenset({HOST})
    bucket_key: ClassVar[str] = "quebec-emploi"

    def season_year(self, now: datetime) -> int:
        return now.year

    def plan(self, now: datetime) -> tuple[str, ...]:
        return (URL,)

    async def fetch(self, client: HttpClient, now: datetime) -> FetchResult:
        listings: list[QuebecEmploiListing] = []
        malformed = 0
        truncated = False

        for page in range(1, PAGE_CAP + 1):
            response = await client.post_json(URL, body=self._request(page))
            rows, dropped, total = self._validate(response.payload)
            listings.extend(rows)
            malformed += dropped
            if len(listings) + malformed >= total:
                break
        else:
            truncated = True

        jobs = tuple(self._to_job(listing, now) for listing in listings)
        notes = []
        if truncated:
            notes.append(f"stopped at Québec Emploi's {PAGE_CAP}-page public-search cap")
        if malformed:
            notes.append(malformed_note(malformed))
        return FetchResult(
            jobs=jobs,
            authoritative=not (truncated or malformed),
            degraded="; ".join(notes),
        )

    def _validate(self, payload: Any) -> tuple[list[QuebecEmploiListing], int, int]:
        try:
            page = QuebecEmploiPage.model_validate(payload)
        except Exception as exc:
            captured = capture_payload(self.name, "stages-students", payload)
            raise PayloadValidationError(
                f"{self.name}: public search response failed validation; raw payload captured at "
                f"{captured}"
            ) from exc
        rows, dropped = validate_rows(
            QuebecEmploiListing, page.items, source=self.name, slug="stages-students"
        )
        return rows, dropped, page.meta.total_hits

    def _to_job(self, listing: QuebecEmploiListing, now: datetime) -> Job:
        title = collapse_whitespace(listing.titre)
        company = collapse_whitespace(listing.employeur) or "Québec Emploi"
        city = collapse_whitespace(listing.nom_ville)
        location = f"{city}, Québec, Canada" if city else "Québec, Canada"
        return Job(
            id=job_id(self.name, "stages-students", str(listing.ide_affch)),
            source=self.name,
            company=company,
            title_raw=title,
            title_normalized=title.lower(),
            apply_url_raw=(f"https://{HOST}/plateforme-emploi/poste/{listing.ide_affch}"),
            description="",
            location_raw=location,
            first_seen=now,
            last_seen=now,
            signals=SourceSignals(employment_type="student stage"),
        )

    @staticmethod
    def _request(page: int) -> dict[str, object]:
        return {
            "sort": {"type": "AUTO"},
            "langue": "fr",
            "page": page,
            "identAWS": "stage-public-feed",
            "filter": {
                "inputSearch": "",
                "address": "",
                "localisation": {"longitude": "", "latitude": "", "distance": 20},
                "adminRegion": [],
                "offerType": ["2", "3"],
                "commitment": [],
                "jobDuration": [],
                "levelEducation": [],
                "studyDiscipline": [],
                "mrc": [],
                "bsq": [],
                "scian": [],
                "postedSince": "",
                "excludeAgencies": False,
                "isUkrainian": False,
                "isExperimente": False,
                "isSubsidized": False,
                "isTrainingProgram": False,
            },
        }
