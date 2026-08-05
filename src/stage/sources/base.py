import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, BeforeValidator, ValidationError

from stage.domain import Company, Job, Platform
from stage.http import HttpClient
from stage.paths import capture_dir


class AdapterError(Exception):
    pass


class PayloadValidationError(AdapterError):
    pass


NullableStr = Annotated[str, BeforeValidator(lambda value: "" if value is None else value)]
NullableBool = Annotated[bool, BeforeValidator(lambda value: False if value is None else value)]


def validate_rows[ModelT: BaseModel](
    model: type[ModelT], rows: Sequence[Any], *, source: str, slug: str
) -> tuple[list[ModelT], int]:
    kept: list[ModelT] = []
    dropped = 0
    for row in rows:
        try:
            kept.append(model.model_validate(row))
        except ValidationError:
            dropped += 1
            capture_payload(f"{source}-posting", slug, row)
    return kept, dropped


def malformed_note(dropped: int) -> str:
    if not dropped:
        return ""
    return (
        f"{dropped} posting(s) failed validation and were dropped; raw rows captured. "
        "The listing is incomplete, so it closes nothing this run"
    )


@dataclass(frozen=True, slots=True)
class FetchResult:
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    not_modified: bool = False
    authoritative: bool = True
    degraded: str = ""
    stale_urls: tuple[str, ...] = field(default_factory=tuple)
    detail_fetches: tuple[object, ...] = field(default_factory=tuple)
    facets: tuple[object, ...] = field(default_factory=tuple)
    forgotten_facets: tuple[object, ...] = field(default_factory=tuple)


@runtime_checkable
class Adapter(Protocol):
    name: ClassVar[str]
    platform: ClassVar[Platform]
    rate_profile: ClassVar[str]
    hosts: ClassVar[frozenset[str]]

    bucket_key: ClassVar[str]

    detail_budget: ClassVar[int]

    rotation_slice: ClassVar[int]

    max_requests_per_company: ClassVar[int]

    def board_key(self, company: Company) -> str: ...

    def hosts_for(self, companies: Sequence[Company]) -> frozenset[str]: ...

    def plan(self, company: Company) -> tuple[str, ...]: ...

    async def fetch(
        self,
        company: Company,
        client: HttpClient,
        now: datetime,
        facets: Any = None,
        details: Sequence[str] = (),
    ) -> FetchResult: ...


def capture_payload(source: str, slug: str, payload: Any) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = capture_dir() / f"{source}-{slug}-{stamp}.json"
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return str(target)
